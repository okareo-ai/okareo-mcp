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
    @patch("src.tools.tests._find_test_run")
    @patch("src.tools.tests.resolve_project_id")
    @patch("src.tools.tests.get_okareo_client")
    def test_reevaluate_with_explicit_checks(
        self, mock_client, mock_resolve, mock_find, monkeypatch
    ):
        monkeypatch.setenv("OKAREO_API_KEY", "k")
        okareo = MagicMock()
        okareo.get_all_checks.return_value = [
            _check_brief("coherence", "id-coh"),
            _check_brief("tone", "id-tone"),
        ]
        okareo.re_evaluate.return_value = {"reevaluated": True}
        mock_client.return_value = okareo
        mock_resolve.return_value = "proj-1"
        mock_find.return_value = {"id": "run-1", "status": "FINISHED"}

        result = json.loads(_tests_tools()["reevaluate_test_run"](
            test_run_id="run-1", checks=["coherence", "tone"]
        ))

        assert result["original_run_unchanged"] is True
        assert sorted(result["reevaluated_check_ids"]) == ["id-coh", "id-tone"]
        run_id, check_ids = okareo.re_evaluate.call_args[0]
        assert run_id == "run-1"
        assert sorted(check_ids) == ["id-coh", "id-tone"]

    @patch("src.tools.tests._derive_run_check_ids")
    @patch("src.tools.tests._find_test_run")
    @patch("src.tools.tests.resolve_project_id")
    @patch("src.tools.tests.get_okareo_client")
    def test_reevaluate_defaults_to_existing_checks(
        self, mock_client, mock_resolve, mock_find, mock_derive,
        monkeypatch,
    ):
        monkeypatch.setenv("OKAREO_API_KEY", "k")
        okareo = MagicMock()
        okareo.get_all_checks.return_value = [_check_brief("coherence", "id-coh")]
        okareo.re_evaluate.return_value = {"reevaluated": True}
        mock_client.return_value = okareo
        mock_resolve.return_value = "proj-1"
        mock_find.return_value = {"id": "run-1", "status": "FINISHED"}
        mock_derive.return_value = ["id-coh"]

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


# ---------------------------------------------------------------------------
# run_test tool — faux-async handoff (spec 025, FR-008)
# ---------------------------------------------------------------------------

class TestRunTestToolHandoff:
    def _wire(self, mock_client, mock_resolve, run_test_impl):
        """Common okareo/scenario wiring; run_test_impl drives mut.run_test."""
        scenario = SimpleNamespace(name="my-scenario", scenario_id="sid")
        scen_mod = MagicMock()
        scen_mod.sync.return_value = [scenario]

        okareo = MagicMock()
        okareo.api_key = "k"
        mut = MagicMock()
        mut.run_test.side_effect = run_test_impl
        okareo.get_model.return_value = mut
        okareo.get_all_checks.return_value = [_check_brief("coherence", "id-coh")]
        mock_client.return_value = okareo
        mock_resolve.return_value = "proj"
        return okareo, mut, scen_mod

    @patch("src.tools.simulations._find_runs", lambda *a, **k: {})
    @patch("src.tools.tests.resolve_project_id")
    @patch("src.tools.tests.get_okareo_client")
    def test_finished_inline_returns_handoff(self, mock_client, mock_resolve):
        from okareo_api_client.api import default as _default_pkg

        result = SimpleNamespace(
            id="tr-1", name="my-scenario-my-model", app_link="http://app/tr-1"
        )
        _okareo, mut, scen_mod = self._wire(
            mock_client, mock_resolve, lambda **kw: result
        )
        tools = _tests_tools()
        with patch.object(
            _default_pkg, "get_scenario_sets_v0_scenario_sets_get",
            scen_mod, create=True,
        ):
            out = json.loads(tools["run_test"](
                scenario_name="my-scenario",
                model_name="my-model",
                checks=["coherence"],
            ))

        assert out["status"] == "finished"
        assert out["test_run_id"] == "tr-1"
        assert out["type"] == "NL_GENERATION"
        assert out["model"] == "my-model"
        assert out["scenario"] == "my-scenario"
        # No conversation-transcript hint on a single-turn test.
        assert "get_conversation_transcript" not in out["message"]
        mut.run_test.assert_called_once()

    @patch("src.tools.simulations._find_runs", lambda *a, **k: {})
    @patch("src.tools.tests.resolve_project_id")
    @patch("src.tools.tests.get_okareo_client")
    def test_failure_surfaces_inline(self, mock_client, mock_resolve):
        from okareo_api_client.api import default as _default_pkg

        def _boom(**kw):
            raise RuntimeError("backend rejected the run")

        _okareo, mut, scen_mod = self._wire(mock_client, mock_resolve, _boom)
        tools = _tests_tools()
        with patch.object(
            _default_pkg, "get_scenario_sets_v0_scenario_sets_get",
            scen_mod, create=True,
        ):
            out = json.loads(tools["run_test"](
                scenario_name="my-scenario",
                model_name="my-model",
                checks=["coherence"],
            ))

        assert "error" in out


# ---------------------------------------------------------------------------
# 034: analytics annotations for get_test_run_results / transcript
# ---------------------------------------------------------------------------


class TestAnalyticsAnnotations:
    def test_get_test_run_results_by_id(self):
        from src.analytics_context import call_scope

        tools = _tests_tools()
        mock_okareo = MagicMock()
        mock_okareo.find_test_data_points.return_value = [
            SimpleNamespace(
                id="dp-1", test_id="dp-1", metric_value={"score": 1},
                scenario_input="a", scenario_result="b",
            ),
            SimpleNamespace(
                id="dp-2", test_id="dp-2", metric_value={"score": 0},
                scenario_input="c", scenario_result="d",
            ),
        ]

        with patch("src.tools.tests.get_okareo_client", return_value=mock_okareo), \
             patch("src.tools.tests.resolve_project_id", return_value="proj-1"), \
             patch("src.tools.tests.find_test_runs", return_value=[{
                 "id": "run-abc",
                 "name": "my-run",
                 "model_metrics": {},
             }]), \
             call_scope() as annotations:
            out = json.loads(tools["get_test_run_results"](
                test_run_id="run-abc", include_transcripts=True,
            ))

        assert "error" not in out
        assert annotations["entity_type"] == "test_run"
        assert annotations["entity_id"] == "run-abc"
        assert annotations["lookup_by"] == "id"
        assert annotations["include_transcripts"] is True
        assert annotations["result_count"] == 2
        assert annotations["project_id"] == "proj-1"

    def test_get_test_run_results_by_name_records_resolved_id(self):
        from src.analytics_context import call_scope

        tools = _tests_tools()
        mock_okareo = MagicMock()
        mock_okareo.find_test_data_points.return_value = []

        with patch("src.tools.tests.get_okareo_client", return_value=mock_okareo), \
             patch("src.tools.tests.resolve_project_id", return_value="proj-1"), \
             patch(
                 "src.tools.tests.find_test_runs",
                 return_value=[{
                     "id": "resolved-uuid",
                     "name": "Nightly",
                     "start_time": "2026-01-02",
                     "model_metrics": {},
                 }],
             ), \
             call_scope() as annotations:
            out = json.loads(tools["get_test_run_results"](name="Nightly"))

        assert "error" not in out
        assert annotations["lookup_by"] == "name"
        assert annotations["entity_id"] == "resolved-uuid"
        assert "Nightly" not in annotations.values()

    def test_get_test_run_results_name_miss_has_no_entity_id(self):
        from src.analytics_context import call_scope

        tools = _tests_tools()
        mock_okareo = MagicMock()

        with patch("src.tools.tests.get_okareo_client", return_value=mock_okareo), \
             patch("src.tools.tests.resolve_project_id", return_value="proj-1"), \
             patch("src.tools.tests.find_test_runs", return_value=[]), \
             call_scope() as annotations:
            out = json.loads(tools["get_test_run_results"](name="missing"))

        assert "error" in out
        assert annotations["lookup_by"] == "name"
        assert annotations["project_id"] == "proj-1"
        assert "entity_id" not in annotations

    def test_get_test_run_results_project_resolve_fail_still_emits(self):
        from src.analytics_context import call_scope

        tools = _tests_tools()

        with patch(
            "src.tools.tests.get_okareo_client",
            side_effect=ValueError("no key"),
        ), call_scope() as annotations:
            out = json.loads(tools["get_test_run_results"](test_run_id="x"))

        assert "error" in out
        assert "project_id" not in annotations

    def test_get_conversation_transcript_annotations(self):
        from src.analytics_context import call_scope

        tools = _tests_tools()
        mock_okareo = MagicMock()
        mock_okareo.find_test_data_points.return_value = [
            SimpleNamespace(
                id="dp-99", test_id="dp-99",
                scenario_input="in", scenario_result="out",
                model_input=[], model_result={}, metric_value={},
            ),
        ]

        with patch("src.tools.tests.get_okareo_client", return_value=mock_okareo), \
             patch("src.tools.tests.resolve_project_id", return_value="proj-1"), \
             patch("src.tools.tests.find_test_runs", return_value=[{
                 "id": "run-1", "name": "r", "model_metrics": {},
             }]), \
             call_scope() as annotations:
            out = json.loads(tools["get_conversation_transcript"](
                test_run_id="run-1", test_id="dp-99",
            ))

        assert "error" not in out
        assert annotations["entity_type"] == "test_run"
        assert annotations["entity_id"] == "run-1"
        assert annotations["lookup_by"] == "id"
        assert annotations["data_point_id"] == "dp-99"
        assert annotations["project_id"] == "proj-1"

    def test_get_conversation_transcript_lookup_by_index(self):
        from src.analytics_context import call_scope

        tools = _tests_tools()
        mock_okareo = MagicMock()
        mock_okareo.find_test_data_points.return_value = [
            SimpleNamespace(
                id="dp-1", test_id="dp-1",
                scenario_input="in", scenario_result="out",
                model_input=[], model_result={}, metric_value={},
            ),
        ]

        with patch("src.tools.tests.get_okareo_client", return_value=mock_okareo), \
             patch("src.tools.tests.resolve_project_id", return_value="proj-1"), \
             patch("src.tools.tests.find_test_runs", return_value=[{
                 "id": "run-1",
                 "name": "r",
                 "model_metrics": {
                     "scores_by_row": [
                         {"test_id": "dp-1", "scenario_index": 1},
                     ],
                 },
             }]), \
             call_scope() as annotations:
            out = json.loads(tools["get_conversation_transcript"](
                test_run_id="run-1", scenario_index=1,
            ))

        assert annotations["lookup_by"] == "index"
        assert annotations["entity_id"] == "run-1"
        assert "error" not in out

    @staticmethod
    def _multiturn_run_and_points():
        """scores_by_row as the API really returns it: positional, no test_id."""
        mock_okareo = MagicMock()
        mock_okareo.find_test_data_points.return_value = [
            SimpleNamespace(
                id=f"dp-{n}", test_id=f"dp-{n}",
                scenario_input="in", scenario_result="out",
                model_input=[], model_result={}, metric_value={},
            )
            for n in ("a", "b", "c")
        ]
        run = [{
            "id": "run-1", "name": "r",
            "model_metrics": {
                "scores_by_row": [
                    {"response_loop": False},
                    {"response_loop": True},
                    {"response_loop": False},
                ],
            },
        }]
        return mock_okareo, run

    def test_get_conversation_transcript_index_without_test_id_in_scores(self):
        mock_okareo, run = self._multiturn_run_and_points()

        with patch("src.tools.tests.get_okareo_client", return_value=mock_okareo), \
             patch("src.tools.tests.resolve_project_id", return_value="proj-1"), \
             patch("src.tools.tests.find_test_runs", return_value=run):
            tools = _tests_tools()
            out = json.loads(tools["get_conversation_transcript"](
                test_run_id="run-1", scenario_index=2,
            ))

        assert "error" not in out
        assert out["test_id"] == "dp-b"
        assert out["scenario_index"] == 2

    def test_get_conversation_transcript_index_out_of_range_message(self):
        mock_okareo, run = self._multiturn_run_and_points()

        with patch("src.tools.tests.get_okareo_client", return_value=mock_okareo), \
             patch("src.tools.tests.resolve_project_id", return_value="proj-1"), \
             patch("src.tools.tests.find_test_runs", return_value=run):
            tools = _tests_tools()
            out = json.loads(tools["get_conversation_transcript"](
                test_run_id="run-1", scenario_index=7,
            ))

        assert "valid indices 1-3" in out["error"]

    def test_get_test_run_results_assigns_positional_scenario_index(self):
        mock_okareo, run = self._multiturn_run_and_points()

        with patch("src.tools.tests.get_okareo_client", return_value=mock_okareo), \
             patch("src.tools.tests.resolve_project_id", return_value="proj-1"), \
             patch("src.tools.tests.find_test_runs", return_value=run):
            tools = _tests_tools()
            out = json.loads(tools["get_test_run_results"](test_run_id="run-1"))

        assert [d["scenario_index"] for d in out["data_points"]] == [1, 2, 3]


class TestListToolsSkipRowLevelMetrics:
    """find_test_runs has no limit parameter, so the only lever a list tool has on
    response size is return_model_metrics. Asking for row-level metrics pulls a
    written explanation per check per row for every run in the project, which
    overruns Cloud Run's response cap and fails the request with a bare HTTP 500.
    """

    def test_list_test_runs_does_not_request_row_level_metrics(self):
        from unittest.mock import MagicMock, patch

        tools = _tests_tools()
        find = MagicMock(return_value=[])

        with patch("src.tools.tests.get_okareo_client", return_value=MagicMock()), \
             patch("src.tools.tests.resolve_project_id", return_value="proj-1"), \
             patch("src.tools.tests.find_test_runs", find):
            json.loads(tools["list_test_runs"]())

        payload = find.call_args[0][1]
        assert payload.return_model_metrics is False
