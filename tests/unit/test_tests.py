"""Unit tests for test run tools (list_checks, run_test, list_test_runs,
get_test_run_results).

US1 / FR-003: SDK 0.0.132 widened ``find_test_data_points`` to return
``List[Union[TestDataPointItem, FullDataPointItem]]``. The MCP reads every
data-point field through the defensive ``_get_attr`` / ``_serialize_value``
accessors, so both shapes must format without error or data loss.
"""

import json
from types import SimpleNamespace

from src.tools.tests import _get_attr, _serialize_value


class Unset:
    """Stand-in for the SDK's Unset sentinel — matched by class name in
    ``_get_attr`` / ``_serialize_value`` via ``type(val).__name__``."""


def _test_data_point_item():
    """A minimal TestDataPointItem-shaped object (the pre-0.0.132 shape)."""
    return SimpleNamespace(
        id="dp-1",
        test_id="test-1",
        metric_value={"score": 0.9},
        scenario_input="hello",
    )


def _full_data_point_item():
    """A FullDataPointItem-shaped object — superset, plus an Unset field."""
    return SimpleNamespace(
        id="dp-2",
        test_id="test-2",
        metric_value={"score": 0.7, "generation_output": "full transcript"},
        scenario_input="world",
        scenario_result="expected",
        tags=["a", "b"],
        group_name=Unset(),
    )


class TestDataPointShapeTolerance:
    def test_get_attr_reads_both_shapes(self):
        for dp in (_test_data_point_item(), _full_data_point_item()):
            assert _get_attr(dp, "id")
            assert _get_attr(dp, "test_id")
            # A field absent on TestDataPointItem resolves to the default.
            assert _get_attr(dp, "scenario_result", "missing") in (
                "expected",
                "missing",
            )

    def test_get_attr_treats_unset_as_default(self):
        dp = _full_data_point_item()
        assert _get_attr(dp, "group_name", "fallback") == "fallback"

    def test_serialize_value_handles_both_shapes(self):
        for dp in (_test_data_point_item(), _full_data_point_item()):
            metric = _serialize_value(_get_attr(dp, "metric_value"))
            assert isinstance(metric, dict)
            assert "score" in metric

    def test_full_data_point_extra_fields_serialize(self):
        dp = _full_data_point_item()
        assert _serialize_value(_get_attr(dp, "tags")) == ["a", "b"]
        assert _serialize_value(_get_attr(dp, "group_name")) is None


# ---------------------------------------------------------------------------
# US4: reevaluate_test_run
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock, patch  # noqa: E402


def _tests_tools():
    """Register the test-run tools and return them by name."""
    from mcp.server.fastmcp import FastMCP

    from src.tools.tests import register_tools

    mcp = FastMCP("test")
    register_tools(mcp)
    return {name: tool.fn for name, tool in mcp._tool_manager._tools.items()}


def _check_brief(name, check_id):
    c = SimpleNamespace(name=name, id=check_id)
    return c


class TestReevaluateTestRun:
    @patch("src.tools.tests.okareo_api_request")
    @patch("src.tools.tests._find_test_run")
    @patch("src.tools.tests.resolve_project_id")
    @patch("src.tools.tests.get_okareo_client")
    def test_reevaluate_with_explicit_checks(
        self, mock_client, mock_resolve, mock_find, mock_request, monkeypatch
    ):
        monkeypatch.setenv("OKAREO_API_KEY", "k")
        okareo = MagicMock()
        okareo.get_all_checks.return_value = [
            _check_brief("coherence", "id-coh"),
            _check_brief("tone", "id-tone"),
        ]
        mock_client.return_value = okareo
        mock_resolve.return_value = "proj-1"
        mock_find.return_value = {"id": "run-1", "status": "FINISHED"}
        mock_request.return_value = {"reevaluated": True}

        result = json.loads(_tests_tools()["reevaluate_test_run"](
            test_run_id="run-1", checks=["coherence", "tone"]
        ))

        assert result["original_run_unchanged"] is True
        assert sorted(result["reevaluated_check_ids"]) == ["id-coh", "id-tone"]
        body = mock_request.call_args[1]["json"]
        assert sorted(body["check_ids"]) == ["id-coh", "id-tone"]

    @patch("src.tools.tests.okareo_api_request")
    @patch("src.tools.tests._derive_run_check_ids")
    @patch("src.tools.tests._find_test_run")
    @patch("src.tools.tests.resolve_project_id")
    @patch("src.tools.tests.get_okareo_client")
    def test_reevaluate_defaults_to_existing_checks(
        self, mock_client, mock_resolve, mock_find, mock_derive, mock_request,
        monkeypatch,
    ):
        monkeypatch.setenv("OKAREO_API_KEY", "k")
        okareo = MagicMock()
        okareo.get_all_checks.return_value = [_check_brief("coherence", "id-coh")]
        mock_client.return_value = okareo
        mock_resolve.return_value = "proj-1"
        mock_find.return_value = {"id": "run-1", "status": "FINISHED"}
        mock_derive.return_value = ["id-coh"]
        mock_request.return_value = {"reevaluated": True}

        result = json.loads(_tests_tools()["reevaluate_test_run"](
            test_run_id="run-1"
        ))

        assert result["reevaluated_check_ids"] == ["id-coh"]
        mock_derive.assert_called_once()

    @patch("src.tools.tests._find_test_run")
    @patch("src.tools.tests.resolve_project_id")
    @patch("src.tools.tests.get_okareo_client")
    def test_reevaluate_rejects_non_terminal_run(
        self, mock_client, mock_resolve, mock_find, monkeypatch
    ):
        monkeypatch.setenv("OKAREO_API_KEY", "k")
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        mock_find.return_value = {"id": "run-1", "status": "RUNNING"}

        result = json.loads(_tests_tools()["reevaluate_test_run"](
            test_run_id="run-1", checks=["coherence"]
        ))

        assert "error" in result
        assert "not complete" in result["error"]

    @patch("src.tools.tests._find_test_run")
    @patch("src.tools.tests.resolve_project_id")
    @patch("src.tools.tests.get_okareo_client")
    def test_reevaluate_run_not_found(
        self, mock_client, mock_resolve, mock_find, monkeypatch
    ):
        monkeypatch.setenv("OKAREO_API_KEY", "k")
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = "proj-1"
        mock_find.return_value = None

        result = json.loads(_tests_tools()["reevaluate_test_run"](
            test_run_id="ghost"
        ))

        assert "error" in result
        assert "not found" in result["error"]
