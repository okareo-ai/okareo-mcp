"""E5 (spec 032): target reply timeout (silence_timeout_ms) guidance.

Both documentation surfaces — the run_simulation docstring and the
voice_augmentations template — carry identical wording, and a directed value
is forwarded unchanged.
"""

import re
from pathlib import Path

import pytest

# The canonical guidance sentences (FR-024..FR-026). The test normalizes
# whitespace, so wrapping differences between surfaces don't matter.
CANONICAL_SENTENCES = [
    "the target reply timeout",
    "how patient Okareo is before indicating that the target can't respond",
    "Do NOT set or change this value unless the user specifically directs it",
    "it should be 10000 ms in nearly all cases",
    "It exists to accommodate untuned targets with very long tool calls, "
    "during which the Driver waits patiently",
    "It does NOT change how fast Okareo responds, and lowering it does not "
    "speed anything up",
    "a slow simulation is not a reason to change it",
]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()


def _surfaces():
    import src.tools.simulations as sims

    # The tool function is registered via decorator; read the module source
    # for the parameter doc (the docstring lives inside run_simulation).
    source = Path(sims.__file__).read_text()
    template = Path(
        Path(sims.__file__).parent.parent / "templates" / "voice_augmentations.md"
    ).read_text()
    return _normalize(source), _normalize(template)


class TestIdenticalWording:
    @pytest.mark.parametrize("sentence", CANONICAL_SENTENCES)
    def test_sentence_present_on_both_surfaces(self, sentence):
        source, template = _surfaces()
        needle = _normalize(sentence)
        assert needle in source, f"missing in run_simulation docs: {sentence}"
        assert needle in template, f"missing in voice_augmentations.md: {sentence}"


class TestDirectedValueForwarded:
    def test_directed_value_survives_payload_round_trip(self):
        """FR-027: a directed silence_timeout_ms is emitted unchanged in the
        simulation params. The tool-level threading (parameter → run_test) is
        covered by tests/integration/test_run_simulation_augmented.py."""
        from okareo_api_client.models.test_run_payload_v2_simulation_params_type_0 import (  # noqa: E501
            TestRunPayloadV2SimulationParamsType0 as RunPayloadV2SimParams,
        )

        from src.voice_augmentation import AugmentedSimulation

        sim = AugmentedSimulation(silence_timeout_ms=12000)
        emitted = RunPayloadV2SimParams.from_dict(sim.to_dict()).to_dict()
        assert emitted["silence_timeout_ms"] == 12000
