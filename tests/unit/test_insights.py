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
            [],                                  # GET list — empty
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
            [{"id": "d-1", "name": "Trends"}],   # GET list — exists
            {"id": "d-1", "name": "Trends"},     # PUT update
        ]

        result = json.loads(tools["save_dashboard"](name="Trends"))

        assert result["action"] == "updated"
        put_call = mock_request.call_args
        assert put_call[0][1] == "put"
        assert put_call[0][2] == "/v0/dashboards/d-1"

    @patch("src.tools.insights.okareo_api_request")
    @patch("src.tools.insights.resolve_project_id")
    @patch("src.tools.insights.get_okareo_client")
    def test_list_dashboards(self, mock_client, mock_resolve, mock_request, tools):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        mock_request.return_value = [{"id": "d-1", "name": "A"}, {"id": "d-2", "name": "B"}]

        result = json.loads(tools["list_dashboards"]())
        assert result["count"] == 2
        assert result["total"] == 2

    @patch("src.tools.insights.okareo_api_request")
    @patch("src.tools.insights.resolve_project_id")
    @patch("src.tools.insights.get_okareo_client")
    def test_reorder_dashboards(self, mock_client, mock_resolve, mock_request, tools):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        mock_request.side_effect = [
            [{"id": "d-1", "name": "A"}, {"id": "d-2", "name": "B"}],  # GET list
            None,                                                      # PUT reorder
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
        mock_request.return_value = []

        result = json.loads(tools["delete_dashboard"](name="ghost"))
        assert "error" in result
        assert "not found" in result["error"]
