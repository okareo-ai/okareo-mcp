"""Project cloning tool (037-project-clone, thinned by 039-server-side-clone).

One tool, ``clone_project``, wrapping the backend's
``POST /v0/projects/{project_id}/clone`` (+ ``dry_run`` query param) via the
raw ``okareo_api_request`` helper — the same shape as ``move_scenario`` (038).
The server owns every clone rule: it copies each Scenario with all of its
rows in one transaction, stamps the ``Cloned from: <source>`` provenance tag,
and enforces the destination-name rules (collision, reserved 'Global'). The
037 client-side pipeline — the per-Scenario copy loop, tag/type repair,
verification re-reads, and resume semantics — is gone; see
specs/039-server-side-clone.

This remains the one sanctioned Project-creating tool (037 FR-006,
superseding 036 FR-025), but the creation itself now happens server-side
inside the clone transaction — no tool module calls the SDK's
``create_project`` any more. It is deliberately NOT ``@project_scoped``: the
response is about two Projects, so the source identity travels in the body
and the destination arrives in the server's plan.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from src.error_handling import format_tool_error
from src.okareo_client import (
    get_okareo_client,
    invalidate_projects_cache,
    okareo_api_request,
    project_resolution_scope,
    resolve_project,
)

_CONFIRM_DIRECTIVE = (
    "This was a dry run — nothing was cloned. Present these counts and any "
    "blockers to the user, and call clone_project again with dry_run=false "
    "only after they explicitly confirm."
)


def _refusal(code: str, message: str, suggestion: str) -> str:
    payload = {
        "category": "validation",
        "code": code,
        "message": message,
        "suggestion": suggestion,
    }
    return json.dumps({"error": payload})


def register_tools(mcp: FastMCP) -> None:
    """Register the clone tool on the given FastMCP server."""

    @mcp.tool(
        title="Clone Project",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,  # creates; deletes nothing
            idempotentHint=False,  # a repeat real clone 409s (name collision)
            openWorldHint=False,
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
                    "unique per organization (case-insensitive; archived "
                    "Projects still hold theirs) — the server refuses a "
                    "taken or reserved name."
                )
            ),
        ],
        dry_run: bool = True,
    ) -> str:
        """Clone a Project server-side: every Scenario, with all rows, into a new Project.

        One call to the backend's clone endpoint does the whole job in a
        single transaction: it creates the destination Project (tagged
        'Cloned from: <source>') and copies every source Scenario — same
        name, type, tags, and rows in the same order. Scenario data never
        enters this conversation. Runs, evaluations, dashboards, and
        monitors are NOT copied; Targets, Drivers, and Checks are shared
        across the organization and stay usable from the new Project.

        DRY-RUN-FIRST PROTOCOL: dry_run defaults to true and the first call
        must keep it that way. Present the returned plan to the user — the
        Scenario and row counts and any blockers — and only after the user
        explicitly confirms call this tool again with dry_run=false. Never
        execute a clone the user has not confirmed against those counts.

        The server owns every clone rule. A blocked clone (the destination
        name is already taken — the source's own name included — or is the
        reserved 'Global') returns the server's structured refusal. Do not
        retry it unchanged: choose a different name.

        Typical next steps after a clone: register the account's Target in
        the new Project (create_or_update_target with project set to it),
        select_project to keep working there, then run the first simulation.

        Args:
            source_project: The golden Project to clone (name or id).
            new_project_name: Name for the new destination Project.
            dry_run: True (default) reports what would be cloned without
                writing anything.
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

        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        with project_resolution_scope():
            try:
                source = resolve_project(okareo, str(source_project))
            except Exception as e:
                return format_tool_error(e)

            try:
                plan = okareo_api_request(
                    okareo,
                    "post",
                    f"/v0/projects/{source.id}/clone",
                    json={"new_project_name": str(new_project_name).strip()},
                    params={"dry_run": "true"} if dry_run else None,
                )
            except httpx.HTTPStatusError as e:
                return _clone_refusal(e, source)
            except Exception as e:
                return format_tool_error(e)

        executed = bool(plan.get("executed")) if isinstance(plan, dict) else False
        if executed:
            # The new Project must be visible to select_project and every
            # project-scoped tool immediately, not after the cache TTL
            # (037 CR-1, unchanged in 039).
            invalidate_projects_cache(okareo)

        result: dict[str, Any] = {
            "executed": executed,
            "plan": plan,
            "source_project": source.as_dict(),
        }
        if dry_run:
            result["next_step"] = _CONFIRM_DIRECTIVE
        return json.dumps(result, default=str)


def _clone_refusal(error: httpx.HTTPStatusError, source) -> str:
    """Map a non-2xx clone response to a structured, never-retried payload."""
    status = error.response.status_code
    try:
        body = error.response.json()
    except Exception:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None

    if status == 409 and isinstance(detail, dict):
        blocker_details = [
            str(b.get("detail"))
            for b in detail.get("blockers", [])
            if isinstance(b, dict) and b.get("detail")
        ]
        return json.dumps({
            "blocked": True,
            "plan": detail,
            "source_project": source.as_dict(),
            "message": (
                " ".join(blocker_details)
                if blocker_details
                else "The clone is blocked; nothing was created. "
                "Do not retry unchanged."
            ),
        }, default=str)
    if status == 503:
        return json.dumps({
            "error": (
                detail
                if isinstance(detail, str)
                else "The clone timed out and was rolled back whole — "
                "nothing was created. Ask the user before retrying."
            ),
            "retryable": True,
        }, default=str)
    return json.dumps({
        "error": (
            detail if isinstance(detail, str) else f"Clone failed with HTTP {status}."
        ),
    }, default=str)
