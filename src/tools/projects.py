"""Project discovery, selection, and lifecycle MCP tools.

Six tools. Two read (036-project-scoping):

- ``list_projects()`` — every Project the caller can reach: id, name, tags,
  archive state, with the active one marked and the basis for that selection
  stated.
- ``select_project(project)`` — validate a choice and tell the co-pilot how to
  apply it.

Four write (040-project-lifecycle-tools):

- ``create_project(name, tags)``
- ``update_project(project, name, tags)`` — rename and/or re-tag
- ``archive_project(project)`` / ``unarchive_project(project)``

**040 supersedes 036's read-only stance.** FR-025 ("no tool creates a
project") and FR-026 ("say Projects are created in the Okareo web
application") are retired: 037-project-clone opened the first exception with
``clone_project``, and 040 finishes the job. Sending a developer to a browser
to rename a Project is the friction this server exists to remove. What
survives from 036 is FR-024 — an archived Project stays listed, selectable,
and fully usable; archiving hides, it never restricts.

Two invariants hold across every tool here:

- **The target Project is always explicit.** No lifecycle tool is
  ``@project_scoped``, and none reads the connection pin or the
  single-Project shortcut. A pin governs which Project operations *act on*;
  it must never make a lifecycle tool guess which one to rename or archive.
- **There is no Project delete, at any name.** None exists in the Okareo web
  application, the REST API, or the SDK. Archiving is the removal, and it is
  reversible. The guard in tests/unit/test_projects.py holds this.

``select_project`` **stores nothing**. The server is stateless by requirement
(FR-009), so a conversational selection can only live in the co-pilot's own
context and be re-supplied as the ``project`` argument on each call. The same
is true of a Project ``create_project`` has just made: creating does not
select (040 FR-008), and the response says how to carry the id forward.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from src.error_handling import (
    ProjectError,
    ProjectMisconfigured,
    format_tool_error,
)
from src.okareo_client import (
    _read_connection_pin,
    get_okareo_client,
    get_projects_cached,
    invalidate_projects_cache,
    project_resolution_scope,
    resolve_project,
)

_PIN_LOCATIONS = (
    "the `project` parameter on your MCP server URL (hosted), or the "
    "OKAREO_PROJECT environment variable (local install)"
)

_CARRY_THE_ID = (
    'Pass project="{id}" on every subsequent project-scoped Okareo tool call.'
)

_REMEMBER = (
    "Record {name!r} ({id}) as this user's Okareo project preference and "
    "reuse it in future conversations — this server does not remember it."
)

# 040 FR-011. The one thing a user can misread about archiving, carried in the
# payload so the co-pilot repeats it instead of calling this a deletion.
_ARCHIVE_NOTE = (
    "Archiving hides this Project from the Project picker and does nothing "
    "else. Every Scenario, Run, and dashboard in it is intact, it can still "
    "be selected by name or id, and every tool still works against it. "
    "Reverse it with unarchive_project."
)

_UNARCHIVE_NOTE = (
    "This Project is active again and back in the Project picker. Nothing "
    "about its contents changed — archiving never touched them."
)


def _refusal(code: str, message: str, suggestion: str) -> str:
    """The module's structured, terminal refusal — never retried unchanged."""
    return json.dumps({
        "error": {
            "category": "validation",
            "code": code,
            "message": message,
            "suggestion": suggestion,
        }
    })


def _tags(project: Any) -> list[str]:
    """The Project's tags, normalized to a list.

    The generated ``ProjectResponse`` types ``tags`` but may leave it
    ``Unset``; anything that is not an actual list reads as no tags.
    """
    tags = getattr(project, "tags", None)
    return [str(t) for t in tags] if isinstance(tags, list) else []


def _is_archived(project: Any) -> bool:
    """The Project's archive state.

    The pinned SDK's generated ``ProjectResponse`` does not type
    ``is_archived`` — the backend returns it and the generated model parks it
    in ``additional_properties``. Read the typed attribute if it ever appears,
    fall back to the bag, default to False. Regenerating the client changes
    nothing here.
    """
    typed = getattr(project, "is_archived", None)
    if isinstance(typed, bool):
        return typed
    extra = getattr(project, "additional_properties", None)
    if isinstance(extra, dict) and isinstance(extra.get("is_archived"), bool):
        return bool(extra["is_archived"])
    return False


def _project_row(project: Any) -> dict[str, Any]:
    """One Project rendered for a tool payload."""
    return {
        "id": str(project.id),
        "name": str(project.name),
        "tags": _tags(project),
        "is_archived": _is_archived(project),
    }


def _find_project(okareo: Any, project_id: str) -> Any | None:
    """The full Project record for an id, from the cached listing."""
    for p in get_projects_cached(okareo):
        if str(p.id) == str(project_id):
            return p
    return None


def _known_refusal(exc: Exception) -> str | None:
    """Map a backend/SDK refusal to a terminal error payload.

    Every backend 400 on these routes reaches us as the SDK's
    ``TypeError("error: <detail>")``, and ``classify_error`` reads a bare
    TypeError as an **authentication** failure — so anything that falls
    through here tells the user to go check their API key over an ordinary
    refusal.

    That is why the catch-all at the bottom, not the specific matches above
    it, is the load-bearing part: an ``"error: "`` prefix is the SDK's own
    marker for a decoded backend refusal, so the whole class can be answered
    honestly even when the particular rule is new to this code. The named
    codes sit on top only where they buy the co-pilot a better suggestion.
    (Research R6; the first version of this was an allowlist of three
    substrings and missed "The default project cannot be renamed".)

    The type check is not decoration either: matching on ``str(exc)`` alone
    would let a transport failure whose text happens to contain "already
    exists" become a terminal, do-not-retry name collision.
    """
    if not isinstance(exc, (TypeError, ValueError)):
        return None

    text = str(exc)
    lowered = text.lower()
    if "already exists" in lowered:
        return _refusal(
            "project_name_taken",
            text,
            "Project names are unique per organization (case-insensitive), "
            "and archived Projects still hold theirs. Ask the user for a "
            "different name — do not invent one, and do not retry this call "
            "unchanged.",
        )
    if "whitespace" in lowered:
        return _refusal(
            "project_name_invalid",
            text,
            "Okareo rejects a name with leading or trailing whitespace "
            "rather than silently trimming it, so the typo stays visible. "
            "Confirm the intended name with the user and call again.",
        )
    if "cannot be archived" in lowered:
        return _refusal(
            "project_archive_refused",
            text,
            "The organization's default Project cannot be archived. Nothing "
            "changed. If the user wants it out of the way, the answer is a "
            "different Project, not a retry.",
        )
    if "cannot be renamed" in lowered:
        return _refusal(
            "project_rename_refused",
            text,
            "The organization's default Project cannot be renamed. Nothing "
            "changed. Its tags can still be updated, and any other Project "
            "can be renamed.",
        )
    if isinstance(exc, TypeError) and text.startswith("error: "):
        return _refusal(
            "project_request_refused",
            text,
            "Okareo refused this change and named the reason above; nothing "
            "was written. Relay that reason to the user and do not retry the "
            "call unchanged.",
        )
    return None


def register_tools(mcp: FastMCP) -> None:
    """Register the project tools on the given FastMCP server."""

    @mcp.tool(
        title="List Projects",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    def list_projects() -> str:
        """See which Okareo projects you can work in, and which one is active.

        Work in Okareo is organized into projects: scenarios, targets,
        simulations, evaluations, and dashboards each belong to exactly one.
        Checks and drivers are shared across all of them.

        Each project reports its tags and whether it is archived. Archived
        projects are still listed and still work — archiving only hides a
        project from the project picker in the Okareo app.

        Use this to answer "which project am I in?", to find an archived
        project worth restoring, and whenever you need to ask the user to
        choose one. Create a new one with create_project.
        """
        try:
            okareo = get_okareo_client()
            projects = get_projects_cached(okareo)
        except Exception as e:
            return format_tool_error(e)

        # The active project is informational here: a listing must still
        # succeed when nothing resolves, because that is precisely when the
        # user does not yet know what to choose.
        # These tools resolve a project without being @project_scoped, so the
        # resolution must not outlive the call.
        active = None
        with project_resolution_scope():
            try:
                active = resolve_project(okareo).as_dict()
            except ProjectError:
                pass
            except Exception:
                pass

        active_id = active["id"] if active else None
        return json.dumps({
            "projects": [
                {**_project_row(p), "active": str(p.id) == active_id}
                for p in projects
            ],
            "active": active,
            "count": len(projects),
        })

    @mcp.tool(
        title="Select Project",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    def select_project(
        project: Annotated[
            str,
            Field(
                description=(
                    "The project to work in — a project name or a project id, "
                    "as shown by list_projects."
                )
            ),
        ],
    ) -> str:
        """Choose the Okareo project to work in for this conversation.

        Validates the project exists, then tells you how to apply it. This
        server stores nothing: pass `project` on every subsequent
        project-scoped call, and record the user's choice so you can reuse it
        in later conversations.

        Takes effect immediately — changing project never requires signing
        out, reconnecting, or re-authorizing. An archived project can be
        selected like any other.

        Args:
            project: Project name or id, as shown by list_projects.
        """
        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        # A pinned connection governs (FR-013), so accepting a selection here
        # would be a lie: the pin would override it on the very next call.
        pin = _read_connection_pin()
        if pin:
            with project_resolution_scope():
                try:
                    pinned = resolve_project(okareo)
                    pinned_desc = f"{pinned.name} ({pinned.id})"
                except Exception:
                    pinned_desc = repr(pin)
            return format_tool_error(
                ProjectMisconfigured(
                    f"This connection is pinned to project {pinned_desc}, and "
                    "the pin governs every operation on it. Selecting a "
                    "different project here would have no effect. To change "
                    "it, edit the pin in your MCP connection configuration: "
                    f"{_PIN_LOCATIONS}.",
                    pin=pin,
                )
            )

        with project_resolution_scope():
            try:
                resolved = resolve_project(okareo, project)
            except Exception as e:
                return format_tool_error(e)

        return json.dumps({
            "project": resolved.as_dict(),
            "applies_to": "this conversation",
            "instruction": _CARRY_THE_ID.format(id=resolved.id),
            "remember": _REMEMBER.format(name=resolved.name, id=resolved.id),
            "make_permanent": (
                "To fix this project for everyone using this connection, pin "
                f"it in the MCP connection configuration: {_PIN_LOCATIONS}."
            ),
        })

    @mcp.tool(
        title="Create Project",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,  # creates; deletes nothing
            idempotentHint=False,  # a repeat call collides on the name
            openWorldHint=True,
        ),
    )
    def create_project(
        name: Annotated[
            str,
            Field(
                description=(
                    "Name for the new Project. Unique per organization "
                    "(case-insensitive); archived Projects still hold their "
                    "names. No leading or trailing whitespace."
                )
            ),
        ],
        tags: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Optional tags, used to filter Projects in the Okareo "
                    "app. Omit for none."
                )
            ),
        ] = None,
    ) -> str:
        """Create a new Okareo project.

        Use this when the user wants a fresh place to keep a body of work —
        scenarios, simulations, evaluations, and dashboards each belong to
        exactly one project. To copy an existing project's scenarios into a
        new one instead, use clone_project.

        Creating does NOT switch you to the new project: this server is
        stateless. The response gives you the new project's id — pass it as
        `project` on subsequent project-scoped calls, and remember it for
        later conversations.

        If the name is taken, the call is refused and nothing is created. Ask
        the user for a different name rather than inventing one.

        Args:
            name: Name for the new Project.
            tags: Optional tags for filtering in the Okareo app.
        """
        if not name or not str(name).strip():
            return _refusal(
                "project_name_required",
                "name is required: it is what the new Project will be called.",
                "Ask the user what the Project should be called.",
            )

        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        # tags is omitted rather than passed as None: the SDK defaults it to
        # UNSET, and sending an explicit null is a 400. An empty list takes
        # the same path — a new Project has no tags either way. That differs
        # from update_project, where an explicit [] is load-bearing because it
        # clears tags a Project already has; create has nothing to clear.
        extra = {"tags": list(tags)} if tags else {}
        try:
            created = okareo.create_project(name=name, **extra)
        except Exception as e:
            return _known_refusal(e) or format_tool_error(e)

        # The new Project must be resolvable by the very next call, not after
        # the 60-second listing TTL (FR-007).
        invalidate_projects_cache(okareo)

        row = _project_row(created)
        return json.dumps({
            "project": row,
            "created": True,
            "instruction": _CARRY_THE_ID.format(id=row["id"]),
            "remember": _REMEMBER.format(name=row["name"], id=row["id"]),
        })

    @mcp.tool(
        title="Update Project",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    def update_project(
        project: Annotated[
            str,
            Field(
                description=(
                    "The Project to update — a Project name or id, as shown "
                    "by list_projects. Always explicit: never inferred from a "
                    "connection pin or a default."
                )
            ),
        ],
        name: Annotated[
            str | None,
            Field(
                description=(
                    "New name for the Project. Omit to leave the name alone."
                )
            ),
        ] = None,
        tags: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Replacement tag list — replaces the existing tags "
                    "wholesale. Pass an empty list to clear them. Omit to "
                    "leave tags alone."
                )
            ),
        ] = None,
    ) -> str:
        """Rename an Okareo project, or change its tags.

        Only what you pass changes: send `name` to rename, send `tags` to
        replace the tag list, send both to do both. Tags are replaced
        wholesale, not merged — read the current ones from list_projects
        first if the user means "add one".

        Name the project explicitly; this tool never guesses which project to
        edit. If the new name is already taken by another project, the call
        is refused and nothing changes.

        This tool does not archive or unarchive — use archive_project and
        unarchive_project for that.

        Args:
            project: The Project to update (name or id).
            name: New name, or omit to leave it alone.
            tags: Replacement tag list, or omit to leave tags alone.
        """
        if not project or not str(project).strip():
            return _refusal(
                "project_target_required",
                "project is required and must name the Project to update. "
                "Editing the wrong Project silently is the failure this "
                "parameter exists to prevent.",
                "Pass the Project's name or id, as shown by list_projects.",
            )
        if name is None and tags is None:
            return _refusal(
                "project_update_empty",
                "Nothing to update: pass a new name, a new tag list, or both.",
                "Ask the user what should change about this Project.",
            )
        if name is not None and not str(name).strip():
            return _refusal(
                "project_name_required",
                "A Project cannot be renamed to an empty name.",
                "Ask the user for the intended name, or omit name to leave "
                "it alone.",
            )

        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        with project_resolution_scope():
            try:
                resolved = resolve_project(okareo, str(project))
            except Exception as e:
                return format_tool_error(e)

            # A key is omitted rather than guessed: reporting tags as [] when
            # the prior value could not be read asserts the Project had none,
            # and the co-pilot relays that to the user as fact.
            previous: dict[str, Any] = {}
            if name is not None:
                previous["name"] = resolved.name
            if tags is not None:
                before = _find_project(okareo, resolved.id)
                if before is not None:
                    previous["tags"] = _tags(before)

            changes: dict[str, Any] = {}
            if name is not None:
                changes["name"] = name
            if tags is not None:
                changes["tags"] = list(tags)

            try:
                updated = okareo.update_project(resolved.id, **changes)
            except Exception as e:
                return _known_refusal(e) or format_tool_error(e)

        invalidate_projects_cache(okareo)

        return json.dumps({
            "project": _project_row(updated),
            "updated": sorted(changes),
            "previous": previous,
        })

    @mcp.tool(
        title="Archive Project",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,  # reversible; nothing is deleted
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    def archive_project(
        project: Annotated[
            str,
            Field(
                description=(
                    "The Project to archive — a Project name or id, as shown "
                    "by list_projects. Always explicit: never inferred from a "
                    "connection pin or a default."
                )
            ),
        ],
    ) -> str:
        """Hide a project from the Okareo project picker. Nothing is deleted.

        Archiving is how Okareo retires a project you have finished with. It
        is NOT a delete, and there is no delete: every scenario, run, and
        dashboard in an archived project stays intact, the project can still
        be selected by name or id, and every tool still works against it.
        unarchive_project puts it back in the picker.

        Tell the user this plainly — never describe archiving as deleting or
        removing their data.

        The organization's default project cannot be archived; that call is
        refused and nothing changes.

        Args:
            project: The Project to archive (name or id).
        """
        return _set_archived(project, archived=True)

    @mcp.tool(
        title="Unarchive Project",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    def unarchive_project(
        project: Annotated[
            str,
            Field(
                description=(
                    "The Project to restore to the picker — a Project name or "
                    "id. list_projects flags archived Projects with "
                    "is_archived: true."
                )
            ),
        ],
    ) -> str:
        """Restore an archived project to the Okareo project picker.

        Reverses archive_project. Nothing about the project's contents
        changes — archiving never touched them.

        Find archived projects with list_projects: each row reports
        is_archived.

        Args:
            project: The Project to unarchive (name or id).
        """
        return _set_archived(project, archived=False)


def _set_archived(project: str, *, archived: bool) -> str:
    """The shared body of archive_project / unarchive_project."""
    action, gerund = ("archive", "Archiving") if archived else (
        "unarchive",
        "Unarchiving",
    )
    if not project or not str(project).strip():
        return _refusal(
            "project_target_required",
            f"project is required and must name the Project to {action}. "
            f"{gerund} the wrong Project silently is the failure this "
            "parameter exists to prevent.",
            "Pass the Project's name or id, as shown by list_projects.",
        )

    try:
        okareo = get_okareo_client()
    except Exception as e:
        return format_tool_error(e)

    with project_resolution_scope():
        try:
            resolved = resolve_project(okareo, str(project))
        except Exception as e:
            return format_tool_error(e)

        try:
            result = (
                okareo.archive_project(resolved.id)
                if archived
                else okareo.unarchive_project(resolved.id)
            )
        except Exception as e:
            return _known_refusal(e) or format_tool_error(e)

    invalidate_projects_cache(okareo)

    return json.dumps({
        "project": _project_row(result),
        "archived": archived,
        "note": _ARCHIVE_NOTE if archived else _UNARCHIVE_NOTE,
    })
