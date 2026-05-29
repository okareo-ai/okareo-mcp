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

from src.error_handling import format_tool_error
from src.okareo_client import (
    get_okareo_client,
    okareo_api_request,
    resolve_project_id,
)


def _find_dashboard_by_name(dashboards, name: str):
    """Return the dashboard dict whose name matches, or None."""
    if not isinstance(dashboards, list):
        return None
    for d in dashboards:
        if isinstance(d, dict) and d.get("name") == name:
            return d
    return None


def register_tools(mcp: FastMCP) -> None:
    """Register analytics and dashboard tools with the FastMCP server."""

    @mcp.tool()
    def query_analytics(
        measures: list[str],
        dimensions: Optional[list[str]] = None,
        cube: Optional[str] = None,
        filters: Optional[list[dict]] = None,
        time_range: Optional[dict] = None,
        include_metadata: bool = False,
    ) -> str:
        """Query Okareo's product analytics to understand evaluation trends.

        Answers questions like "how is my evaluation quality trending" by
        aggregating measures across dimensions.

        Args:
            measures: Metrics to aggregate (e.g. ["test_runs.count"]). Required.
            dimensions: Optional group-by fields (e.g. ["test_runs.day"]).
            cube: Optional analytics cube name to scope the query.
            filters: Optional list of filter objects.
            time_range: Optional time-range object.
            include_metadata: When true, also return the available cubes,
                dimensions, and measures so the query can be refined.
        """
        if not measures or not isinstance(measures, list):
            return json.dumps({"error": "measures must be a non-empty list."})

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

        body: dict = {"project_id": str(project_id), "measures": measures}
        if dimensions:
            body["dimensions"] = dimensions
        if cube:
            body["cube"] = cube
        if filters:
            body["filters"] = filters
        if time_range:
            body["time_range"] = time_range

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

    @mcp.tool()
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

        dashboards = dashboards if isinstance(dashboards, list) else []
        total = len(dashboards)
        if limit and limit > 0:
            dashboards = dashboards[:limit]
        return json.dumps({
            "dashboards": dashboards,
            "count": len(dashboards),
            "total": total,
        }, default=str)

    @mcp.tool()
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

        match = _find_dashboard_by_name(dashboards, name)
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

    @mcp.tool()
    def save_dashboard(
        name: str,
        panels: Optional[list[dict]] = None,
        description: Optional[str] = None,
        time_range: Optional[dict] = None,
    ) -> str:
        """Create or update an analytics dashboard by name (upsert).

        If a dashboard with this name already exists it is updated; otherwise a
        new one is created.

        Args:
            name: Dashboard name — the upsert key.
            panels: Optional list of panel definitions.
            description: Optional dashboard description.
            time_range: Optional default time-range object.
        """
        if not name or not name.strip():
            return json.dumps({"error": "name is required."})

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

        existing = _find_dashboard_by_name(dashboards, name)
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

    @mcp.tool()
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
            for d in (dashboards or [])
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

    @mcp.tool()
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

        match = _find_dashboard_by_name(dashboards, name)
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
