"""Scenario management tools for the Okareo MCP server.

Provides seven MCP tools for the full scenario lifecycle:

- save_scenario: Create a named scenario from rows of input/result data (idempotent)
- list_scenarios: Browse all scenarios in the project
- get_scenario: Read a scenario's metadata and data rows
- create_scenario_version: Create a new version of an existing scenario
- preview_delete_scenario: Preview what will be deleted before removing a scenario
- delete_scenario: Permanently delete a scenario and its related data
- move_scenario: Move a scenario and everything under it to another project
  (dry-run-first; 038-scenario-move)
"""

import json
import os
import re
import uuid
from typing import Annotated, Optional

import httpx

from pydantic import Field

from okareo_api_client.errors import UnexpectedStatus

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from src.analytics_context import annotate
from src.error_handling import format_tool_error
from src.okareo_client import (
    find_test_runs,
    PROJECT_PARAM_DESC,
    get_okareo_client,
    okareo_api_request,
    project_resolution_scope,
    project_scoped,
    resolve_project,
)

# FR-012: the co-pilot ingests datasets below this row count inline; larger
# datasets are routed to manual upload to avoid unnecessary token cost.
SCENARIO_ROW_THRESHOLD = 2000
# FR-011: defensive guard on the inline `content` payload size (bytes).
MAX_INLINE_BYTES = 4 * 1024 * 1024

_MANUAL_UPLOAD_GUIDANCE = (
    "Save it locally and upload it directly to Okareo via the web app, SDK, or "
    "CLI, rather than passing it through the assistant (which incurs unnecessary "
    "token cost)."
)


def is_http_mode() -> bool:
    """True in multi-tenant streamable-http mode, where the server has no access
    to the caller's local filesystem (mirrors the TRANSPORT check in server.py)."""
    return os.environ.get("TRANSPORT", "stdio") == "streamable-http"


def _parse_jsonl_content(content: str) -> "tuple[list[dict], Optional[str]]":
    """Parse JSONL text into row dicts. Returns (rows, error_message).

    Blank/whitespace-only lines are skipped. Every non-blank line must be a JSON
    object. On any invalid line or an empty dataset, returns ([], message).
    """
    rows: list[dict] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return [], f"Dataset contains invalid rows: line {lineno} is not valid JSON."
        if not isinstance(obj, dict):
            return [], f"Dataset contains invalid rows: line {lineno} is not a JSON object."
        rows.append(obj)
    if not rows:
        return [], "No rows found in the dataset."
    return rows, None


def _upload_scenario_from_bytes(okareo, name: str, data: bytes, project_id):
    """Upload JSONL bytes as a scenario set without touching the server disk.

    Mirrors Okareo.upload_scenario_set but streams from an in-memory buffer, so
    the hosted server never needs access to the caller's local file (031).
    """
    from io import BytesIO
    from uuid import UUID

    from okareo_api_client.api.default import (
        scenario_sets_upload_v0_scenario_sets_upload_post,
    )
    from okareo_api_client.models.body_scenario_sets_upload_v0_scenario_sets_upload_post import (  # noqa: E501
        BodyScenarioSetsUploadV0ScenarioSetsUploadPost,
    )
    from okareo_api_client.types import File

    body = BodyScenarioSetsUploadV0ScenarioSetsUploadPost(
        name=name,
        project_id=UUID(project_id) if isinstance(project_id, str) else project_id,
        file=File(file_name=f"{name}.jsonl", payload=BytesIO(data)),
    )
    response = scenario_sets_upload_v0_scenario_sets_upload_post.sync(
        client=okareo.client,
        api_key=okareo.api_key,
        body=body,
    )
    okareo.validate_response(response)
    return response


def _get_attr(obj, attr, default=None):
    """Get an attribute, returning default if Unset."""
    val = getattr(obj, attr, default)
    if type(val).__name__ == "Unset":
        return default
    return val


def _serialize_value(val):
    """Serialize a value that may be Unset, a complex object, or a primitive."""
    if val is None:
        return None
    if type(val).__name__ == "Unset":
        return None
    if hasattr(val, "additional_properties"):
        return dict(val.additional_properties)
    if hasattr(val, "to_dict"):
        return val.to_dict()
    if isinstance(val, (dict, list, str, int, float, bool)):
        return val
    return str(val)


def _save_scenario_impl(
    name: str,
    content: Optional[str] = None,
    file_path: Optional[str] = None,
    rows: Optional[list[dict]] = None,
    tags: Optional[list[str]] = None,
    project: Optional[str] = None,
) -> str:
    """Shared implementation behind the per-mode save_scenario registrations."""
    from okareo_api_client.api.default import (
        get_scenario_sets_v0_scenario_sets_get,
    )

    # FR-006: exactly one dataset source.
    sources = [n for n, v in (("content", content), ("file_path", file_path), ("rows", rows)) if v]
    if len(sources) == 0:
        # FR-014: on the hosted server an unknown file_path argument is dropped
        # by schema validation before reaching this function, so a zero-source
        # call is how an old client's file_path attempt lands here.
        if is_http_mode():
            return json.dumps({
                "error": "Provide exactly one dataset source: content (raw "
                "JSONL text, for datasets under 2,000 rows) or rows (small "
                "inline datasets). File paths are not supported on the hosted "
                "server — it cannot read your local files. Read the file and "
                "pass its text as content (under 2,000 rows); for 2,000 rows "
                f"or more: {_MANUAL_UPLOAD_GUIDANCE}",
            })
        return json.dumps({
            "error": "Provide exactly one dataset source: content (raw JSONL "
            "text, preferred for < 2,000 rows), file_path (local .jsonl, any "
            "size), or rows (small inline datasets).",
        })
    if len(sources) > 1:
        valid = "content or rows" if is_http_mode() else "content, file_path, or rows"
        return json.dumps({
            "error": f"Provide only one of {valid} (got: {', '.join(sources)}).",
        })

    input_source = sources[0]

    # FR-007/FR-010: file_path cannot work on the hosted server.
    if file_path and is_http_mode():
        return json.dumps({
            "error": "file_path is not available on the hosted server (it has "
            "no access to your local files). For a dataset under 2,000 rows, "
            "read the file and pass its contents as `content`. For 2,000 rows "
            f"or more: {_MANUAL_UPLOAD_GUIDANCE}",
        })

    if file_path and not os.path.isfile(file_path):
        return json.dumps({
            "error": f"File not found: {file_path}",
        })

    # FR-003/007/008/011/012: validate `content` fully before any upload.
    content_rows: Optional[list[dict]] = None
    if content:
        if len(content.encode("utf-8")) > MAX_INLINE_BYTES:
            return json.dumps({
                "error": "Dataset exceeds the inline size limit "
                f"({MAX_INLINE_BYTES // (1024 * 1024)} MB). {_MANUAL_UPLOAD_GUIDANCE}",
            })
        parsed, parse_error = _parse_jsonl_content(content)
        if parse_error:
            return json.dumps({"error": parse_error})
        if len(parsed) >= SCENARIO_ROW_THRESHOLD:
            return json.dumps({
                "error": f"This dataset has {len(parsed)} rows "
                f"(>= {SCENARIO_ROW_THRESHOLD}). {_MANUAL_UPLOAD_GUIDANCE}",
            })
        content_rows = parsed

    try:
        okareo = get_okareo_client()
        project_id = resolve_project(okareo, project).id
    except Exception as e:
        return format_tool_error(e)

    # Check for existing scenario with same name (idempotent create).
    # The listing is project-filtered, so "same name" means "same name in this
    # project" — which is what FR-001a requires and what the backend's own
    # collision rules assume. This is a short-circuit for the caller's
    # convenience, not an alternative collision rule (FR-001c).
    try:
        scenarios = get_scenario_sets_v0_scenario_sets_get.sync(
            client=okareo.client,
            project_id=project_id,
            api_key=okareo.api_key,
        )
        if scenarios and not isinstance(scenarios, Exception):
            for s in scenarios:
                if _get_attr(s, "name") == name:
                    row_count = _get_attr(s, "scenario_count", 0)
                    scenario_id = str(_get_attr(s, "scenario_id", ""))
                    existing_project_id = str(_get_attr(s, "project_id", ""))
                    annotate(
                        project_id=existing_project_id or project_id,
                        entity_type="scenario",
                        entity_id=scenario_id,
                        row_count=row_count,
                        input_source=input_source,
                    )
                    return json.dumps({
                        "name": name,
                        "id": scenario_id,
                        "project_id": existing_project_id,
                        "tags": _get_attr(s, "tags", []) or [],
                        "row_count": row_count,
                        "created_date": str(_get_attr(s, "time_created", "")),
                        "created": False,
                        "message": f"Scenario '{name}' already exists with {row_count} rows.",
                    }, default=str)
    except Exception as e:
        return format_tool_error(e)

    # Count rows in JSONL file for accurate row_count in response
    file_row_count = 0
    if file_path:
        with open(file_path) as f:
            file_row_count = sum(1 for line in f if line.strip())

    # Determine the row_count reported to the caller (FR-003).
    if content_rows is not None:
        row_count = len(content_rows)
    elif file_path:
        row_count = file_row_count
    else:
        row_count = len(rows)

    # Create new scenario
    try:
        if content_rows is not None:
            result = _upload_scenario_from_bytes(
                okareo, name, content.encode("utf-8"), project_id
            )
        elif file_path:
            result = okareo.upload_scenario_set(
                name,
                file_path=file_path,
            )
        else:
            from okareo_api_client.models.scenario_set_create import ScenarioSetCreate
            from okareo_api_client.models.seed_data import SeedData

            seed_data = [
                SeedData(input_=row.get("input"), result=row.get("result"))
                for row in rows
            ]
            scenario_set = ScenarioSetCreate(
                name=name,
                seed_data=seed_data,
                project_id=project_id,
            )
            result = okareo.create_scenario_set(scenario_set)
    except Exception as e:
        return format_tool_error(e)

    # Set tags if provided (SDK ScenarioSetCreate doesn't support tags)
    result_tags = []
    if tags:
        try:
            from okareo_api_client.api.default import (
                update_scenario_set_v0_scenario_sets_scenario_id_put,
            )
            from okareo_api_client.models.scenario_set_update import ScenarioSetUpdate

            update_body = ScenarioSetUpdate(tags=tags)
            update_scenario_set_v0_scenario_sets_scenario_id_put.sync(
                scenario_id=_get_attr(result, "scenario_id"),
                client=okareo.client,
                body=update_body,
                api_key=okareo.api_key,
            )
            result_tags = tags
        except Exception:
            pass  # Tags update is best-effort; don't fail the create

    scenario_id = str(_get_attr(result, "scenario_id", ""))
    result_project_id = str(_get_attr(result, "project_id", project_id))
    annotate(
        project_id=result_project_id,
        entity_type="scenario",
        entity_id=scenario_id,
        row_count=row_count,
        input_source=input_source,
    )
    return json.dumps({
        "name": _get_attr(result, "name", name),
        "id": scenario_id,
        "project_id": result_project_id,
        "tags": result_tags,
        "row_count": row_count,
        "created_date": str(_get_attr(result, "time_created", "")),
        "created": True,
    }, default=str)


def register_tools(mcp: FastMCP) -> None:
    """Register all scenario tools with the FastMCP server."""

    # FR-014: copilots attempt any parameter the schema advertises, so the
    # hosted registration must not carry `file_path` at all (the hosted server
    # cannot read the caller's disk). TRANSPORT is fixed for the process
    # lifetime, so branching at registration is safe.
    if is_http_mode():

        @mcp.tool(
            title="Save Scenario",
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        @project_scoped
        def save_scenario(
            name: str,
            content: Optional[str] = None,
            rows: Optional[list[dict]] = None,
            tags: Optional[list[str]] = None,
            project: Annotated[Optional[str], Field(description=PROJECT_PARAM_DESC)] = None,
        ) -> str:
            """Save a named scenario for use in quality tests.

            Provide EXACTLY ONE dataset source: `content` or `rows`. Feed the
            scenario rows directly — the server cannot read files from your
            machine.

            Choose by size, to avoid unnecessary token cost:
            - Datasets UNDER 2,000 rows: read the .jsonl file yourself and pass
              its text as `content` (preferred), or pass `rows` for tiny
              datasets.
            - Datasets of 2,000 ROWS OR MORE: do NOT read the file into
              context. Instead, tell the user to save it locally and upload it
              directly to Okareo via the web app, SDK, or CLI. Passing a large
              dataset through the assistant wastes tokens and is rejected.

            If a scenario with the same name already exists, the existing
            scenario is returned (idempotent). Scenarios are immutable after
            creation — use create_scenario_version to create updated versions.

            Args:
                name: A unique name for the scenario.
                content: Raw JSONL text (one JSON object with 'input' and
                    'result' per line). Preferred for datasets under 2,000
                    rows. For 2,000+ rows, upload manually instead (see above).
                rows: List of data rows, each with 'input' (any type) and
                    'result' (any type). Use for small scenarios (< 20 rows).
                tags: Optional list of tags for categorizing the scenario.
            """
            return _save_scenario_impl(
                name, content=content, rows=rows, tags=tags, project=project
            )

    else:

        @mcp.tool(
            title="Save Scenario",
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        @project_scoped
        def save_scenario(
            name: str,
            content: Optional[str] = None,
            file_path: Optional[str] = None,
            rows: Optional[list[dict]] = None,
            tags: Optional[list[str]] = None,
            project: Annotated[Optional[str], Field(description=PROJECT_PARAM_DESC)] = None,
        ) -> str:
            """Save a named scenario for use in quality tests.

            Provide EXACTLY ONE dataset source: `content`, `file_path`, or
            `rows`.

            Prefer `file_path` for local .jsonl files — the server reads the
            file directly, so no rows pass through the assistant's context.
            When passing rows through the assistant instead, keep the dataset
            UNDER 2,000 rows (`content` with the file's text, or `rows` for
            tiny datasets). For 2,000 rows or more, always use `file_path` or
            upload directly to Okareo via the web app, SDK, or CLI, to avoid
            unnecessary token cost.

            If a scenario with the same name already exists, the existing
            scenario is returned (idempotent). Scenarios are immutable after
            creation — use create_scenario_version to create updated versions.

            Args:
                name: A unique name for the scenario.
                content: Raw JSONL text (one JSON object with 'input' and
                    'result' per line). Only for datasets under 2,000 rows.
                file_path: Path to a local .jsonl file. Preferred — works for
                    any size.
                rows: List of data rows, each with 'input' (any type) and
                    'result' (any type). Use for small scenarios (< 20 rows).
                tags: Optional list of tags for categorizing the scenario.
            """
            return _save_scenario_impl(
                name,
                content=content,
                file_path=file_path,
                rows=rows,
                tags=tags,
                project=project,
            )

    @mcp.tool(
        title="List Scenarios",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @project_scoped
    def list_scenarios(limit: int = 20, project: Annotated[Optional[str], Field(description=PROJECT_PARAM_DESC)] = None) -> str:
        """List scenarios in the project, most recent first.

        Returns scenario names, IDs, tags, row counts, and creation dates.
        Use this to discover existing scenarios before running a test.

        Args:
            limit: Maximum number of scenarios to return (default 20).
                Set to 0 to return all scenarios.
        """
        from okareo_api_client.api.default import (
            get_scenario_sets_v0_scenario_sets_get,
        )

        try:
            okareo = get_okareo_client()
            project_id = resolve_project(okareo, project).id
        except Exception as e:
            return format_tool_error(e)

        try:
            scenarios = get_scenario_sets_v0_scenario_sets_get.sync(
                client=okareo.client,
                project_id=project_id,
                api_key=okareo.api_key,
            )
        except Exception as e:
            return format_tool_error(e)

        if not scenarios or isinstance(scenarios, Exception):
            return json.dumps({
                "scenarios": [],
                "count": 0,
                "message": "No scenarios found in project.",
            })

        result = []
        for s in scenarios:
            result.append({
                "name": _get_attr(s, "name", ""),
                "id": str(_get_attr(s, "scenario_id", "")),
                "project_id": str(_get_attr(s, "project_id", "")),
                "tags": _get_attr(s, "tags", []) or [],
                "row_count": _get_attr(s, "scenario_count", 0),
                "created_date": str(_get_attr(s, "time_created", "")),
            })

        # Sort by created_date descending (most recent first)
        result.sort(key=lambda x: x["created_date"], reverse=True)

        # Apply limit
        if limit > 0:
            result = result[:limit]

        return json.dumps({"scenarios": result, "count": len(result)}, default=str)

    @mcp.tool(
        title="Get Scenario",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @project_scoped
    def get_scenario(
        name: Optional[str] = None,
        scenario_id: Optional[str] = None,
        project: Annotated[Optional[str], Field(description=PROJECT_PARAM_DESC)] = None,
    ) -> str:
        """Read a scenario's metadata and all data rows.

        Look up by name or scenario ID. Returns scenario details and all
        input/result data rows.

        Args:
            name: Name of the scenario to retrieve.
            scenario_id: ID of the scenario to retrieve. Takes precedence over name.
        """
        from okareo_api_client.api.default import (
            get_scenario_sets_v0_scenario_sets_get,
        )

        if not name and not scenario_id:
            return json.dumps({"error": "Provide either name or scenario_id."})

        try:
            okareo = get_okareo_client()
            project_id = resolve_project(okareo, project).id
        except Exception as e:
            return format_tool_error(e)

        resolved_id = scenario_id
        scenario_meta = None

        # Resolve name to ID if needed
        if not scenario_id and name:
            try:
                scenarios = get_scenario_sets_v0_scenario_sets_get.sync(
                    client=okareo.client,
                    project_id=project_id,
                    api_key=okareo.api_key,
                )
                if scenarios and not isinstance(scenarios, Exception):
                    for s in scenarios:
                        if _get_attr(s, "name") == name:
                            resolved_id = _get_attr(s, "scenario_id")
                            scenario_meta = {
                                "name": _get_attr(s, "name", ""),
                                "scenario_id": _get_attr(s, "scenario_id", ""),
                                "scenario_count": _get_attr(s, "scenario_count", 0),
                                "time_created": str(_get_attr(s, "time_created", "")),
                                "app_link": _get_attr(s, "app_link", ""),
                            }
                            break
            except Exception as e:
                return format_tool_error(e)

            if resolved_id is None:
                return json.dumps({
                    "error": f"Scenario '{name}' not found. "
                    "Use list_scenarios to see available scenarios.",
                })

        # Get data points
        try:
            data_points = okareo.get_scenario_data_points(resolved_id)
        except Exception as e:
            return format_tool_error(e)

        rows = []
        if isinstance(data_points, list):
            for dp in data_points:
                rows.append({
                    "input": _serialize_value(_get_attr(dp, "input_")),
                    "result": _serialize_value(_get_attr(dp, "result")),
                })

        if scenario_meta is None:
            scenario_meta = {
                "scenario_id": resolved_id,
                "name": name or "",
            }

        response = {
            **scenario_meta,
            "rows": rows,
            "row_count": len(rows),
        }

        return json.dumps(response, default=str)

    @mcp.tool(
        title="Create Scenario Version",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @project_scoped
    def create_scenario_version(base_name: str, rows: list[dict], project: Annotated[Optional[str], Field(description=PROJECT_PARAM_DESC)] = None) -> str:
        """Create a new version of an existing scenario with updated data.

        Automatically determines the next version number (e.g., 'my-test-v2',
        'my-test-v3'). The original scenario is treated as version 1.

        Args:
            base_name: Name of the original scenario to create a version of.
            rows: List of data rows for the new version, each with 'input' and 'result'.
        """
        from okareo_api_client.api.default import (
            get_scenario_sets_v0_scenario_sets_get,
        )
        from okareo_api_client.models.scenario_set_create import ScenarioSetCreate
        from okareo_api_client.models.seed_data import SeedData

        if not rows:
            return json.dumps({"error": "At least one row is required."})

        try:
            okareo = get_okareo_client()
            project_id = resolve_project(okareo, project).id
        except Exception as e:
            return format_tool_error(e)

        # Strip existing version suffix if present
        version_match = re.match(r"^(.+)-v(\d+)$", base_name)
        if version_match:
            base_name = version_match.group(1)

        # Scan existing versions
        try:
            scenarios = get_scenario_sets_v0_scenario_sets_get.sync(
                client=okareo.client,
                project_id=project_id,
                api_key=okareo.api_key,
            )
        except Exception as e:
            return format_tool_error(e)

        max_version = 1  # Original is implicitly v1
        pattern = re.compile(rf"^{re.escape(base_name)}-v(\d+)$")
        if scenarios and not isinstance(scenarios, Exception):
            for s in scenarios:
                s_name = _get_attr(s, "name", "")
                m = pattern.match(s_name)
                if m:
                    max_version = max(max_version, int(m.group(1)))

        next_version = max_version + 1
        version_name = f"{base_name}-v{next_version}"

        # Create the versioned scenario
        try:
            seed_data = [
                SeedData(input_=row.get("input"), result=row.get("result"))
                for row in rows
            ]
            scenario_set = ScenarioSetCreate(
                name=version_name,
                seed_data=seed_data,
                project_id=project_id,
            )
            result = okareo.create_scenario_set(scenario_set)
        except Exception as e:
            return format_tool_error(e)

        return json.dumps({
            "scenario_id": _get_attr(result, "scenario_id", ""),
            "name": _get_attr(result, "name", version_name),
            "version": next_version,
            "base_name": base_name,
            "row_count": len(rows),
            "app_link": _get_attr(result, "app_link", ""),
        }, default=str)

    @mcp.tool(
        title="Preview Scenario Deletion",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @project_scoped
    def preview_delete_scenario(
        name: Optional[str] = None,
        scenario_id: Optional[str] = None,
        project: Annotated[Optional[str], Field(description=PROJECT_PARAM_DESC)] = None,
    ) -> str:
        """Preview what will be deleted before removing a scenario.

        Shows the scenario details and count of related test runs that will
        also be deleted. Use delete_scenario to confirm deletion after reviewing.

        Args:
            name: Name of the scenario to preview deletion for.
            scenario_id: ID of the scenario. Takes precedence over name.
        """
        from okareo_api_client.api.default import (
            get_scenario_sets_v0_scenario_sets_get,
        )
        from okareo_api_client.models.general_find_payload import GeneralFindPayload

        if not name and not scenario_id:
            return json.dumps({"error": "Provide either name or scenario_id."})

        try:
            okareo = get_okareo_client()
            project_id = resolve_project(okareo, project).id
        except Exception as e:
            return format_tool_error(e)

        resolved_id = scenario_id
        scenario_name = name

        # Resolve name to ID if needed
        if not scenario_id:
            try:
                scenarios = get_scenario_sets_v0_scenario_sets_get.sync(
                    client=okareo.client,
                    project_id=project_id,
                    api_key=okareo.api_key,
                )
                if scenarios and not isinstance(scenarios, Exception):
                    for s in scenarios:
                        if _get_attr(s, "name") == name:
                            resolved_id = _get_attr(s, "scenario_id")
                            scenario_name = _get_attr(s, "name")
                            break
            except Exception as e:
                return format_tool_error(e)

            if resolved_id is None:
                return json.dumps({
                    "error": f"Scenario '{name}' not found. "
                    "Use list_scenarios to see available scenarios.",
                })

        # Count related test runs
        related_test_run_count = 0
        try:
            payload = GeneralFindPayload(
                scenario_set_id=resolved_id,
                project_id=project_id,
            )
            try:
                runs = find_test_runs(okareo, payload)
            except UnexpectedStatus as ue:
                runs = json.loads(ue.content) if ue.status_code == 200 else None
            if runs and not isinstance(runs, Exception):
                related_test_run_count = len(runs)
        except Exception:
            pass  # Non-critical — proceed with count of 0

        message = (
            f"Deleting '{scenario_name}' will also delete "
            f"{related_test_run_count} related test run(s)."
        )

        return json.dumps({
            "scenario_id": resolved_id,
            "scenario_name": scenario_name,
            "related_test_run_count": related_test_run_count,
            "message": message,
        }, default=str)

    @mcp.tool(
        title="Delete Scenario",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @project_scoped
    def delete_scenario(scenario_id: str, name: str, project: Annotated[Optional[str], Field(description=PROJECT_PARAM_DESC)] = None) -> str:
        """Permanently delete a scenario and all related test data.

        Both scenario_id and name are required. Use preview_delete_scenario first
        to see what will be deleted before confirming.

        Args:
            scenario_id: The ID of the scenario to delete (from preview_delete_scenario).
            name: The name of the scenario to delete.
        """
        from okareo_api_client.api.default import (
            delete_scenario_set_v0_scenario_sets_scenario_id_delete,
        )

        try:
            okareo = get_okareo_client()
            resolve_project(okareo, project)
        except Exception as e:
            return format_tool_error(e)

        # Validate scenario still exists
        try:
            okareo.get_scenario_data_points(scenario_id)
        except Exception:
            return json.dumps({
                "error": f"Scenario '{name}' not found or already deleted.",
            })

        # Delete
        try:
            delete_scenario_set_v0_scenario_sets_scenario_id_delete.sync(
                scenario_id=scenario_id,
                client=okareo.client,
                api_key=okareo.api_key,
                name=name,
            )
        except Exception as e:
            return format_tool_error(e)

        return json.dumps({
            "deleted": True,
            "scenario_id": scenario_id,
            "name": name,
            "message": (
                f"Scenario '{name}' and all related test data have been deleted."
            ),
        }, default=str)

    @mcp.tool(
        title="Move Scenario",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,  # relocates; deletes nothing
            idempotentHint=False,  # a repeat real move 400s (already there)
            openWorldHint=False,
        ),
    )
    def move_scenario(
        scenario: Annotated[
            str,
            Field(
                description=(
                    "The Scenario to move — its name (resolved within the source "
                    "project) or its id."
                )
            ),
        ],
        to_project: Annotated[
            str,
            Field(description="Destination project — name or id."),
        ],
        dry_run: bool = True,
        project: Annotated[Optional[str], Field(description=PROJECT_PARAM_DESC)] = None,
    ) -> str:
        """Move a Scenario — and everything under it — to another project.

        Moves are per Scenario, never per simulation: the Scenario, all of its
        simulations, their datapoints, check results, and traces move together.
        Targets never move — they are shared across the organization and stay
        usable from every project. Monitors, dashboards, and notification
        settings stay in the source project.

        DRY-RUN-FIRST PROTOCOL: dry_run defaults to true and the first call
        must keep it that way. Present the returned plan to the user — the
        per-table counts and any blockers — and only after
        the user explicitly confirms call this tool again with dry_run=false.
        Never execute a move the user has not confirmed against those counts.

        A blocked move (simulations still running, or a same-named Scenario in
        the destination) returns the server's structured refusal. Do not retry
        it unchanged: wait for the simulations to finish, or rename one of the
        Scenarios.

        Args:
            scenario: The Scenario to move — name (within the source project) or id.
            to_project: Destination project — name or id.
            dry_run: True (default) reports what would move without writing.
        """
        from okareo_api_client.api.default import (
            get_scenario_sets_v0_scenario_sets_get,
        )

        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        with project_resolution_scope():
            # Two projects, one confined scope (036 FR-018): the source by the
            # standard precedence, the destination always explicitly — so a
            # bad destination fails with the same available-projects error
            # shape as everywhere else, never a fallback.
            try:
                source = resolve_project(okareo, project)
                destination = resolve_project(okareo, to_project)
            except Exception as e:
                return format_tool_error(e)

            # Resolve the Scenario within the SOURCE project.
            scenario_id = None
            try:
                uuid.UUID(str(scenario))
                scenario_id = str(scenario)
            except ValueError:
                try:
                    rows = get_scenario_sets_v0_scenario_sets_get.sync(
                        client=okareo.client,
                        project_id=source.id,
                        api_key=okareo.api_key,
                    )
                except Exception as e:
                    return format_tool_error(e)
                for row in rows or []:
                    if _get_attr(row, "name") == scenario:
                        scenario_id = _get_attr(row, "scenario_id")
                        break
                if scenario_id is None:
                    return json.dumps({
                        "error": (
                            f"Scenario '{scenario}' not found in project "
                            f"'{source.name}'. Use list_scenarios to see "
                            "available scenarios."
                        ),
                    })

            try:
                plan = okareo_api_request(
                    okareo,
                    "post",
                    f"/v0/scenario_sets/{scenario_id}/move",
                    json={"destination_project_id": destination.id},
                    params={"dry_run": "true"} if dry_run else None,
                )
            except httpx.HTTPStatusError as e:
                return _move_refusal(e, source, destination)
            except Exception as e:
                return format_tool_error(e)

        result = {
            "executed": bool(plan.get("executed")) if isinstance(plan, dict) else False,
            "plan": plan,
            "source_project": source.as_dict(),
            "destination_project": destination.as_dict(),
        }
        if dry_run:
            result["next_step"] = (
                "This was a dry run — nothing moved. Present these counts and "
                "any blockers to the user, and call move_scenario again with "
                "dry_run=false only after they explicitly confirm."
            )
        return json.dumps(result, default=str)

    return None


def _move_refusal(error: httpx.HTTPStatusError, source, destination) -> str:
    """Map a non-2xx move response to a structured, never-retried payload."""
    status = error.response.status_code
    try:
        body = error.response.json()
    except Exception:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None

    if status == 409 and isinstance(detail, dict):
        return json.dumps({
            "blocked": True,
            "plan": detail,
            "source_project": source.as_dict(),
            "destination_project": destination.as_dict(),
            "message": (
                "The move is blocked; nothing moved. Options: wait for the "
                "running simulations to finish, or rename the colliding "
                "Scenario. Do not retry unchanged."
            ),
        }, default=str)
    if status == 503:
        return json.dumps({
            "error": (
                detail
                if isinstance(detail, str)
                else "The move timed out and was rolled back whole — nothing "
                "moved. Ask the user before retrying."
            ),
            "retryable": True,
        }, default=str)
    return json.dumps({
        "error": (
            detail if isinstance(detail, str) else f"Move failed with HTTP {status}."
        ),
    }, default=str)
