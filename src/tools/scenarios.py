"""Scenario management tools for the Okareo MCP server.

Provides six MCP tools for the full scenario lifecycle:

- save_scenario: Create a named scenario from rows of input/result data (idempotent)
- list_scenarios: Browse all scenarios in the project
- get_scenario: Read a scenario's metadata and data rows
- create_scenario_version: Create a new version of an existing scenario
- preview_delete_scenario: Preview what will be deleted before removing a scenario
- delete_scenario: Permanently delete a scenario and its related data
"""

import json
import os
import re
from typing import Optional

from okareo_api_client.errors import UnexpectedStatus

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from src.error_handling import format_tool_error
from src.okareo_client import find_test_runs, get_okareo_client, resolve_project_id


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


def register_tools(mcp: FastMCP) -> None:
    """Register all scenario tools with the FastMCP server."""

    @mcp.tool(
        title="Save Scenario",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def save_scenario(
        name: str,
        file_path: Optional[str] = None,
        rows: Optional[list[dict]] = None,
        tags: Optional[list[str]] = None,
    ) -> str:
        """Save a named scenario for use in quality tests.

        Provide EITHER file_path (preferred for large datasets) OR rows.
        When file_path is provided, the file is uploaded directly to Okareo
        without passing through the LLM context — use this for .jsonl files.

        If a scenario with the same name already exists, the existing scenario
        is returned (idempotent). Scenarios are immutable after creation — use
        create_scenario_version to create updated versions.

        Args:
            name: A unique name for the scenario.
            file_path: Path to a .jsonl file containing scenario rows. Each line
                should be a JSON object with 'input' and 'result' fields. Preferred
                for large datasets to avoid context window limits.
            rows: List of data rows, each with 'input' (any type) and 'result'
                (any type). Use for small scenarios (< 20 rows).
            tags: Optional list of tags for categorizing the scenario.
        """
        from okareo_api_client.api.default import (
            get_scenario_sets_v0_scenario_sets_get,
        )

        if not file_path and not rows:
            return json.dumps({
                "error": "Provide either file_path or rows. "
                "Use file_path for .jsonl files (preferred for large datasets).",
            })

        if file_path and rows:
            return json.dumps({
                "error": "Provide either file_path or rows, not both.",
            })

        if file_path and not os.path.isfile(file_path):
            return json.dumps({
                "error": f"File not found: {file_path}",
            })

        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        # Check for existing scenario with same name (idempotent create)
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
                        return json.dumps({
                            "name": name,
                            "id": str(_get_attr(s, "scenario_id", "")),
                            "project_id": str(_get_attr(s, "project_id", "")),
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

        # Create new scenario
        try:
            if file_path:
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

        return json.dumps({
            "name": _get_attr(result, "name", name),
            "id": str(_get_attr(result, "scenario_id", "")),
            "project_id": str(_get_attr(result, "project_id", project_id)),
            "tags": result_tags,
            "row_count": file_row_count if file_path else len(rows),
            "created_date": str(_get_attr(result, "time_created", "")),
            "created": True,
        }, default=str)

    @mcp.tool(
        title="List Scenarios",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def list_scenarios(limit: int = 20) -> str:
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
            project_id = resolve_project_id(okareo)
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
    def get_scenario(
        name: Optional[str] = None,
        scenario_id: Optional[str] = None,
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
            project_id = resolve_project_id(okareo)
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
    def create_scenario_version(base_name: str, rows: list[dict]) -> str:
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
            project_id = resolve_project_id(okareo)
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
    def preview_delete_scenario(
        name: Optional[str] = None,
        scenario_id: Optional[str] = None,
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
            project_id = resolve_project_id(okareo)
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
    def delete_scenario(scenario_id: str, name: str) -> str:
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

    return None
