"""Unit tests for analytics and dashboard tools (US5)."""

import json
from unittest.mock import MagicMock, patch

import pytest


def _register_and_get_tools():
    from mcp.server.fastmcp import FastMCP

    from src.tools.insights import register_tools

    mcp = FastMCP("test")
    register_tools(mcp)
    return {name: tool.fn for name, tool in mcp._tool_manager._tools.items()}


@pytest.fixture
def tools():
    return _register_and_get_tools()


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setenv("OKAREO_API_KEY", "test-api-key-12345")


class TestQueryAnalytics:
    @patch("src.tools.insights.okareo_api_request")
    @patch("src.tools.insights.resolve_project_id")
    @patch("src.tools.insights.get_okareo_client")
    def test_basic_query(self, mock_client, mock_resolve, mock_request, tools):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        mock_request.return_value = {"rows": [{"count": 5}]}

        result = json.loads(tools["query_analytics"](
            measures=["test_runs.count"], dimensions=["test_runs.day"]
        ))

        assert result["result"] == {"rows": [{"count": 5}]}
        call = mock_request.call_args
        assert call[0][1] == "post" and call[0][2] == "/v0/analytics/query"
        body = call[1]["json"]
        assert body["project_id"] == "proj-1"
        assert body["measures"] == ["test_runs.count"]
        assert body["dimensions"] == ["test_runs.day"]

    @patch("src.tools.insights.okareo_api_request")
    @patch("src.tools.insights.resolve_project_id")
    @patch("src.tools.insights.get_okareo_client")
    def test_query_with_metadata(self, mock_client, mock_resolve, mock_request, tools):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        mock_request.side_effect = [
            {"cubes": ["test_runs"]},   # /v0/analytics/meta
            {"rows": []},               # /v0/analytics/query
        ]

        result = json.loads(tools["query_analytics"](
            measures=["test_runs.count"], include_metadata=True
        ))

        assert result["metadata"] == {"cubes": ["test_runs"]}
        assert "result" in result

    def test_empty_measures_rejected(self, tools):
        result = json.loads(tools["query_analytics"](measures=[]))
        assert "error" in result

    @patch("src.tools.insights.okareo_api_request")
    @patch("src.tools.insights.resolve_project_id")
    @patch("src.tools.insights.get_okareo_client")
    def test_query_analytics_defaults_time_range(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        mock_request.return_value = {"rows": []}

        json.loads(tools["query_analytics"](measures=["avg_check_value"]))

        body = mock_request.call_args[1]["json"]
        assert body["time_range"] == "LAST_30_DAYS"
        assert "time_dimensions" not in body

    @patch("src.tools.insights.okareo_api_request")
    @patch("src.tools.insights.resolve_project_id")
    @patch("src.tools.insights.get_okareo_client")
    def test_query_analytics_time_dimensions_passthrough(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        mock_request.return_value = {"rows": []}

        td = [{"dimension": "test_run.start_time", "granularity": "day"}]
        json.loads(tools["query_analytics"](
            measures=["avg_check_value"], time_dimensions=td
        ))

        body = mock_request.call_args[1]["json"]
        assert body["time_dimensions"] == td
        # No default time_range is injected when time_dimensions is supplied.
        assert "time_range" not in body

    def test_query_analytics_rejects_bad_time_range(self, tools):
        result = json.loads(tools["query_analytics"](
            measures=["avg_check_value"], time_range="LAST_DECADE"
        ))
        assert "error" in result
        assert "LAST_30_DAYS" in result["error"]


class TestDashboards:
    @patch("src.tools.insights.okareo_api_request")
    @patch("src.tools.insights.resolve_project_id")
    @patch("src.tools.insights.get_okareo_client")
    def test_save_dashboard_creates_when_absent(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        mock_request.side_effect = [
            {"items": []},                       # GET list — empty envelope
            {"id": "d-1", "name": "Trends"},     # POST create
        ]

        result = json.loads(tools["save_dashboard"](
            name="Trends", panels=[{"k": "v"}]
        ))

        assert result["action"] == "created"
        assert mock_request.call_args[0][1] == "post"

    @patch("src.tools.insights.okareo_api_request")
    @patch("src.tools.insights.resolve_project_id")
    @patch("src.tools.insights.get_okareo_client")
    def test_save_dashboard_updates_when_present(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        mock_request.side_effect = [
            {"items": [{"id": "d-1", "name": "Trends"}]},   # GET list — exists
            {"id": "d-1", "name": "Trends"},                # PUT update
        ]

        result = json.loads(tools["save_dashboard"](name="Trends"))

        assert result["action"] == "updated"
        put_call = mock_request.call_args
        assert put_call[0][1] == "put"
        assert put_call[0][2] == "/v0/dashboards/d-1"

    @patch("src.tools.insights.okareo_api_request")
    @patch("src.tools.insights.resolve_project_id")
    @patch("src.tools.insights.get_okareo_client")
    def test_save_dashboard_no_duplicate_on_resave(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        # Second save of an existing name must PUT (update), never POST again.
        mock_request.side_effect = [
            {"items": [{"id": "d-1", "name": "Trends"}]},   # GET list — exists
            {"id": "d-1", "name": "Trends"},                # PUT update
        ]

        result = json.loads(tools["save_dashboard"](
            name="Trends", panels=[{"k": "v2"}]
        ))

        assert result["action"] == "updated"
        methods = [call.args[1] for call in mock_request.call_args_list]
        assert "post" not in methods

    @patch("src.tools.insights.okareo_api_request")
    @patch("src.tools.insights.resolve_project_id")
    @patch("src.tools.insights.get_okareo_client")
    def test_save_dashboard_time_range_string(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        mock_request.side_effect = [
            {"items": []},                       # GET list — empty
            {"id": "d-1", "name": "Trends"},     # POST create
        ]

        json.loads(tools["save_dashboard"](
            name="Trends", time_range="LAST_30_DAYS"
        ))

        body = mock_request.call_args[1]["json"]
        assert body["time_range"] == "LAST_30_DAYS"

    def test_save_dashboard_rejects_bad_time_range(self, tools):
        result = json.loads(tools["save_dashboard"](
            name="Trends", time_range="LAST_DECADE"
        ))
        assert "error" in result
        assert "LAST_30_DAYS" in result["error"]

    @patch("src.tools.insights.okareo_api_request")
    @patch("src.tools.insights.resolve_project_id")
    @patch("src.tools.insights.get_okareo_client")
    def test_save_dashboard_accepts_panel_shape(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        mock_request.side_effect = [
            {"items": []},                       # GET list — empty
            {"id": "d-1", "name": "Trends"},     # POST create
        ]
        panel = {
            "title": "Avg Check Value by Check",
            "chart_type": "bar",
            "query": {"measures": ["avg_check_value"], "dimensions": ["check.name"]},
            "layout": {"x": 0, "y": 0, "w": 6, "h": 4},
        }

        json.loads(tools["save_dashboard"](name="Trends", panels=[panel]))

        body = mock_request.call_args[1]["json"]
        assert body["panels"] == [panel]

    @patch("src.tools.insights.okareo_api_request")
    @patch("src.tools.insights.resolve_project_id")
    @patch("src.tools.insights.get_okareo_client")
    def test_get_dashboard_found(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        mock_request.side_effect = [
            {"items": [{"id": "d-1", "name": "Trends"}]},   # GET list envelope
            {"id": "d-1", "name": "Trends", "panels": []},  # GET by id
        ]

        result = json.loads(tools["get_dashboard"](name="Trends"))

        assert "error" not in result
        assert result["dashboard"]["id"] == "d-1"

    @patch("src.tools.insights.okareo_api_request")
    @patch("src.tools.insights.resolve_project_id")
    @patch("src.tools.insights.get_okareo_client")
    def test_list_dashboards(self, mock_client, mock_resolve, mock_request, tools):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        mock_request.return_value = {
            "items": [{"id": "d-1", "name": "A"}, {"id": "d-2", "name": "B"}]
        }

        result = json.loads(tools["list_dashboards"]())
        assert result["count"] == 2
        assert result["total"] == 2
        assert {d["name"] for d in result["dashboards"]} == {"A", "B"}

    @patch("src.tools.insights.okareo_api_request")
    @patch("src.tools.insights.resolve_project_id")
    @patch("src.tools.insights.get_okareo_client")
    def test_reorder_dashboards(self, mock_client, mock_resolve, mock_request, tools):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        mock_request.side_effect = [
            {"items": [{"id": "d-1", "name": "A"}, {"id": "d-2", "name": "B"}]},  # GET list
            None,                                                                # PUT reorder
        ]

        result = json.loads(tools["reorder_dashboards"](ordered_names=["B", "A"]))

        assert "message" in result
        reorder_call = mock_request.call_args
        assert reorder_call[1]["json"]["ordered_ids"] == ["d-2", "d-1"]

    @patch("src.tools.insights.okareo_api_request")
    @patch("src.tools.insights.resolve_project_id")
    @patch("src.tools.insights.get_okareo_client")
    def test_delete_dashboard_not_found(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        mock_request.return_value = {"items": []}

        result = json.loads(tools["delete_dashboard"](name="ghost"))
        assert "error" in result
        assert "not found" in result["error"]

    @patch("src.tools.insights.okareo_api_request")
    @patch("src.tools.insights.resolve_project_id")
    @patch("src.tools.insights.get_okareo_client")
    def test_delete_dashboard_found(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        mock_request.side_effect = [
            {"items": [{"id": "d-1", "name": "Trends"}]},   # GET list envelope
            None,                                           # DELETE
        ]

        result = json.loads(tools["delete_dashboard"](name="Trends"))

        assert result["deleted"] is True
        del_call = mock_request.call_args
        assert del_call[0][1] == "delete"
        assert del_call[0][2] == "/v0/dashboards/d-1"
