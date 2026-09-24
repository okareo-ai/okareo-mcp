"""044: every error string documented in the voice_augmentations template's
"Common errors" table must be what the validator actually returns, so the
guidance an agent reads cannot drift from the code."""

import re
from pathlib import Path

import pytest

from src.voice_augmentation import preflight_augmentation

TEMPLATE = Path(__file__).resolve().parents[2] / "src/templates/voice_augmentations.md"

# Documented error -> a block that triggers it (max_turns=5).
TRIGGERS = {
    "Unknown augmentation strategy '<x>'. Known: [...].": {"echo": {}},
    "Unsupported augmentation combination: barge_in, cap. Only noise + one other strategy is composable.":
        {"cap": {"probability": 0.3}, "barge_in": {"prompt": "x"}},
    "Invalid cap.probability=1.5. Must be in [0.0, 1.0].": {"cap": {"probability": 1.5}},
    "cap.probability is required.": {"cap": {"pause_ms": 800}},
    "Invalid barge_in.max_offset_ms=100: must be >= min_offset_ms (200).":
        {"barge_in": {"prompt": "x", "min_offset_ms": 200, "max_offset_ms": 100}},
    "Unknown field cap.start_at_turn. cap accepts: ['pause_ms', 'probability'].":
        {"cap": {"probability": 0.3, "start_at_turn": 2}},
    "barge_in.utterance is published by the SDK but ignored by Okareo. Use barge_in.prompt to steer what the interruption says.":
        {"barge_in": {"prompt": "x", "utterance": "wait"}},
    "Set either noise.profile or noise.noise_profile, not both.":
        {"noise": {"profile": "a", "noise_profile": "b", "snr_db": 10}},
    "Invalid dropout.start_at_turn=0. Must be an int >= 1 (turn 0 is the agent's greeting).":
        {"dropout": {"probability": 0.3, "start_at_turn": 0}},
    "augmentation.dropout.start_at_turn=6 is beyond max_turns=5, so dropout would never fire. Lower start_at_turn or raise max_turns.":
        {"dropout": {"probability": 0.3, "start_at_turn": 6}},
    "Invalid dropout.seed='abc'. Must be an int or null.":
        {"dropout": {"probability": 0.3, "seed": "abc"}},
}

# Raised by run_simulation after it has looked up the Target, not by the
# block validator, so it has no block that triggers it here.
NOT_FROM_PREFLIGHT = {
    "Augmentations apply only to voice Targets. Target 'X' is of type 'custom_endpoint'.",
}


def _documented_errors() -> list[str]:
    text = TEMPLATE.read_text()
    section = text.split("## Common errors", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"^\| `(.+?)` \|", section, flags=re.MULTILINE)


def _as_pattern(documented: str) -> re.Pattern:
    escaped = re.escape(documented)
    escaped = escaped.replace(re.escape("<x>"), ".+").replace(re.escape("[...]"), r"\[.*\]")
    return re.compile(f"^{escaped}$")


def test_every_documented_error_has_a_trigger():
    documented = set(_documented_errors()) - NOT_FROM_PREFLIGHT
    assert documented == set(TRIGGERS), (
        f"Documented but not tested: {sorted(documented - set(TRIGGERS))}; "
        f"tested but not documented: {sorted(set(TRIGGERS) - documented)}"
    )


@pytest.mark.parametrize("documented", sorted(TRIGGERS))
def test_documented_error_matches_validator_output(documented):
    err = preflight_augmentation(TRIGGERS[documented], 5)
    assert err is not None, f"no error for {TRIGGERS[documented]}"
    assert _as_pattern(documented).match(err["error"]), (
        f"documented: {documented!r}\nactual:     {err['error']!r}"
    )
