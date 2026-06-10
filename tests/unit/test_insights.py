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
            name="Trends",
            panels=[{"title": "T", "chart_type": "stat",
                     "query": {"measures": ["issue_rate"]},
                     "size": "small-square"}],
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
            name="Trends",
            panels=[{"title": "T", "chart_type": "stat",
                     "query": {"measures": ["issue_rate"]},
                     "size": "small-square"}],
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
            "layout": {"x": 0, "y": 0, "w": 6, "h": 6},
        }

        result = json.loads(tools["save_dashboard"](name="Trends", panels=[panel]))

        # A compliant raw layout passes through byte-identical, with no
        # adjustments reported.
        body = mock_request.call_args[1]["json"]
        assert body["panels"] == [panel]
        assert "adjustments" not in result

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


def _panel(title, size=None, layout=None, chart_type="stat"):
    p = {"title": title, "chart_type": chart_type,
         "query": {"measures": ["issue_rate"]}}
    if size is not None:
        p["size"] = size
    if layout is not None:
        p["layout"] = layout
    return p


def _save(tools, mock_request, panels, saves=1):
    """Run save_dashboard against an empty project; return (results, bodies)."""
    mock_request.side_effect = [
        {"items": []},                       # GET list — empty
        {"id": "d-1", "name": "Board"},      # POST create
    ] * saves
    results, bodies = [], []
    for _ in range(saves):
        results.append(json.loads(tools["save_dashboard"](
            name="Board", panels=panels
        )))
        bodies.append(mock_request.call_args[1]["json"])
    return results, bodies


@patch("src.tools.insights.okareo_api_request")
@patch("src.tools.insights.resolve_project_id")
@patch("src.tools.insights.get_okareo_client")
class TestCardSizes:
    """US1 — named card sizes resolve to exact catalog dimensions."""

    def test_each_named_size_resolves_exact_dims(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        from src.tools.insights import CARD_SIZES

        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        panels = [_panel(name, size=name) for name in CARD_SIZES]

        results, bodies = _save(tools, mock_request, panels)

        assert "error" not in results[0]
        for sent, (name, (w, h)) in zip(bodies[0]["panels"], CARD_SIZES.items()):
            assert sent["layout"]["w"] == w, name
            assert sent["layout"]["h"] == h, name

    def test_unknown_size_rejected_lists_catalog(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        from src.tools.insights import CARD_SIZES

        result = json.loads(tools["save_dashboard"](
            name="Board", panels=[_panel("A", size="tiny-circle")]
        ))

        assert "tiny-circle" in result["error"]
        for name in CARD_SIZES:
            assert name in result["error"]

    def test_panel_without_size_or_layout_rejected(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        result = json.loads(tools["save_dashboard"](
            name="Board", panels=[_panel("Bare")]
        ))
        assert "Bare" in result["error"]
        assert "'size'" in result["error"]

    def test_size_beats_layout_dims_and_reports(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        panels = [_panel("A", size="half-rectangle",
                         layout={"x": 0, "y": 0, "w": 3, "h": 3})]

        results, bodies = _save(tools, mock_request, panels)

        sent = bodies[0]["panels"][0]
        assert sent["layout"] == {"x": 0, "y": 0, "w": 6, "h": 6}
        adjusted_fields = {a["field"] for a in results[0]["adjustments"]}
        assert adjusted_fields == {"layout.w", "layout.h"}

    def test_size_key_not_forwarded_to_backend(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"

        _, bodies = _save(tools, mock_request,
                          [_panel("A", size="small-square")])

        assert "size" not in bodies[0]["panels"][0]

    def test_same_input_yields_identical_layout(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        panels = [_panel("A", size="half-rectangle"),
                  _panel("B", size="small-square"),
                  _panel("C", size="full-square")]

        _, bodies = _save(tools, mock_request, panels, saves=2)

        assert bodies[0]["panels"] == bodies[1]["panels"]


@patch("src.tools.insights.okareo_api_request")
@patch("src.tools.insights.resolve_project_id")
@patch("src.tools.insights.get_okareo_client")
class TestAutoPlacement:
    """US3 — first-fit placement: packed rows, no overlaps, explicit honored."""

    @staticmethod
    def _layouts(body):
        return [p["layout"] for p in body["panels"]]

    def test_half_rectangles_share_first_row(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        panels = [_panel("A", size="half-rectangle"),
                  _panel("B", size="half-rectangle")]

        _, bodies = _save(tools, mock_request, panels)

        a, b = self._layouts(bodies[0])
        assert (a["x"], a["y"]) == (0, 0)
        assert (b["x"], b["y"]) == (6, 0)

    def test_small_squares_fill_remaining_row_space(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        panels = [_panel("A", size="half-rectangle"),
                  _panel("B", size="small-square"),
                  _panel("C", size="small-square")]

        _, bodies = _save(tools, mock_request, panels)

        a, b, c = self._layouts(bodies[0])
        assert (a["x"], a["y"]) == (0, 0)
        assert (b["x"], b["y"]) == (6, 0)
        assert (c["x"], c["y"]) == (9, 0)

    def test_overflow_wraps_to_next_row_without_shrinking(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        panels = [_panel("A", size="full-rectangle"),
                  _panel("B", size="half-rectangle"),
                  _panel("C", size="half-square")]

        _, bodies = _save(tools, mock_request, panels)

        a, b, c = self._layouts(bodies[0])
        assert (a["x"], a["y"], a["w"], a["h"]) == (0, 0, 12, 9)
        assert (b["x"], b["y"], b["w"], b["h"]) == (0, 9, 6, 6)
        assert (c["x"], c["y"], c["w"], c["h"]) == (6, 9, 6, 9)

    def test_explicit_positions_honored(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        panels = [_panel("A", size="half-rectangle",
                         layout={"x": 6, "y": 12})]

        _, bodies = _save(tools, mock_request, panels)

        assert self._layouts(bodies[0])[0] == {"x": 6, "y": 12, "w": 6, "h": 6}

    def test_explicit_overlap_rejected_naming_both_panels(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        panels = [
            _panel("First", layout={"x": 0, "y": 0, "w": 6, "h": 6}),
            _panel("Second", layout={"x": 3, "y": 3, "w": 6, "h": 6}),
        ]

        result = json.loads(tools["save_dashboard"](
            name="Board", panels=panels
        ))

        assert "First" in result["error"]
        assert "Second" in result["error"]
        assert "overlap" in result["error"]

    def test_auto_panels_flow_around_explicit_obstacle(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        panels = [
            _panel("Fixed", layout={"x": 0, "y": 0, "w": 6, "h": 6}),
            _panel("Auto", size="half-rectangle"),
        ]

        _, bodies = _save(tools, mock_request, panels)

        fixed, auto = self._layouts(bodies[0])
        assert (fixed["x"], fixed["y"]) == (0, 0)
        assert (auto["x"], auto["y"]) == (6, 0)


class TestSizeGuidance:
    """US2 — the tool description teaches the size catalog (SC-004)."""

    def test_save_dashboard_description_lists_catalog_and_guidance(self):
        from mcp.server.fastmcp import FastMCP

        from src.tools.insights import CARD_SIZES, register_tools

        mcp = FastMCP("test")
        register_tools(mcp)
        description = mcp._tool_manager._tools["save_dashboard"].description

        for name in CARD_SIZES:
            assert name in description
        # Guidance pairs chart types with sizes at the point of creation.
        assert "stat" in description
        assert "table" in description
        assert "adjustments" in description


@patch("src.tools.insights.okareo_api_request")
@patch("src.tools.insights.resolve_project_id")
@patch("src.tools.insights.get_okareo_client")
class TestHeightNormalization:
    """US4 — raw heights below the width-band floor are sized up on save."""

    def test_half_width_height_floored_to_six(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        panels = [_panel("Squished", layout={"x": 0, "y": 0, "w": 6, "h": 2})]

        results, bodies = _save(tools, mock_request, panels)

        assert bodies[0]["panels"][0]["layout"]["h"] == 6
        assert bodies[0]["panels"][0]["layout"]["w"] == 6
        (adj,) = results[0]["adjustments"]
        assert adj == {"panel": "Squished", "field": "h", "from": 2, "to": 6,
                       "reason": "minimum height for w<=6 is 6"}

    def test_full_width_height_floored_to_nine(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        panels = [_panel("Wide", layout={"x": 0, "y": 0, "w": 12, "h": 5})]

        results, bodies = _save(tools, mock_request, panels)

        assert bodies[0]["panels"][0]["layout"]["h"] == 9
        (adj,) = results[0]["adjustments"]
        assert adj["from"] == 5 and adj["to"] == 9

    def test_compliant_heights_pass_through_unchanged(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        panels = [_panel("A", layout={"x": 0, "y": 0, "w": 3, "h": 6}),
                  _panel("B", layout={"x": 0, "y": 6, "w": 12, "h": 9})]

        results, bodies = _save(tools, mock_request, panels)

        assert [p["layout"] for p in bodies[0]["panels"]] == [
            {"x": 0, "y": 0, "w": 3, "h": 6},
            {"x": 0, "y": 6, "w": 12, "h": 9},
        ]
        assert "adjustments" not in results[0]

    def test_stored_dashboard_round_trip_unchanged(
        self, mock_client, mock_resolve, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        # Panels shaped like get_dashboard output (raw layouts, compliant
        # dims) must re-save byte-identical — the get -> modify -> save
        # round-trip is part of the contract (spec 027 FR-008).
        stored = [
            {"title": "Issue Rate", "chart_type": "stat",
             "query": {"measures": ["issue_rate"]},
             "layout": {"x": 0, "y": 0, "w": 3, "h": 6}},
            {"title": "Runs", "chart_type": "table",
             "query": {"measures": ["test_run_count"]},
             "table_config": {},
             "layout": {"x": 0, "y": 6, "w": 12, "h": 12}},
        ]

        results, bodies = _save(tools, mock_request, stored)

        assert bodies[0]["panels"] == stored
        assert "adjustments" not in results[0]
