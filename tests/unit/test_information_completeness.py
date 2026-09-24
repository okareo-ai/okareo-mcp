"""Every question an agent must answer is still answerable (043 + 041).

Option A drops the run-level row block at every depth. That is only safe if
nothing a consumer legitimately needs went with it. These tests pin the
information contract rather than any one field's location, so a future
reshuffle that loses a capability fails here even if every other test passes.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.okareo_client import ResolvedProject

RUN_ID = "run-info-1"


def _tools(module):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    module.register_tools(mcp)
    return {n: t.fn for n, t in mcp._tool_manager._tools.items()}


def _run():
    return {
        "id": RUN_ID,
        "name": "nightly regression",
        "type": "MULTI_TURN",
        "status": "FINISHED",
        "test_data_point_count": 3,
        "start_time": "2026-09-10T01:00:00",
        "end_time": "2026-09-10T01:30:00",
        "app_link": "https://app.okareo.com/eval/x",
        "mut_id": "m1",
        "scenario_set_id": "s1",
        "driver_id": "d1",
        "author_email": "matt@okareo.com",
        "author_name": "Matt Wyman",
        "author_type": "userToken",
        "simulation_params": {"max_turns": 10},
        "model_metrics": {
            "mean_scores": {"agent_managed_task": 0.33},
            "percentile_scores": {"latency": {"p50": 100.0}},
            "aggregate_check_metadata": {"total_cost": 0.03},
            "aggregate_baseline_metrics": {"avg_turn_latency": 12636.1},
            "check_ids": [{"name": "agent_managed_task", "id": "c1", "version": 1}],
            "scores_by_row": [{"agent_managed_task": True}],
        },
    }


def _dps(n=3):
    out = []
    for i in range(n):
        dp = MagicMock()
        dp.id = dp.test_id = f"dp-{i}"
        dp.metric_value = {"score": i}
        dp.checks = {
            "agent_managed_task": i != 1,
            "agent_managed_task__explanation": f"verdict rationale {i}",
        }
        dp.scenario_input = {"persona": f"caller {i}", "guidance": "g" * 50}
        dp.scenario_result = f"expected {i}"
        dp.model_input = [{"role": "user", "content": f"turn {i}"}]
        dp.model_result = f"reply {i}"
        dp.error_message = None
        out.append(dp)
    return out


@pytest.fixture
def run_results():
    import src.tools.tests as mod
    from src.okareo_client import _reset_for_tests

    def _call(**kwargs):
        _reset_for_tests()
        okareo = MagicMock()
        okareo.api_key = "k"
        okareo.find_test_data_points.return_value = _dps()
        with patch("src.tools.tests.get_okareo_client", return_value=okareo), \
             patch("src.tools.tests.resolve_project",
                   return_value=ResolvedProject(id="p1", name="Demos", basis="explicit")), \
             patch("src.tools.tests.find_test_runs", MagicMock(return_value=[_run()])), \
             patch("src.okareo_client.get_targets_cached", return_value={"m1": "Pandora"}), \
             patch("src.okareo_client.get_scenarios_cached", return_value={"s1": "billing"}), \
             patch("src.okareo_client.get_drivers_cached", return_value={"d1": "angry"}):
            return json.loads(_tools(mod)["get_test_run_results"](
                test_run_id=RUN_ID, **kwargs
            ))

    return _call


class TestQuestionsAnAgentMustBeAbleToAnswer:
    def test_how_did_the_run_do_overall(self, run_results):
        m = run_results()["test_run"]["model_metrics"]
        assert m["mean_scores"]["agent_managed_task"] == 0.33
        assert m["percentile_scores"]["latency"]["p50"] == 100.0

    def test_what_did_it_cost(self, run_results):
        m = run_results()["test_run"]["model_metrics"]
        assert m["aggregate_check_metadata"]["total_cost"] == 0.03
        assert m["aggregate_baseline_metrics"]["avg_turn_latency"]

    def test_which_checks_ran(self, run_results):
        ids = run_results()["test_run"]["model_metrics"]["check_ids"]
        assert [c["name"] for c in ids] == ["agent_managed_task"]

    def test_what_did_it_run_against(self, run_results):
        run = run_results()["test_run"]
        assert run["target"]["name"] == "Pandora"
        assert run["scenario"]["name"] == "billing"
        assert run["driver"]["name"] == "angry"

    def test_who_produced_it(self, run_results):
        run = run_results()["test_run"]
        assert run["author_name"] == "Matt Wyman"
        assert run["author_email"] == "matt@okareo.com"

    def test_which_conversations_failed(self, run_results):
        """The capability that moved from scores_by_row to per-row checks."""
        rows = run_results(detail_level="detailed")["data_points"]
        failed = [
            r["test_id"] for r in rows
            if r["checks"]["agent_managed_task"] is False
        ]
        assert failed == ["dp-1"]

    def test_why_did_that_one_fail(self, run_results):
        rows = run_results(detail_level="detailed")["data_points"]
        failing = next(r for r in rows if not r["checks"]["agent_managed_task"])
        assert failing["checks"]["agent_managed_task__explanation"] == (
            "verdict rationale 1"
        )

    def test_the_verdict_is_attributable_to_its_own_conversation(self, run_results):
        """041's load-bearing property: no prose-matching join needed."""
        for i, row in enumerate(run_results(detail_level="detailed")["data_points"]):
            assert row["test_id"] == f"dp-{i}"
            assert row["checks"]["agent_managed_task__explanation"].endswith(str(i))

    def test_what_was_that_conversation_testing(self, run_results):
        row = run_results()["data_points"][0]
        assert row["scenario_input"]["persona"] == "caller 0"
        assert row["scenario_result"] == "expected 0"

    def test_what_was_actually_said(self, run_results):
        row = run_results(detail_level="full")["data_points"][0]
        assert row["model_input"] and row["model_result"]

    def test_how_do_i_get_the_rest(self, run_results):
        out = run_results(limit=2)
        assert out["has_more"] is True
        assert "offset" in out["next_step"]

    def test_how_many_are_there_in_total(self, run_results):
        assert run_results(limit=2)["total_count"] == 3

    def test_which_depth_answered_me(self, run_results):
        assert run_results()["detail_level"] == "summary"


class TestNothingWasLostWithTheRowBlock:
    """Each capability the dropped block used to carry, still served."""

    def test_per_check_verdicts_still_available(self, run_results):
        assert run_results(detail_level="detailed")["data_points"][0]["checks"]

    def test_judge_explanations_still_available(self, run_results):
        row = run_results(detail_level="detailed")["data_points"][0]
        assert "__explanation" in json.dumps(row["checks"])

    def test_run_level_averages_still_available(self, run_results):
        assert run_results()["test_run"]["model_metrics"]["mean_scores"]

    def test_verdicts_now_page_with_their_rows(self, run_results):
        out = run_results(detail_level="detailed", limit=1, offset=1)
        assert len(out["data_points"]) == 1
        assert out["data_points"][0]["test_id"] == "dp-1"
        assert out["data_points"][0]["checks"]["agent_managed_task"] is False


class TestListingsStillAnswerFindingQuestions:
    def _list(self, tool, module_path, **kwargs):
        import importlib

        mod = importlib.import_module(module_path)
        from src.okareo_client import _reset_for_tests

        _reset_for_tests()
        okareo = MagicMock()
        okareo.api_key = "k"
        patch_base = module_path.replace("src.tools.", "src.tools.")
        with patch(f"{patch_base}.get_okareo_client", return_value=okareo), \
             patch(f"{patch_base}.resolve_project",
                   return_value=ResolvedProject(id="p1", name="Demos", basis="explicit")), \
             patch(f"{patch_base}.find_test_runs", MagicMock(return_value=[_run()])), \
             patch("src.okareo_client.get_targets_cached", return_value={"m1": "Pandora"}), \
             patch("src.okareo_client.get_scenarios_cached", return_value={"s1": "billing"}), \
             patch("src.okareo_client.get_drivers_cached", return_value={"d1": "angry"}):
            return json.loads(_tools(mod)[tool](**kwargs))

    def test_find_the_most_recent_run(self):
        out = self._list("list_test_runs", "src.tools.tests")
        assert out["test_runs"][0]["name"] == "nightly regression"
        assert out["test_runs"][0]["status"] == "FINISHED"

    def test_know_what_a_listed_run_ran_against(self):
        entry = self._list("list_test_runs", "src.tools.tests")["test_runs"][0]
        assert entry["target"]["name"] == "Pandora"

    def test_compare_runs_without_opening_them(self):
        entry = self._list(
            "list_test_runs", "src.tools.tests", detail_level="detailed",
        )["test_runs"][0]
        assert entry["model_metrics"]["mean_scores"]
        assert entry["target"]["name"] == "Pandora"

    def test_judge_the_cost_of_opening_a_run(self):
        entry = self._list("list_test_runs", "src.tools.tests")["test_runs"][0]
        assert entry["test_data_point_count"] == 3

    def test_simulations_listing_answers_the_same(self):
        out = self._list(
            "list_simulations", "src.tools.simulations", detail_level="detailed",
        )
        assert out["simulations"][0]["model_metrics"]
        assert out["simulations"][0]["target"]["name"] == "Pandora"
