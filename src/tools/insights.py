"""Analytics and dashboard tools for the Okareo MCP server.

Lets a developer query Okareo's product analytics and manage dashboards from
the agent workflow. These call the Okareo API through ``okareo_api_request``
because the published ``okareo`` 0.0.132 SDK does not wrap the
``/v0/analytics/*`` or ``/v0/dashboards*`` endpoints (see
``specs/022-sdk-132-upgrade`` research R2).

Named ``insights`` to avoid colliding with ``src/analytics.py``, which is the
MCP's own product-telemetry module.
"""

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from src.error_handling import format_tool_error
from src.okareo_client import (
    get_okareo_client,
    okareo_api_request,
    resolve_project_id,
)

# Supported relative look-back windows (the backend ``TimeRange`` enum). Both
# dashboards and analytics queries accept these enum strings — NOT objects.
TIME_RANGES: tuple[str, ...] = (
    "LAST_HOUR",
    "LAST_24_HOURS",
    "LAST_7_DAYS",
    "LAST_14_DAYS",
    "LAST_30_DAYS",
    "LAST_90_DAYS",
)

# Analytics queries require a time window; default to this when the caller
# supplies neither ``time_range`` nor ``time_dimensions``.
DEFAULT_ANALYTICS_TIME_RANGE = "LAST_30_DAYS"


def _validate_time_range(time_range: Optional[str]) -> Optional[str]:
    """Return an error message if ``time_range`` is set but unsupported."""
    if time_range is not None and time_range not in TIME_RANGES:
        return (
            f"Unsupported time_range '{time_range}'. "
            f"Supported values: {', '.join(TIME_RANGES)}."
        )
    return None


def _dashboards_from_response(resp):
    """Extract the dashboard list from a ``GET /v0/dashboards`` response.

    The backend wraps the list in a ``DashboardListResponse`` (``{"items":
    [...]}``); a bare list is tolerated for forward/backward compatibility.
    """
    if isinstance(resp, dict):
        items = resp.get("items")
        return items if isinstance(items, list) else []
    if isinstance(resp, list):
        return resp
    return []


def _find_dashboard_by_name(dashboards, name: str):
    """Return the dashboard dict whose name matches, or None.

    When multiple dashboards share the name (legacy duplicates created before
    the upsert lookup was fixed), the most-recently-modified one is returned so
    the choice is deterministic; no automatic de-duplication is performed.
    """
    if not isinstance(dashboards, list):
        return None
    matches = [
        d for d in dashboards if isinstance(d, dict) and d.get("name") == name
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    return max(
        matches,
        key=lambda d: (
            d.get("time_modified") or d.get("time_created") or ""
        ),
    )


def register_tools(mcp: FastMCP) -> None:
    """Register analytics and dashboard tools with the FastMCP server."""

    @mcp.tool(
        title="Query Analytics",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def query_analytics(
        measures: list[str],
        dimensions: Optional[list[str]] = None,
        cube: Optional[str] = None,
        filters: Optional[list[dict]] = None,
        time_range: Optional[str] = None,
        time_dimensions: Optional[list[dict]] = None,
        include_metadata: bool = False,
    ) -> str:
        """Query Okareo's product analytics to understand evaluation trends.

        Answers questions like "how is my evaluation quality trending" by
        aggregating measures across dimensions over a time window.

        Args:
            measures: Metrics to aggregate. Required. For the ``check_trend``
                cube: avg_check_value, issue_rate, error_rate, datapoint_count,
                issue_count, error_count, test_run_count, avg_latency, sum_cost,
                input_token_count, output_token_count.
            dimensions: Optional group-by fields (e.g. ["check.name"],
                ["target.name"], ["provider"]).
            cube: Optional analytics cube name (defaults to ``check_trend``,
                currently the only cube).
            filters: Optional list of filter objects
                ``{"member": ..., "operator": ..., "values": [...]}``.
            time_range: Optional look-back window — one of LAST_HOUR,
                LAST_24_HOURS, LAST_7_DAYS, LAST_14_DAYS, LAST_30_DAYS,
                LAST_90_DAYS. If neither time_range nor time_dimensions is
                given, defaults to LAST_30_DAYS (the analytics API requires a
                time window).
            time_dimensions: Optional time bucketing — a list with at most one
                entry, e.g. [{"dimension": "test_run.start_time",
                "granularity": "day"}] (granularity: hour, day, or week).
            include_metadata: When true, also return the available cubes,
                dimensions, and measures so the query can be refined.
        """
        if not measures or not isinstance(measures, list):
            return json.dumps({"error": "measures must be a non-empty list."})

        tr_error = _validate_time_range(time_range)
        if tr_error:
            return json.dumps({"error": tr_error})

        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        metadata = None
        if include_metadata:
            try:
                metadata = okareo_api_request(
                    okareo, "get", "/v0/analytics/meta",
                    params={"project_id": project_id},
                )
            except Exception as e:
                return format_tool_error(e)

        # The analytics API requires exactly one of time_range or a single
        # time_dimension; default to a sensible window when the caller gives
        # neither so "how is my quality trending" works without manual setup.
        if time_range is None and not time_dimensions:
            time_range = DEFAULT_ANALYTICS_TIME_RANGE

        body: dict = {"project_id": str(project_id), "measures": measures}
        if dimensions:
            body["dimensions"] = dimensions
        if cube:
            body["cube"] = cube
        if filters:
            body["filters"] = filters
        if time_range:
            body["time_range"] = time_range
        if time_dimensions:
            body["time_dimensions"] = time_dimensions

        try:
            result = okareo_api_request(
                okareo, "post", "/v0/analytics/query", json=body
            )
        except Exception as e:
            return format_tool_error(e)

        payload: dict = {"result": result}
        if metadata is not None:
            payload["metadata"] = metadata
        return json.dumps(payload, default=str)

    @mcp.tool(
        title="List Dashboards",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def list_dashboards(limit: int = 20) -> str:
        """List the analytics dashboards in your Okareo project.

        Args:
            limit: Maximum number of dashboards to return (default 20). Use 0
                for no limit.
        """
        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        try:
            dashboards = okareo_api_request(
                okareo, "get", "/v0/dashboards",
                params={"project_id": project_id},
            )
        except Exception as e:
            return format_tool_error(e)

        dashboards = _dashboards_from_response(dashboards)
        total = len(dashboards)
        if limit and limit > 0:
            dashboards = dashboards[:limit]
        return json.dumps({
            "dashboards": dashboards,
            "count": len(dashboards),
            "total": total,
        }, default=str)

    @mcp.tool(
        title="Get Dashboard",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def get_dashboard(name: str) -> str:
        """Retrieve a dashboard's full configuration by name.

        Args:
            name: Name of the dashboard to retrieve.
        """
        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        try:
            dashboards = okareo_api_request(
                okareo, "get", "/v0/dashboards",
                params={"project_id": project_id},
            )
        except Exception as e:
            return format_tool_error(e)

        match = _find_dashboard_by_name(
            _dashboards_from_response(dashboards), name
        )
        if match is None:
            return json.dumps({
                "error": f"Dashboard '{name}' not found. "
                "Use list_dashboards to see available dashboards.",
            })

        dashboard_id = match.get("id")
        try:
            detail = okareo_api_request(
                okareo, "get", f"/v0/dashboards/{dashboard_id}",
                params={"project_id": project_id},
            )
        except Exception as e:
            return format_tool_error(e)
        return json.dumps({"dashboard": detail or match}, default=str)

    @mcp.tool(
        title="Save Dashboard",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def save_dashboard(
        name: str,
        panels: Optional[list[dict]] = None,
        description: Optional[str] = None,
        time_range: Optional[str] = None,
    ) -> str:
        """Create or update an analytics dashboard by name (upsert).

        If a dashboard with this name already exists it is updated; otherwise a
        new one is created.

        Args:
            name: Dashboard name — the upsert key.
            panels: Optional list of panel definitions. Each panel is an object:

                - ``title`` (str, required): panel heading.
                - ``chart_type`` (str, required): one of ``line``, ``bar``,
                  ``composed``, ``area``, ``radar``, ``stat``, ``table``.
                - ``query`` (object, required): what to chart —
                  ``{"cube": "check_trend", "measures": [...],
                  "dimensions": [...], "filters": [...],
                  "time_dimensions": [...], "order": {...}}``. ``measures`` is
                  required; everything else is optional. ``cube`` defaults to
                  ``check_trend``. The dashboard ``time_range`` applies to all
                  panels — panels do NOT carry their own time range.
                - ``layout`` (object, required): grid placement
                  ``{"x": >=0, "y": >=0, "w": >=1, "h": >=1}`` (integers).
                - ``table_config`` (object, optional): ONLY for
                  ``chart_type == "table"``.

                ``check_trend`` measures: ``avg_check_value``, ``issue_rate``,
                ``error_rate``, ``datapoint_count``, ``issue_count``,
                ``error_count``, ``test_run_count``, ``avg_latency``,
                ``sum_cost``, ``input_token_count``, ``output_token_count``.
                ``check_trend`` dimensions: ``check.name``, ``check.id``,
                ``target.name``, ``target.id``, ``scenario.name``,
                ``scenario.id``, ``test_run.id``, ``test_run.type``,
                ``test_run.is_latest_for_target``, ``source``, ``provider``,
                ``request_model_name``, ``response_model_name``, ``tag``.
                Use ``query_analytics(include_metadata=True)`` for the
                authoritative, current set.

                Example panel::

                    {"title": "Avg Check Value by Check", "chart_type": "bar",
                     "query": {"measures": ["avg_check_value"],
                               "dimensions": ["check.name"]},
                     "layout": {"x": 0, "y": 0, "w": 6, "h": 4}}
            description: Optional dashboard description.
            time_range: Optional default look-back window for the whole
                dashboard. One of: LAST_HOUR, LAST_24_HOURS, LAST_7_DAYS,
                LAST_14_DAYS, LAST_30_DAYS, LAST_90_DAYS. Defaults to
                LAST_90_DAYS when omitted.
        """
        if not name or not name.strip():
            return json.dumps({"error": "name is required."})

        tr_error = _validate_time_range(time_range)
        if tr_error:
            return json.dumps({"error": tr_error})

        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        try:
            dashboards = okareo_api_request(
                okareo, "get", "/v0/dashboards",
                params={"project_id": project_id},
            )
        except Exception as e:
            return format_tool_error(e)

        existing = _find_dashboard_by_name(
            _dashboards_from_response(dashboards), name
        )
        body: dict = {"name": name}
        if panels is not None:
            body["panels"] = panels
        if description is not None:
            body["description"] = description
        if time_range is not None:
            body["time_range"] = time_range

        try:
            if existing is not None:
                result = okareo_api_request(
                    okareo, "put", f"/v0/dashboards/{existing.get('id')}",
                    json=body, params={"project_id": project_id},
                )
                action = "updated"
            else:
                result = okareo_api_request(
                    okareo, "post", "/v0/dashboards",
                    json=body, params={"project_id": project_id},
                )
                action = "created"
        except Exception as e:
            return format_tool_error(e)

        return json.dumps({
            "dashboard": result,
            "action": action,
            "message": f"Dashboard '{name}' {action}.",
        }, default=str)

    @mcp.tool(
        title="Reorder Dashboards",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def reorder_dashboards(ordered_names: list[str]) -> str:
        """Set the display order of dashboards.

        Args:
            ordered_names: Dashboard names in the desired order.
        """
        if not ordered_names or not isinstance(ordered_names, list):
            return json.dumps({"error": "ordered_names must be a non-empty list."})

        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        try:
            dashboards = okareo_api_request(
                okareo, "get", "/v0/dashboards",
                params={"project_id": project_id},
            )
        except Exception as e:
            return format_tool_error(e)

        name_to_id = {
            d.get("name"): d.get("id")
            for d in _dashboards_from_response(dashboards)
            if isinstance(d, dict)
        }
        ordered_ids: list = []
        unknown: list = []
        for n in ordered_names:
            if n in name_to_id:
                ordered_ids.append(name_to_id[n])
            else:
                unknown.append(n)
        if unknown:
            return json.dumps({
                "error": f"Unknown dashboard(s): {unknown}. "
                "Use list_dashboards to see available dashboards.",
            })

        try:
            okareo_api_request(
                okareo, "put", "/v0/dashboards/reorder",
                json={"ordered_ids": ordered_ids},
                params={"project_id": project_id},
            )
        except Exception as e:
            return format_tool_error(e)
        return json.dumps({
            "ordered": ordered_names,
            "message": f"Reordered {len(ordered_ids)} dashboard(s).",
        })

    @mcp.tool(
        title="Delete Dashboard",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    def delete_dashboard(name: str) -> str:
        """Delete a dashboard by name.

        Args:
            name: Name of the dashboard to delete.
        """
        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        try:
            dashboards = okareo_api_request(
                okareo, "get", "/v0/dashboards",
                params={"project_id": project_id},
            )
        except Exception as e:
            return format_tool_error(e)

        match = _find_dashboard_by_name(
            _dashboards_from_response(dashboards), name
        )
        if match is None:
            return json.dumps({
                "error": f"Dashboard '{name}' not found. "
                "Use list_dashboards to see available dashboards.",
            })

        try:
            okareo_api_request(
                okareo, "delete", f"/v0/dashboards/{match.get('id')}",
                params={"project_id": project_id},
            )
        except Exception as e:
            return format_tool_error(e)
        return json.dumps({
            "deleted": True,
            "name": name,
            "message": f"Dashboard '{name}' has been deleted.",
        })

    return None
