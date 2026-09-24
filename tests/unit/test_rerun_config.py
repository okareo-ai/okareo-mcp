"""The run-configuration resolver (043 US6).

One mapping, read from two directions: what `based_on_run_id` will inherit, and
what a run summary's `rerun` block displays. Two implementations would drift,
and the drift would be invisible — the block's whole job is to predict the
inheritance, so nothing about reading either one would reveal a mismatch.
"""

from src.run_config import RERUN_PARAM_FIELDS, resolve_run_config


def _run(**params):
    """A run record carrying the given simulation_params."""
    return {
        "id": "run-1",
        "name": "nightly",
        "simulation_params": dict(params),
    }


class TestFieldsAreMappedToToolParameters:
    def test_core_knobs_pass_through(self):
        out = resolve_run_config(
            _run(repeats=2, max_turns=10, first_turn="driver"), []
        )
        assert out["params"]["repeats"] == 2
        assert out["params"]["max_turns"] == 10
        assert out["params"]["first_turn"] == "driver"

    def test_stop_check_keeps_its_nested_shape(self):
        stop = {"check_name": "agent_managed_task", "stop_on": False}
        out = resolve_run_config(_run(stop_check=stop), [])
        assert out["params"]["stop_check"] == stop

    def test_augmentation_passes_through_verbatim(self):
        # research R11, measured on a live voice run: the nested block comes
        # back exactly as sent, so "re-run with different noise" is an edit.
        aug = {"noise": {"noise_profile": "cafeteria", "noise_snr_db": 10}}
        out = resolve_run_config(_run(augmentation=aug), [])
        assert out["params"]["augmentation"] == aug

    def test_silence_timeout_passes_through(self):
        out = resolve_run_config(_run(silence_timeout_ms=10000), [])
        assert out["params"]["silence_timeout_ms"] == 10000

    def test_checks_come_from_the_caller_not_simulation_params(self):
        out = resolve_run_config(_run(), ["agent_managed_task", "latency"])
        assert out["params"]["checks"] == ["agent_managed_task", "latency"]

    def test_every_emitted_key_is_a_run_simulation_parameter(self):
        out = resolve_run_config(
            _run(
                repeats=1, max_turns=5, first_turn="target",
                stop_check={"check_name": "c", "stop_on": True},
                checks_at_every_turn=True, turn_transition_time=500,
                augmentation={"noise": {}}, silence_timeout_ms=1,
            ),
            ["c"],
        )
        assert set(out["params"]).issubset(set(RERUN_PARAM_FIELDS) | {"checks"})


class TestSparseRecords:
    """research R11a: the recorded field set differs between runs, because only
    the knobs actually set get recorded. A missing field is not a field set to
    its default, and must not be asserted as one."""

    def test_absent_field_is_omitted_not_defaulted(self):
        out = resolve_run_config(_run(max_turns=10), [])
        assert "max_turns" in out["params"]
        for absent in (
            "checks_at_every_turn", "turn_transition_time", "augmentation",
            "silence_timeout_ms", "first_turn", "repeats",
        ):
            assert absent not in out["params"]

    def test_a_run_with_no_simulation_params_yields_empty_params(self):
        out = resolve_run_config({"id": "run-1"}, [])
        assert out["params"] == {}
        assert out["unavailable"] == []

    def test_null_stop_check_is_carried_as_an_explicit_none(self):
        # `stop_check: null` appears alongside populated fields on real runs,
        # so it means "this run had none", not "unknown" (research R11).
        out = resolve_run_config(_run(stop_check=None), [])
        assert "stop_check" in out["params"]
        assert out["params"]["stop_check"] is None

    def test_empty_check_list_is_omitted(self):
        assert "checks" not in resolve_run_config(_run(), [])["params"]

    def test_object_style_run_record_is_read(self):
        class Run:
            simulation_params = {"max_turns": 7}

        assert resolve_run_config(Run(), [])["params"]["max_turns"] == 7


class TestUnsettableFields:
    """research R10: a field the backend records but `run_simulation` cannot
    set must be reported, never emitted — a block that offers it produces a
    call the tool rejects, and one that hides it produces a run that differs
    from its parent invisibly."""

    def test_concurrent_ask_probability_is_reported_not_emitted(self):
        out = resolve_run_config(_run(concurrent_ask_probability=0.4), [])
        assert "concurrent_ask_probability" not in out["params"]
        entry = next(
            u for u in out["unavailable"]
            if u["field"] == "concurrent_ask_probability"
        )
        assert entry["value"] == 0.4
        assert entry["reason"]

    def test_unknown_recorded_field_is_reported(self):
        out = resolve_run_config(_run(some_future_knob="x"), [])
        assert any(u["field"] == "some_future_knob" for u in out["unavailable"])

    def test_default_valued_unsettable_field_is_not_reported(self):
        # Recorded at its default it tells nobody anything, and reporting it on
        # every run would train a reader to ignore the list.
        out = resolve_run_config(_run(concurrent_ask_probability=0.0), [])
        assert out["unavailable"] == []

    def test_unavailable_is_empty_when_everything_is_settable(self):
        out = resolve_run_config(_run(max_turns=5, repeats=1), [])
        assert out["unavailable"] == []


class TestDriftBetweenTheBlockAndInheritance:
    """The property that makes the displayed block a promise rather than a
    rendering: what it shows is exactly what a re-run applies."""

    def test_resolution_is_idempotent_through_a_round_trip(self):
        original = _run(
            repeats=3, max_turns=12, first_turn="driver",
            stop_check={"check_name": "c", "stop_on": False},
            augmentation={"noise": {"noise_profile": "cafeteria"}},
            silence_timeout_ms=9000,
        )
        first = resolve_run_config(original, ["c1", "c2"])

        # Feed the block's own values back in as if they were a recorded run.
        echoed = {"simulation_params": {
            k: v for k, v in first["params"].items() if k != "checks"
        }}
        second = resolve_run_config(echoed, first["params"]["checks"])

        assert second["params"] == first["params"]
