"""Depth, paging and provenance for get_test_run_results (043 US1).

The defect this covers: for a MULTI_TURN run the per-conversation scores did
not live in the field the tool pages over. They arrived inside
``model_metrics.scores_by_row`` -- a full-run list -- while the paged
``data_points`` carried an empty ``metric_value``. So ``limit=1`` against a
50-conversation run returned all 50 conversations' scores and written
explanations. Measured on a live run 2026-09-22.

That block is now dropped at every depth rather than paged: 041 established
it is not positionally aligned to the data-point list and carries no key to
join on, and the backend is removing its ``__explanation`` entries. Per-row
verdicts and evidence come from each data point's own ``checks``, which is
correctly paired to its conversation and pages with it.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.okareo_client import ResolvedProject


def _register_and_get_tools():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    from src.tools.tests import register_tools

    register_tools(mcp)
    return {name: t.fn for name, t in mcp._tool_manager._tools.items()}


@pytest.fixture
def tools():
    return _register_and_get_tools()


@pytest.fixture(autouse=True)
def _isolate_caches():
    from src.okareo_client import _reset_for_tests

    _reset_for_tests()
    yield
    _reset_for_tests()


RUN_ID = "8e862d88-26ae-4578-830d-dd7bb5f3017d"


def _scores_row(i, passed):
    """One scores_by_row entry: per-check keys plus __explanation keys."""
    return {
        "agent_managed_task": passed,
        "agent_managed_task__explanation": f"explanation for conversation {i}",
        "reasoning-expectation-met": passed,
        "reasoning-expectation-met__explanation": f"reasoning prose {i}",
    }


def _run(n_rows, with_scores=True):
    """A run record shaped like the live API response."""
    metrics = {
        "mean_scores": {"agent_managed_task": 0.5},
        "percentile_scores": {},
        "aggregate_check_metadata": {"total_cost": 0.03},
        "aggregate_baseline_metrics": {"avg_turn_latency": 12636.1},
        "check_ids": [{"name": "agent_managed_task", "id": "c1", "version": 1}],
    }
    if with_scores:
        metrics["scores_by_row"] = [
            _scores_row(i, i % 2 == 0) for i in range(n_rows)
        ]
    return {
        "id": RUN_ID,
        "name": "REPS R - confused-caller",
        "type": "MULTI_TURN",
        "status": "FINISHED",
        "test_data_point_count": n_rows,
        "start_time": "2026-09-10T01:25:08",
        "end_time": "2026-09-10T01:27:27",
        "time_created": "2026-09-10T01:25:08",
        "app_link": "https://app.okareo.com/eval/x",
        "mut_id": "m1",
        "scenario_set_id": "s1",
        "driver_id": "d1",
        "filter_group_id": "fg1",
        "project_id": "proj-1",
        "tags": [],
        "error_matrix": [],
        "failure_message": None,
        "progress": 100.0,
        "simulation_params": {"max_turns": 10},
        "author_email": "matt@okareo.com",
        "author_name": "Matt Wyman",
        "author_type": "userToken",
        "model_metrics": metrics,
    }


def _data_points(n):
    out = []
    for i in range(n):
        dp = MagicMock()
        dp.id = f"dp-{i}"
        dp.test_id = f"dp-{i}"
        dp.metric_value = {"generation_output": "transcript " * 30}
        dp.checks = {
            "agent_managed_task": i % 2 == 0,
            "agent_managed_task__explanation": f"judge prose for conversation {i}",
        }
        dp.scenario_input = {
            "sub_capability": "ambiguous-intent",
            "persona": "confused, hesitant, imprecise",
            "guidance": "Open with exactly: 'I need to sort out my order.' " * 3,
            "driver": "confused-caller",
        }
        dp.scenario_result = f"Expected outcome {i}"
        dp.model_input = [{"role": "user", "content": "long transcript " * 40}]
        dp.model_result = "final answer " * 40
        dp.error_message = None
        out.append(dp)
    return out


def _call(tools, n_rows=50, with_scores=True, find_spy=None, **kwargs):
    okareo = MagicMock()
    okareo.api_key = "k"
    okareo.find_test_data_points.return_value = _data_points(n_rows)

    find = find_spy or MagicMock(return_value=[_run(n_rows, with_scores)])

    with patch("src.tools.tests.get_okareo_client", return_value=okareo), \
         patch("src.tools.tests.resolve_project",
               return_value=ResolvedProject(id="proj-1", name="Demos", basis="explicit")), \
         patch("src.tools.tests.find_test_runs", find), \
         patch("src.okareo_client.get_targets_cached", return_value={"m1": "Pandora Voice"}), \
         patch("src.okareo_client.get_scenarios_cached", return_value={"s1": "confused-caller-set"}), \
         patch("src.okareo_client.get_drivers_cached", return_value={"d1": "confused-caller"}):
        raw = tools["get_test_run_results"](test_run_id=RUN_ID, **kwargs)
    return json.loads(raw)


class TestRunFetchNeverAsksForRowMetrics:
    """043 R1 revised by 041: the run-level row block is unjoinable and is
    losing its explanations, so it is never surfaced -- which means it is never
    requested either, at any depth. The saving is on the wire throughout."""

    def _metrics_flag(self, tools, **kwargs):
        find = MagicMock(return_value=[_run(5)])
        _call(tools, n_rows=5, find_spy=find, **kwargs)
        return find.call_args[0][1].return_model_metrics

    def test_summary_does_not_request_it(self, tools):
        assert self._metrics_flag(tools, detail_level="summary") is False

    def test_detailed_does_not_request_it_either(self, tools):
        assert self._metrics_flag(tools, detail_level="detailed") is False

    def test_full_does_not_request_it_either(self, tools):
        assert self._metrics_flag(tools, detail_level="full") is False


class TestDepthValidation:
    def test_unknown_depth_is_rejected_before_any_network_call(self, tools):
        find = MagicMock()
        with patch("src.tools.tests.get_okareo_client") as client:
            out = json.loads(
                tools["get_test_run_results"](
                    test_run_id=RUN_ID, detail_level="verbose",
                )
            )
        assert "verbose" in out["error"]
        assert client.call_count == 0
        assert find.call_count == 0


class TestRowLevelBlockIsNeverSurfaced:
    """Option A: the block is not positionally aligned to the data-point list
    and carries no join key (041), so pairing a verdict to a conversation
    through it is guesswork. It is dropped at every depth."""

    def test_absent_at_every_depth(self, tools):
        for depth in ("summary", "detailed", "full"):
            metrics = _call(
                tools, n_rows=50, detail_level=depth, limit=5,
            )["test_run"]["model_metrics"]
            assert "scores_by_row" not in metrics
            assert "scores_by_label" not in metrics
            assert "row_level_metrics" not in metrics

    def test_aggregates_survive_at_summary(self, tools):
        # FR-007: the aggregates are what make summary useful.
        metrics = _call(tools, n_rows=50, limit=5)["test_run"]["model_metrics"]
        for key in (
            "mean_scores", "percentile_scores",
            "aggregate_check_metadata", "aggregate_baseline_metrics", "check_ids",
        ):
            assert key in metrics


class TestPerRowChecks:
    """041 FR-001: each row's own verdicts and judge explanations, correctly
    paired. This is the per-row source of truth after option A."""

    def test_absent_at_summary(self, tools):
        assert "checks" not in _call(tools, n_rows=5)["data_points"][0]

    def test_present_from_detailed_upward(self, tools):
        for depth in ("detailed", "full"):
            row = _call(tools, n_rows=5, detail_level=depth)["data_points"][0]
            assert row["checks"]["agent_managed_task"] is True
            assert "judge prose" in row["checks"][
                "agent_managed_task__explanation"
            ]

    def test_verdicts_are_paired_to_their_own_conversation(self, tools):
        """The property the run-level block could not provide."""
        out = _call(tools, n_rows=50, detail_level="detailed", limit=5, offset=10)
        for i, row in enumerate(out["data_points"]):
            assert row["checks"]["agent_managed_task__explanation"] == (
                f"judge prose for conversation {10 + i}"
            )

    def test_checks_are_paged_with_their_rows(self, tools):
        out = _call(tools, n_rows=50, detail_level="detailed", limit=5)
        assert len(out["data_points"]) == 5
        assert all("checks" in r for r in out["data_points"])

    def test_row_without_checks_is_tolerated(self, tools):
        okareo = MagicMock()
        okareo.api_key = "k"
        dps = _data_points(3)
        for dp in dps:
            dp.checks = None
        okareo.find_test_data_points.return_value = dps
        with patch("src.tools.tests.get_okareo_client", return_value=okareo), \
             patch("src.tools.tests.resolve_project",
                   return_value=ResolvedProject(id="proj-1", name="Demos", basis="explicit")), \
             patch("src.tools.tests.find_test_runs", MagicMock(return_value=[_run(3)])), \
             patch("src.okareo_client.get_targets_cached", return_value={}), \
             patch("src.okareo_client.get_scenarios_cached", return_value={}), \
             patch("src.okareo_client.get_drivers_cached", return_value={}):
            out = json.loads(tools["get_test_run_results"](
                test_run_id=RUN_ID, detail_level="detailed",
            ))
        assert out["data_points"][0]["checks"] is None


class TestGenerationOutputStaysOutOfLeanDepths:
    """metric_value can carry generation_output, which is the whole transcript."""

    def test_stripped_below_full(self, tools):
        for depth in ("summary", "detailed"):
            row = _call(tools, n_rows=3, detail_level=depth)["data_points"][0]
            assert "generation_output" not in row["metric_value"]

    def test_present_at_full(self, tools):
        row = _call(tools, n_rows=3, detail_level="full")["data_points"][0]
        assert "generation_output" in row["metric_value"]


class TestPaging:
    def test_default_page_is_bounded(self, tools):
        out = _call(tools, n_rows=50)
        assert out["data_point_count"] == 20
        assert out["total_count"] == 50
        assert out["has_more"] is True

    def test_limit_zero_still_returns_everything(self, tools):
        out = _call(tools, n_rows=50, limit=0)
        assert out["data_point_count"] == 50
        assert out["has_more"] is False

    def test_offset_beyond_total_empties_the_page(self, tools):
        out = _call(tools, n_rows=5, detail_level="detailed", offset=99)
        assert out["data_points"] == []
        assert out["has_more"] is False

    def test_next_step_names_the_follow_up_call(self, tools):
        out = _call(tools, n_rows=50)
        assert "get_test_run_results" in out["next_step"]
        assert "offset" in out["next_step"]


class TestSeedDataIsNeverTruncated:
    """FR-004: scenario input is structured, so a partial value drops fields
    rather than shortening a string. Whole value or nothing."""

    def test_scenario_input_is_intact_at_summary(self, tools):
        out = _call(tools, n_rows=3, detail_level="summary")
        seed = out["data_points"][0]["scenario_input"]
        assert set(seed) == {"sub_capability", "persona", "guidance", "driver"}
        assert seed["guidance"].endswith("order.' ")

    def test_scenario_result_is_intact_at_summary(self, tools):
        out = _call(tools, n_rows=3)
        assert out["data_points"][0]["scenario_result"] == "Expected outcome 0"

    def test_seed_survives_every_depth(self, tools):
        for depth in ("summary", "detailed", "full"):
            out = _call(tools, n_rows=3, detail_level=depth)
            assert out["data_points"][0]["scenario_input"]["persona"]


class TestTranscriptsGatedOnFull:
    def test_absent_below_full(self, tools):
        for depth in ("summary", "detailed"):
            out = _call(tools, n_rows=3, detail_level=depth)
            assert "model_input" not in out["data_points"][0]
            assert "model_result" not in out["data_points"][0]

    def test_present_at_full(self, tools):
        out = _call(tools, n_rows=3, detail_level="full")
        assert out["data_points"][0]["model_input"]
        assert out["data_points"][0]["model_result"]


class TestRunFieldSet:
    """FR-006: a defined set, not a raw passthrough."""

    def test_author_identity_present_at_every_depth(self, tools):
        # FR-006a: authorship is provenance -- it answers who produced a result.
        for depth in ("summary", "detailed", "full"):
            run = _call(tools, n_rows=3, detail_level=depth)["test_run"]
            assert run["author_email"] == "matt@okareo.com"
            assert run["author_name"] == "Matt Wyman"
            assert run["author_type"] == "userToken"

    def test_raw_passthrough_fields_are_dropped(self, tools):
        run = _call(tools, n_rows=3)["test_run"]
        for dropped in (
            "filter_group_id", "error_matrix", "failure_message", "progress",
        ):
            assert dropped not in run

    def test_identity_and_timings_retained(self, tools):
        run = _call(tools, n_rows=3)["test_run"]
        for kept in ("id", "name", "type", "status", "start_time", "end_time",
                     "test_data_point_count", "app_link", "simulation_params"):
            assert kept in run


class TestProvenance:
    """FR-025/FR-026: no listing surfaced these before this feature."""

    def test_names_and_ids_at_summary(self, tools):
        run = _call(tools, n_rows=3)["test_run"]
        assert run["target"]["name"] == "Pandora Voice"
        assert run["scenario"]["name"] == "confused-caller-set"
        assert run["driver"]["name"] == "confused-caller"
        assert run["target"]["id"] == "m1"

    def test_unresolvable_name_does_not_fail_the_call(self, tools):
        okareo = MagicMock()
        okareo.api_key = "k"
        okareo.find_test_data_points.return_value = _data_points(3)
        with patch("src.tools.tests.get_okareo_client", return_value=okareo), \
             patch("src.tools.tests.resolve_project",
                   return_value=ResolvedProject(id="proj-1", name="Demos", basis="explicit")), \
             patch("src.tools.tests.find_test_runs",
                   MagicMock(return_value=[_run(3)])), \
             patch("src.okareo_client.get_targets_cached", return_value={}), \
             patch("src.okareo_client.get_scenarios_cached", return_value={}), \
             patch("src.okareo_client.get_drivers_cached", return_value={}):
            out = json.loads(tools["get_test_run_results"](test_run_id=RUN_ID))
        assert out["test_run"]["target"] == {"id": "m1"}


class TestLookupParity:
    """research R3 / US1 scenario 6: name and id paths must agree."""

    def _by_name(self, tools, depth):
        okareo = MagicMock()
        okareo.api_key = "k"
        okareo.find_test_data_points.return_value = _data_points(5)
        calls = []

        def _find(_ok, payload):
            calls.append(payload)
            return [_run(5)]

        with patch("src.tools.tests.get_okareo_client", return_value=okareo), \
             patch("src.tools.tests.resolve_project",
                   return_value=ResolvedProject(id="proj-1", name="Demos", basis="explicit")), \
             patch("src.tools.tests.find_test_runs", _find), \
             patch("src.okareo_client.get_targets_cached", return_value={"m1": "Pandora Voice"}), \
             patch("src.okareo_client.get_scenarios_cached", return_value={"s1": "s"}), \
             patch("src.okareo_client.get_drivers_cached", return_value={"d1": "d"}):
            out = json.loads(tools["get_test_run_results"](
                name="REPS R - confused-caller", detail_level=depth,
            ))
        return out, calls

    def test_name_path_matches_id_path_shape(self, tools):
        by_name, _ = self._by_name(tools, "detailed")
        by_id = _call(tools, n_rows=5, detail_level="detailed")
        assert set(by_name["test_run"]) == set(by_id["test_run"])
        assert set(by_name) == set(by_id)

    def test_name_sweep_never_requests_row_metrics(self, tools):
        """The unbounded sweep must stay metrics-free or it 500s a big project."""
        _, calls = self._by_name(tools, "detailed")
        sweep = calls[0]
        assert sweep.return_model_metrics is False

    def test_name_path_refetches_by_id(self, tools):
        """The sweep matches the name; a second id-bounded fetch is what both
        paths actually answer from, which is why their shapes agree."""
        out, calls = self._by_name(tools, "detailed")
        assert len(calls) >= 2
        assert str(calls[-1].id) == RUN_ID
        assert out["test_run"]["id"] == RUN_ID


class TestDeprecatedAlias:
    """FR-010a / research R8."""

    def test_include_transcripts_true_serves_full(self, tools):
        out = _call(tools, n_rows=3, include_transcripts=True)
        assert out["data_points"][0]["model_input"]
        assert "deprecation" in out

    def test_deprecation_note_names_the_replacement(self, tools):
        out = _call(tools, n_rows=3, include_transcripts=True)
        assert "detail_level" in out["deprecation"]

    def test_detail_level_wins_when_both_supplied(self, tools):
        out = _call(
            tools, n_rows=3, include_transcripts=True, detail_level="summary",
        )
        assert "model_input" not in out["data_points"][0]
        assert "deprecation" in out

    def test_include_transcripts_false_is_not_an_alias(self, tools):
        out = _call(tools, n_rows=3, include_transcripts=False)
        assert "deprecation" not in out


class TestRerunBlock:
    """043 US6 FR-033: a run summary's action surface. The block an agent edits
    and sends, rather than translating prose config into tool parameters —
    which is the step it most often gets wrong (stop_check and augmentation are
    nested, and augmentation's composition rules reject most combinations)."""

    def test_present_at_summary(self, tools):
        out = _call(tools, n_rows=3)
        assert "rerun" in out
        assert "call" in out["rerun"]
        assert "inherited" in out["rerun"]

    def test_present_at_every_depth(self, tools):
        for depth in ("summary", "detailed", "full"):
            assert "rerun" in _call(tools, n_rows=3, detail_level=depth)

    def test_call_names_based_on_run_id(self, tools):
        call = _call(tools, n_rows=3)["rerun"]["call"]
        assert "run_simulation" in call
        assert f"based_on_run_id='{RUN_ID}'" in call

    def test_inherited_carries_the_recorded_config(self, tools):
        inherited = _call(tools, n_rows=3)["rerun"]["inherited"]
        assert inherited["max_turns"] == 10
        # The fixture records only max_turns, so repeats stays absent rather
        # than being claimed at its default (research R11a).
        assert "repeats" not in inherited

    def test_inherited_carries_the_check_list_as_names(self, tools):
        # Ready to pass straight to run_simulation's `checks` parameter.
        inherited = _call(tools, n_rows=3)["rerun"]["inherited"]
        assert inherited["checks"] == ["agent_managed_task"]

    def test_inherited_holds_only_run_simulation_parameters(self, tools):
        from src.run_config import RERUN_PARAM_FIELDS

        inherited = _call(tools, n_rows=3)["rerun"]["inherited"]
        assert set(inherited).issubset(set(RERUN_PARAM_FIELDS) | {"checks"})

    def test_note_explains_per_field_override(self, tools):
        note = _call(tools, n_rows=3)["rerun"]["note"]
        assert "override" in note.lower()

    def test_augmentation_appears_in_tool_parameter_shape(self, tools):
        okareo = MagicMock()
        okareo.api_key = "k"
        okareo.find_test_data_points.return_value = _data_points(2)
        run = _run(2)
        aug = {"noise": {"noise_profile": "cafeteria", "noise_snr_db": 10}}
        run["simulation_params"] = {
            "max_turns": 10, "augmentation": aug, "silence_timeout_ms": 10000,
        }
        with patch("src.tools.tests.get_okareo_client", return_value=okareo), \
             patch("src.tools.tests.resolve_project",
                   return_value=ResolvedProject(id="proj-1", name="Demos", basis="explicit")), \
             patch("src.tools.tests.find_test_runs", MagicMock(return_value=[run])), \
             patch("src.okareo_client.get_targets_cached", return_value={}), \
             patch("src.okareo_client.get_scenarios_cached", return_value={}), \
             patch("src.okareo_client.get_drivers_cached", return_value={}):
            out = json.loads(tools["get_test_run_results"](test_run_id=RUN_ID))
        inherited = out["rerun"]["inherited"]
        assert inherited["augmentation"] == aug
        assert inherited["silence_timeout_ms"] == 10000

    def test_unsettable_field_is_listed_not_offered(self, tools):
        okareo = MagicMock()
        okareo.api_key = "k"
        okareo.find_test_data_points.return_value = _data_points(2)
        run = _run(2)
        run["simulation_params"] = {
            "max_turns": 10, "concurrent_ask_probability": 0.4,
        }
        with patch("src.tools.tests.get_okareo_client", return_value=okareo), \
             patch("src.tools.tests.resolve_project",
                   return_value=ResolvedProject(id="proj-1", name="Demos", basis="explicit")), \
             patch("src.tools.tests.find_test_runs", MagicMock(return_value=[run])), \
             patch("src.okareo_client.get_targets_cached", return_value={}), \
             patch("src.okareo_client.get_scenarios_cached", return_value={}), \
             patch("src.okareo_client.get_drivers_cached", return_value={}):
            out = json.loads(tools["get_test_run_results"](test_run_id=RUN_ID))
        assert "concurrent_ask_probability" not in out["rerun"]["inherited"]
        assert any(
            u["field"] == "concurrent_ask_probability"
            for u in out["rerun"]["unavailable"]
        )

    def test_absent_recorded_field_is_omitted_not_defaulted(self, tools):
        """research R11a: the record is sparse between runs."""
        okareo = MagicMock()
        okareo.api_key = "k"
        okareo.find_test_data_points.return_value = _data_points(2)
        run = _run(2)
        run["simulation_params"] = {"max_turns": 7}
        with patch("src.tools.tests.get_okareo_client", return_value=okareo), \
             patch("src.tools.tests.resolve_project",
                   return_value=ResolvedProject(id="proj-1", name="Demos", basis="explicit")), \
             patch("src.tools.tests.find_test_runs", MagicMock(return_value=[run])), \
             patch("src.okareo_client.get_targets_cached", return_value={}), \
             patch("src.okareo_client.get_scenarios_cached", return_value={}), \
             patch("src.okareo_client.get_drivers_cached", return_value={}):
            out = json.loads(tools["get_test_run_results"](test_run_id=RUN_ID))
        inherited = out["rerun"]["inherited"]
        assert inherited["max_turns"] == 7
        for absent in ("repeats", "first_turn", "turn_transition_time"):
            assert absent not in inherited

    def test_checks_fall_back_to_data_point_metrics(self, tools):
        """research R9: check_ids is the primary source; derive from the rows
        when a run does not carry it."""
        okareo = MagicMock()
        okareo.api_key = "k"
        dps = _data_points(2)
        for dp in dps:
            dp.metric_value = {"derived_check": 1}
        okareo.find_test_data_points.return_value = dps
        run = _run(2)
        run["model_metrics"].pop("check_ids", None)
        with patch("src.tools.tests.get_okareo_client", return_value=okareo), \
             patch("src.tools.tests.resolve_project",
                   return_value=ResolvedProject(id="proj-1", name="Demos", basis="explicit")), \
             patch("src.tools.tests.find_test_runs", MagicMock(return_value=[run])), \
             patch("src.okareo_client.get_targets_cached", return_value={}), \
             patch("src.okareo_client.get_scenarios_cached", return_value={}), \
             patch("src.okareo_client.get_drivers_cached", return_value={}):
            out = json.loads(tools["get_test_run_results"](test_run_id=RUN_ID))
        assert out["rerun"]["inherited"]["checks"] == ["derived_check"]
