"""Project cloning tool (037-project-clone).

One tool, ``clone_project``: copy every Scenario from a golden Project into a
newly created destination Project, verify each copy field-for-field, and
report. Scenario rows move exclusively inside this process — never through
the model conversation (FR-005).

This is the one sanctioned Project-creating tool (037 FR-006, superseding
036 FR-025). It is deliberately NOT ``@project_scoped``: a two-Project
operation cannot be honestly stamped with a single ``project`` block, so both
identities travel in the response body instead (FR-012). Source resolution
still runs inside ``project_resolution_scope()`` so nothing leaks into a
later operation's error report.

Known backend asymmetries this module compensates for (research R1/R2):

- the Scenario create request cannot express ``tags`` and preserves
  ``generation_type`` only for DRIVER/CUSTOM_GENERATOR — both repaired via
  the update PUT, then verified by re-read;
- per-row ``meta_data`` has no write path at all — reported as a fidelity
  gap when a source row carries any;
- a pre-fix backend soft-returns an existing set on a duplicate Scenario
  name — detected (``warning`` non-null, or a returned Project other than
  the destination) and turned into a loud abort (FR-011).
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from src.error_handling import format_tool_error
from src.okareo_client import (
    get_okareo_client,
    invalidate_projects_cache,
    project_resolution_scope,
    resolve_project,
)

_CONFIRM_DIRECTIVE = (
    "Present these counts to the user and call clone_project again with "
    "dry_run=false only after they explicitly confirm."
)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Dict-or-attribute access, tolerant of generated-model shapes."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _tags(value: Any) -> list[str]:
    """Normalize a tags field (None/Unset/list) to a plain list of strings."""
    if not value:
        return []
    return [str(t) for t in value]


def _type_of(item: Any) -> str:
    """A Scenario's generation type as a plain string, from either field name."""
    value = _get(item, "type_", None)
    if not value:
        value = _get(item, "type", None)
    return str(value) if value else ""


def _canon(value: Any) -> str:
    """Canonical form for row-content comparison — absorbs dict key order."""
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _has_metadata(datapoint: Any) -> bool:
    """True when a source row carries per-row metadata (which has no write path)."""
    meta = _get(datapoint, "meta_data", None)
    if not meta:
        return False
    if isinstance(meta, dict):
        return bool(meta)
    props = getattr(meta, "additional_properties", None)
    if props is not None:
        return bool(props)
    return True


def _refusal(code: str, message: str, suggestion: str, *, category: str = "validation", **data: Any) -> str:
    payload: dict[str, Any] = {
        "category": category,
        "code": code,
        "message": message,
        "suggestion": suggestion,
    }
    payload.update(data)
    return json.dumps({"error": payload})


def _list_scenarios(okareo: Any, project_id: str) -> list[Any]:
    from okareo_api_client.api.default import (
        get_scenario_sets_v0_scenario_sets_get,
    )

    listing = get_scenario_sets_v0_scenario_sets_get.sync(
        client=okareo.client,
        project_id=project_id,
        api_key=okareo.api_key,
    )
    if not listing or isinstance(listing, Exception):
        return []
    return list(listing)


def _find_project_by_name(okareo: Any, name: str) -> Any:
    """Fresh (uncached) lookup of a Project by name or id — resume detection
    must not act on a 60-second-old picture of the organization."""
    wanted = name.strip().casefold()
    for project in okareo.get_projects():
        if str(project.name).strip().casefold() == wanted:
            return project
        if str(project.id).casefold() == wanted:
            return project
    return None


def _repair(okareo: Any, scenario_id: str, project_id: str, tags: list[str], type_: str) -> None:
    """Re-apply what the create endpoint dropped (tags, non-Driver types)."""
    from okareo_api_client.api.default import (
        update_scenario_set_v0_scenario_sets_scenario_id_put,
    )
    from okareo_api_client.models.scenario_set_update import ScenarioSetUpdate

    body = ScenarioSetUpdate(project_id=project_id, tags=tags, type_=type_)
    update_scenario_set_v0_scenario_sets_scenario_id_put.sync(
        scenario_id=scenario_id,
        client=okareo.client,
        body=body,
        api_key=okareo.api_key,
    )


def _generation_type(type_value: str) -> Any:
    from okareo_api_client.models.scenario_type import ScenarioType

    try:
        return ScenarioType(str(type_value))
    except ValueError:
        return None


def register_tools(mcp: FastMCP) -> None:
    """Register the clone tool on the given FastMCP server."""

    @mcp.tool(
        title="Clone Project",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    def clone_project(
        source_project: Annotated[
            str,
            Field(
                description=(
                    "The golden Project to clone — a Project name or id, as "
                    "shown by list_projects. Always explicit: the source is "
                    "never inferred from a connection pin or a default."
                )
            ),
        ],
        new_project_name: Annotated[
            str,
            Field(
                description=(
                    "Name for the new destination Project. Project names are "
                    "unique per organization; a name already in use is "
                    "rejected unless it holds a prior, resumable clone of "
                    "this same source."
                )
            ),
        ],
        dry_run: Annotated[
            bool,
            Field(
                description=(
                    "true: count the source's Scenarios and rows and report "
                    "what the clone would do, writing nothing. Always start "
                    "here."
                )
            ),
        ] = False,
    ) -> str:
        """Clone a golden Project: copy every Scenario into a new Project.

        Copies each Scenario faithfully — same name, generation type, tags,
        and rows in the same order — into a brand-new Project it creates,
        then verifies every copy by reading it back. Scenario data never
        enters this conversation; rows are copied server-side, so size does
        not matter.

        Protocol — always follow, in order:
        1. Call with dry_run=true first.
        2. Present the counts to the user ("N Scenarios, ~M rows, into new
           Project '<name>' — proceed?").
        3. Only after the user explicitly confirms, call again with
           dry_run=false to execute.

        If a clone fails partway, run the exact same call again: Scenarios
        already copied are skipped, missing ones are copied — safe to repeat
        until the report shows everything verified. A destination name that
        belongs to an unrelated existing Project is refused, never merged
        into.

        Typical next steps after a clone: register the account's Target in
        the new Project (create_or_update_target with project set to it),
        select_project to keep working there, then run the first simulation.

        Args:
            source_project: The golden Project to clone (name or id).
            new_project_name: Name for the new destination Project.
            dry_run: true to count and report without writing anything.
        """
        if not source_project or not str(source_project).strip():
            return _refusal(
                "clone_source_required",
                "source_project is required and must name the Project to "
                "clone. The source is never inferred from a connection pin "
                "or a default — cloning the wrong Project silently is the "
                "failure this parameter exists to prevent.",
                "Pass the golden Project's name or id as source_project.",
            )
        if not new_project_name or not str(new_project_name).strip():
            return _refusal(
                "clone_destination_required",
                "new_project_name is required: it names the destination "
                "Project the clone will create.",
                "Pass the new Project's name as new_project_name.",
            )

        new_name = str(new_project_name).strip()

        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        with project_resolution_scope():
            try:
                source = resolve_project(okareo, str(source_project))
            except Exception as e:
                return format_tool_error(e)

        if (
            new_name.casefold() == source.name.strip().casefold()
            or new_name.casefold() == source.id.casefold()
        ):
            return _refusal(
                "clone_refused_source_is_destination",
                f"new_project_name resolves to the source Project "
                f"{source.name!r} ({source.id}) itself — a Project cannot be "
                "cloned onto itself.",
                "Choose a different new_project_name.",
            )

        try:
            source_listing = _list_scenarios(okareo, source.id)
        except Exception as e:
            return format_tool_error(e)

        source_block = {
            "id": source.id,
            "name": source.name,
            "basis": source.basis,
        }

        if dry_run:
            scenarios = [
                {
                    "name": str(_get(s, "name", "")),
                    "row_count": int(_get(s, "scenario_count", 0) or 0),
                    "tags": _tags(_get(s, "tags", [])),
                    "generation_type": _type_of(s),
                }
                for s in source_listing
            ]
            try:
                destination_exists = _find_project_by_name(okareo, new_name) is not None
            except Exception:
                destination_exists = False
            return json.dumps({
                "dry_run": True,
                "source_project": source_block,
                "new_project_name": new_name,
                "scenario_count": len(scenarios),
                "row_count": sum(s["row_count"] for s in scenarios),
                "scenarios": scenarios,
                "destination_exists": destination_exists,
                "status": "dry_run",
                "next_step": _CONFIRM_DIRECTIVE,
            }, default=str)

        # ---- Execute: destination Project -------------------------------
        source_names = {str(_get(s, "name", "")) for s in source_listing}
        destination_created = True
        pre_existing: dict[str, Any] = {}
        # Plain human-readable tag, deliberately NOT the __-prefixed system
        # namespace: it renders as-is in the Projects table's existing tag
        # chips with zero frontend work. Being user-editable is accepted.
        provenance_tag = f"Cloned from: {source.name}"
        try:
            created = okareo.create_project(new_name, tags=[provenance_tag])
            destination_id = str(created.id)
            destination_name = str(created.name)
            # The clone's own source resolution just primed the project
            # cache; without this, the new Project is invisible to every
            # project-scoped tool — including the follow-on steps this
            # tool's report recommends — until the TTL expires.
            invalidate_projects_cache(okareo)
        except Exception as create_exc:
            # The backend rejected the create. If the name resolves to an
            # existing Project, the rejection was its per-organization name
            # uniqueness — decide resume vs refusal. Anything else is
            # formatted as-is. (Research R3: the SDK raises a bare TypeError
            # for every backend rejection, so the cause is established by
            # re-resolving the name, never by parsing the message.)
            try:
                existing = _find_project_by_name(okareo, new_name)
            except Exception:
                existing = None
            if existing is None:
                return format_tool_error(create_exc)
            if str(existing.id) == source.id:
                return _refusal(
                    "clone_refused_source_is_destination",
                    f"new_project_name resolves to the source Project "
                    f"{source.name!r} ({source.id}) itself — a Project "
                    "cannot be cloned onto itself.",
                    "Choose a different new_project_name.",
                )
            try:
                existing_listing = _list_scenarios(okareo, str(existing.id))
            except Exception as e:
                return format_tool_error(e)
            existing_names = {str(_get(s, "name", "")) for s in existing_listing}
            strangers = existing_names - source_names
            if strangers:
                return _refusal(
                    "clone_refused_destination_conflict",
                    f"A Project named {new_name!r} already exists and holds "
                    f"{len(strangers)} Scenario(s) that are not in the "
                    "source — it is not a prior clone of "
                    f"{source.name!r}, so cloning into it would merge "
                    f"unrelated work. The backend rejected creating it "
                    f"again: {create_exc}",
                    "Choose a different new_project_name, or clean up the "
                    "existing Project in the Okareo web application first.",
                    conflicting_scenarios=len(strangers),
                    destination={
                        "id": str(existing.id),
                        "name": str(existing.name),
                    },
                )
            # A prior (possibly partial) clone of this source: resume into it.
            destination_created = False
            destination_id = str(existing.id)
            destination_name = str(existing.name)
            pre_existing = {
                str(_get(s, "name", "")): s for s in existing_listing
            }

        # ---- Execute: per-Scenario copy ----------------------------------
        from okareo_api_client.models.scenario_set_create import ScenarioSetCreate
        from okareo_api_client.models.seed_data import SeedData

        work: list[dict[str, Any]] = []
        created_count = 0
        for s in source_listing:
            name = str(_get(s, "name", ""))
            item: dict[str, Any] = {
                "name": name,
                "_source_id": str(_get(s, "scenario_id", "")),
                "generation_type": _type_of(s),
                "tags": _tags(_get(s, "tags", [])),
                "row_count": int(_get(s, "scenario_count", 0) or 0),
                "_rows": None,
                "error": None,
            }
            work.append(item)

            if name in pre_existing:
                pre = pre_existing[name]
                item["status"] = "skipped_existing"
                item["destination_id"] = str(_get(pre, "scenario_id", ""))
                # Converge what a prior run may have died before repairing.
                if (
                    _tags(_get(pre, "tags", [])) != item["tags"]
                    or _type_of(pre) != item["generation_type"]
                ):
                    try:
                        _repair(
                            okareo,
                            item["destination_id"],
                            destination_id,
                            item["tags"],
                            item["generation_type"],
                        )
                    except Exception as e:
                        item["error"] = f"tag/type convergence failed: {e}"
                continue

            try:
                rows = okareo.get_scenario_data_points(item["_source_id"])
            except Exception as e:
                item["status"] = "failed"
                item["error"] = str(e)
                continue
            item["_rows"] = list(rows)
            item["row_count"] = len(item["_rows"])

            seed_data = [
                SeedData(input_=_get(dp, "input_"), result=_get(dp, "result"))
                for dp in item["_rows"]
            ]
            create_kwargs: dict[str, Any] = {
                "name": name,
                "seed_data": seed_data,
                "project_id": destination_id,
            }
            generation_type = _generation_type(item["generation_type"])
            if generation_type is not None:
                create_kwargs["generation_type"] = generation_type
            try:
                response = okareo.create_scenario_set(
                    ScenarioSetCreate(**create_kwargs)
                )
            except Exception as e:
                item["status"] = "failed"
                item["error"] = str(e)
                continue

            # FR-011: the pre-fix backend soft-returns an existing set on a
            # duplicate name. Either signal aborts the whole clone — every
            # subsequent create would soft-return too.
            warning = _get(response, "warning", None)
            returned_project = str(_get(response, "project_id", "") or "")
            if warning or returned_project != destination_id:
                detail = (
                    f"backend warning: {warning}"
                    if warning
                    else f"returned Scenario belongs to Project {returned_project}, "
                    f"not the destination {destination_id}"
                )
                return _refusal(
                    "clone_aborted_backend_soft_return",
                    f"Aborting the clone: creating Scenario {name!r} did not "
                    "create a copy — the backend returned an existing "
                    f"Scenario instead ({detail}). This is the pre-fix "
                    "duplicate-name behavior; the backend does not yet scope "
                    "Scenario name uniqueness per Project.",
                    "Deploy the okareo_server per-Project Scenario name "
                    "uniqueness fix, then re-run the same clone — Scenarios "
                    "copied so far will be skipped.",
                    category="server_error",
                    copied_before_abort=created_count,
                    scenario=name,
                )

            item["status"] = "created"
            item["destination_id"] = str(_get(response, "scenario_id", ""))
            created_count += 1

            response_type = _type_of(response)
            if item["tags"] or response_type != item["generation_type"]:
                try:
                    _repair(
                        okareo,
                        item["destination_id"],
                        destination_id,
                        item["tags"],
                        item["generation_type"],
                    )
                except Exception as e:
                    item["error"] = f"tag/type repair failed: {e}"

        # ---- Verify: re-read every copy and compare (research R7) --------
        try:
            post_by_name = {
                str(_get(s, "name", "")): s
                for s in _list_scenarios(okareo, destination_id)
            }
        except Exception:
            post_by_name = {}

        fidelity_gaps: list[str] = []
        for item in work:
            if item.get("status") == "failed":
                item["fidelity"] = "not verified (copy failed)"
                continue
            mismatches: list[str] = []
            post = post_by_name.get(item["name"])
            if post is None:
                mismatches.append("not found in the destination listing")
            else:
                post_type = _type_of(post)
                if post_type != item["generation_type"]:
                    mismatches.append(
                        f"generation_type: source {item['generation_type']!r}, "
                        f"copy {post_type!r}"
                    )
                post_tags = _tags(_get(post, "tags", []))
                if sorted(post_tags) != sorted(item["tags"]):
                    mismatches.append(
                        f"tags: source {sorted(item['tags'])!r}, "
                        f"copy {sorted(post_tags)!r}"
                    )
                try:
                    source_rows = item["_rows"]
                    if source_rows is None:
                        source_rows = list(
                            okareo.get_scenario_data_points(item["_source_id"])
                        )
                        item["row_count"] = len(source_rows)
                    copy_rows = list(
                        okareo.get_scenario_data_points(item["destination_id"])
                    )
                except Exception as e:
                    mismatches.append(f"verification read failed: {e}")
                else:
                    if len(copy_rows) != len(source_rows):
                        mismatches.append(
                            f"row count: source {len(source_rows)}, "
                            f"copy {len(copy_rows)}"
                        )
                    else:
                        for i, (src_dp, dst_dp) in enumerate(
                            zip(source_rows, copy_rows)
                        ):
                            if _canon(_get(src_dp, "input_")) != _canon(
                                _get(dst_dp, "input_")
                            ):
                                mismatches.append(f"rows[{i}].input differs")
                            if _canon(_get(src_dp, "result")) != _canon(
                                _get(dst_dp, "result")
                            ):
                                mismatches.append(f"rows[{i}].result differs")
                    for i, src_dp in enumerate(source_rows):
                        if _has_metadata(src_dp):
                            mismatches.append(
                                f"rows[{i}].meta_data present on the source "
                                "Scenario; the create API has no metadata "
                                "write path"
                            )
            if mismatches:
                item["fidelity"] = mismatches
                fidelity_gaps.extend(
                    f"{item['name']}: {m}" for m in mismatches
                )
            else:
                item["fidelity"] = "verified"

        # ---- Report -------------------------------------------------------
        skipped_count = sum(
            1 for i in work if i.get("status") == "skipped_existing"
        )
        failed_count = sum(1 for i in work if i.get("status") == "failed")
        status = (
            "completed"
            if failed_count == 0 and not fidelity_gaps
            else "completed_with_failures"
        )
        scenarios_report = [
            {
                "name": i["name"],
                "status": i.get("status"),
                "destination_id": i.get("destination_id"),
                "row_count": i["row_count"],
                "tags": i["tags"],
                "generation_type": i["generation_type"],
                "fidelity": i.get("fidelity"),
                "error": i.get("error"),
            }
            for i in work
        ]
        return json.dumps({
            "dry_run": False,
            "source_project": source_block,
            "destination_project": {
                "id": destination_id,
                "name": destination_name,
                "created": destination_created,
                # Stamped at create time only; a resumed destination keeps
                # whatever tags it already has — never re-tagged, never
                # duplicated.
                "provenance_tag": provenance_tag if destination_created else None,
            },
            "scenario_count": len(work),
            "row_count": sum(i["row_count"] for i in work),
            "created": created_count,
            "skipped": skipped_count,
            "failed": failed_count,
            "scenarios": scenarios_report,
            "fidelity_gaps": fidelity_gaps,
            "status": status,
            "next_step": (
                f"Register the account's Target in Project "
                f"{destination_name!r} (create_or_update_target with "
                f'project="{destination_name}"), select_project to keep '
                "working there, then run the first simulation."
            ),
        }, default=str)
