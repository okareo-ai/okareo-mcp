"""Verify AugmentedSimulation.to_dict() round-trips through the SDK's
openapi client without losing the augmentation block or silence_timeout_ms
(spec 023-tool-fixes US4 / research.md R1).
"""

# Aliased away from the ``Test*`` prefix so pytest does not try to collect
# this SDK data model as a test class (PytestCollectionWarning).
from okareo_api_client.models.test_run_payload_v2_simulation_params_type_0 import (
    TestRunPayloadV2SimulationParamsType0 as RunPayloadV2SimParams,
)
from okareo.model_under_test import StopConfig

from src.voice_augmentation import AugmentedSimulation


def test_augmentation_block_survives_openapi_round_trip() -> None:
    sim = AugmentedSimulation(
        augmentation={
            "barge_in": {
                "prompt": "Interrupt politely.",
                "probability": 0.2,
                "min_offset_ms": 200,
                "max_offset_ms": 600,
            },
            "noise": {"noise_profile": "cafeteria", "noise_snr_db": 10},
        },
        silence_timeout_ms=8000,
    )
    payload = RunPayloadV2SimParams.from_dict(sim.to_dict())
    out = payload.to_dict()

    assert "augmentation" in out
    assert out["augmentation"]["barge_in"]["probability"] == 0.2
    assert out["augmentation"]["noise"]["noise_profile"] == "cafeteria"
    assert out["silence_timeout_ms"] == 8000


def test_omitted_extras_do_not_pollute_payload() -> None:
    """Bare AugmentedSimulation (no augmentation, no silence_timeout_ms)
    emits a payload identical to plain Simulation in spirit — the new
    keys do NOT appear.
    """
    sim = AugmentedSimulation()
    d = sim.to_dict()
    assert "augmentation" not in d
    assert "silence_timeout_ms" not in d


def test_inherits_parent_simulation_fields() -> None:
    """The new subclass still emits all the legacy Simulation fields."""
    sim = AugmentedSimulation(
        repeats=3,
        max_turns=10,
        first_turn="driver",
        checks_at_every_turn=True,
        turn_transition_time=2000,
        stop_check=StopConfig(check_name="red_flag", stop_on=True),
    )
    d = sim.to_dict()
    assert d["repeats"] == 3
    assert d["max_turns"] == 10
    assert d["first_turn"] == "driver"
    assert d["checks_at_every_turn"] is True
    assert d["turn_transition_time"] == 2000
    assert d["stop_check"] is not None
