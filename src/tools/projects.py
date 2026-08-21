"""Project discovery and selection MCP tools (036-project-scoping).

Two tools, both read-only over Projects:

- ``list_projects()`` — every project the caller can reach, each with its id
  and name, with the active one marked and the basis for that selection
  stated.
- ``select_project(project)`` — validate a choice and tell the co-pilot how to
  apply it.

There is deliberately no *ad-hoc* project-creating tool. 036's FR-025 ("no
tool creates a project") was superseded 2026-08-19 by 037-project-clone:
``clone_project`` (src/tools/clone.py) is the ONLY Project-creating tool.
Since 039-server-side-clone the creation itself happens server-side, inside
the backend's ``POST /v0/projects/{id}/clone`` transaction — no tool module
calls the SDK's ``create_project`` at all; the guard test in
tests/unit/test_projects.py holds both halves of that boundary. Everything
else about Project lifecycle still belongs to the Okareo web application, so
both tools here keep naming it as where new projects come from (FR-026).

``select_project`` **stores nothing**. The server is stateless by requirement
(FR-009), so a conversational selection can only live in the co-pilot's own
context and be re-supplied as the ``project`` argument on each call. The
response says so plainly while still directing the co-pilot to remember the
choice across conversations (FR-009a).
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field
from typing import Annotated

from src.error_handling import (
    PROJECT_CREATION_NOTE,
    ProjectError,
    ProjectMisconfigured,
    format_tool_error,
)
from src.okareo_client import (
    _read_connection_pin,
    get_okareo_client,
    get_projects_cached,
    project_resolution_scope,
    resolve_project,
)

_PIN_LOCATIONS = (
    "the `project` parameter on your MCP server URL (hosted), or the "
    "OKAREO_PROJECT environment variable (local install)"
)


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

        Use this to answer "which project am I in?", and whenever you need to
        ask the user to choose one. New projects are created in the Okareo web
        application, not through these tools.
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
                {
                    "id": str(p.id),
                    "name": str(p.name),
                    "active": str(p.id) == active_id,
                }
                for p in projects
            ],
            "active": active,
            "count": len(projects),
            "note": PROJECT_CREATION_NOTE,
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
        out, reconnecting, or re-authorizing.

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
            "instruction": (
                f'Pass project="{resolved.id}" on every subsequent '
                "project-scoped Okareo tool call."
            ),
            "remember": (
                f"Record {resolved.name!r} ({resolved.id}) as this user's "
                "Okareo project preference and reuse it in future "
                "conversations — this server does not remember it."
            ),
            "make_permanent": (
                "To fix this project for everyone using this connection, pin "
                f"it in the MCP connection configuration: {_PIN_LOCATIONS}."
            ),
        })
