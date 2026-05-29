"""Voice-simulation augmentation helpers (spec 023-tool-fixes US4–US8).

Bridges the gap between the MCP and the okareo SDK 0.0.132, which does
not expose the backend's `augmentation` block or `silence_timeout_ms`
on its `Simulation` dataclass. We subclass `Simulation` and emit the
extras in `to_dict()`; the openapi-generated
`TestRunPayloadV2SimulationParamsType0` preserves them via
`additional_properties` (see specs/023-tool-fixes/research.md R1).

Also provides pure-function preflight validators for the augmentation
block: composition rule, per-strategy required-field / range checks,
unknown-key detection, and offset ordering. All validators return a
list of error envelopes — empty list means pass.
"""

from __future__ import annotations

from typing import Any, Optional

from attrs import define as _attrs_define
from okareo.model_under_test import Simulation

# ---------------------------------------------------------------------------
# AugmentedSimulation — SDK bridge for the new payload keys
# ---------------------------------------------------------------------------

@_attrs_define
class AugmentedSimulation(Simulation):
    """A `Simulation` that also emits `augmentation` and `silence_timeout_ms`
    in `to_dict()`. The openapi client preserves these as
    `additional_properties`, so they reach the backend untouched.
    """

    augmentation: Optional[dict] = None
    silence_timeout_ms: Optional[int] = None

    def to_dict(self) -> dict:
        d = super().to_dict()
        if self.augmentation is not None:
            d["augmentation"] = self.augmentation
        if self.silence_timeout_ms is not None:
            d["silence_timeout_ms"] = self.silence_timeout_ms
        return d


# ---------------------------------------------------------------------------
# Constants for validators
# ---------------------------------------------------------------------------

KNOWN_STRATEGIES: tuple[str, ...] = (
    "cap",
    "directed_speech",
    "secondary_speaker",
    "backchannel",
    "barge_in",
    "noise",
)


def _err(strategy: str, field: str, message: str) -> dict:
    """Build a structured error envelope used by every validator."""
    return {
        "error": message,
        "field": f"augmentation.{strategy}.{field}" if field else f"augmentation.{strategy}",
        "strategy": strategy,
    }


def _is_real_number(value: Any) -> bool:
    """True for int / float (excluding bool, which inherits from int)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _in_range(
    value: Any,
    lo: float,
    hi: float,
    *,
    inclusive: bool = True,
) -> bool:
    if not _is_real_number(value):
        return False
    if inclusive:
        return lo <= value <= hi
    return lo < value < hi


# ---------------------------------------------------------------------------
# Composition rule — US5 (FR-018, FR-020)
# ---------------------------------------------------------------------------

def validate_composition(augmentation: dict) -> list[str]:
    """Return the list of conflicting non-noise strategy names.

    Empty list = no conflict (valid: empty dict, noise-only, or exactly
    one non-noise +/- noise).
    """
    if not isinstance(augmentation, dict):
        return []
    non_noise = [k for k in augmentation.keys() if k != "noise" and k in KNOWN_STRATEGIES]
    if len(non_noise) > 1:
        return sorted(non_noise)
    return []


# ---------------------------------------------------------------------------
# Unknown-key detection — US6 (FR-018 edge case)
# ---------------------------------------------------------------------------

def validate_known_keys(augmentation: dict) -> list[dict]:
    """Return one error per unknown top-level key."""
    if not isinstance(augmentation, dict):
        return []
    return [
        {
            "error": (
                f"Unknown augmentation strategy '{k}'. "
                f"Known: {list(KNOWN_STRATEGIES)}."
            ),
            "field": f"augmentation.{k}",
            "strategy": k,
            "known": list(KNOWN_STRATEGIES),
        }
        for k in augmentation.keys()
        if k not in KNOWN_STRATEGIES
    ]


# ---------------------------------------------------------------------------
# Per-strategy validators — US6 (FR-021, FR-022, FR-023)
# ---------------------------------------------------------------------------

def _validate_probability(strategy: str, config: dict) -> list[dict]:
    if "probability" not in config:
        return [_err(strategy, "probability", f"{strategy}.probability is required.")]
    p = config["probability"]
    if not _in_range(p, 0.0, 1.0):
        return [_err(
            strategy,
            "probability",
            f"Invalid {strategy}.probability={p!r}. Must be in [0.0, 1.0].",
        )]
    return []


def _validate_lpf_gain_sr(strategy: str, config: dict) -> list[dict]:
    errors: list[dict] = []
    if "lpf_cutoff_hz" in config:
        v = config["lpf_cutoff_hz"]
        if not (_is_real_number(v) and v > 0):
            errors.append(_err(
                strategy,
                "lpf_cutoff_hz",
                f"Invalid {strategy}.lpf_cutoff_hz={v!r}. Must be > 0.",
            ))
    if "gain_db" in config:
        v = config["gain_db"]
        if not _in_range(v, -40.0, 0.0):
            errors.append(_err(
                strategy,
                "gain_db",
                f"Invalid {strategy}.gain_db={v!r}. Must be in [-40.0, 0.0].",
            ))
    if "sample_rate" in config:
        v = config["sample_rate"]
        if not (_is_real_number(v) and v > 0):
            errors.append(_err(
                strategy,
                "sample_rate",
                f"Invalid {strategy}.sample_rate={v!r}. Must be > 0.",
            ))
    return errors


def _validate_offsets(strategy: str, config: dict) -> list[dict]:
    """min/max_offset_ms ordering + non-negativity (FR-023)."""
    errors: list[dict] = []
    min_v = config.get("min_offset_ms")
    max_v = config.get("max_offset_ms")
    if min_v is not None:
        if not (_is_real_number(min_v) and min_v >= 0):
            errors.append(_err(
                strategy,
                "min_offset_ms",
                f"Invalid {strategy}.min_offset_ms={min_v!r}. Must be >= 0.",
            ))
    if max_v is not None:
        if not (_is_real_number(max_v) and max_v >= 0):
            errors.append(_err(
                strategy,
                "max_offset_ms",
                f"Invalid {strategy}.max_offset_ms={max_v!r}. Must be >= 0.",
            ))
    if (
        _is_real_number(min_v)
        and _is_real_number(max_v)
        and max_v < min_v
    ):
        errors.append(_err(
            strategy,
            "max_offset_ms",
            (
                f"Invalid {strategy}.max_offset_ms={max_v!r}: must be >= "
                f"min_offset_ms ({min_v!r})."
            ),
        ))
    return errors


def validate_cap(config: dict) -> list[dict]:
    s = "cap"
    errors = _validate_probability(s, config)
    if "pause_ms" in config:
        v = config["pause_ms"]
        if not _in_range(v, 0, 10000) or isinstance(v, float):
            errors.append(_err(
                s,
                "pause_ms",
                f"Invalid cap.pause_ms={v!r}. Must be an int in [0, 10000].",
            ))
    return errors


def validate_directed_speech(config: dict) -> list[dict]:
    s = "directed_speech"
    return _validate_probability(s, config) + _validate_lpf_gain_sr(s, config)


def validate_secondary_speaker(config: dict) -> list[dict]:
    s = "secondary_speaker"
    errors = _validate_probability(s, config)
    voice = config.get("secondary_voice")
    if not isinstance(voice, str) or not voice:
        errors.append(_err(
            s,
            "secondary_voice",
            f"{s}.secondary_voice is required and must be a non-empty string.",
        ))
    if "inter_speaker_pause_ms" in config:
        v = config["inter_speaker_pause_ms"]
        if not _in_range(v, 0, 5000) or isinstance(v, float):
            errors.append(_err(
                s,
                "inter_speaker_pause_ms",
                (
                    f"Invalid {s}.inter_speaker_pause_ms={v!r}. "
                    "Must be an int in [0, 5000]."
                ),
            ))
    errors.extend(_validate_lpf_gain_sr(s, config))
    return errors


def _require_non_empty_string(strategy: str, field: str, config: dict) -> list[dict]:
    v = config.get(field)
    if not isinstance(v, str) or not v:
        return [_err(
            strategy,
            field,
            f"{strategy}.{field} is required and must be a non-empty string.",
        )]
    return []


def validate_backchannel(config: dict) -> list[dict]:
    s = "backchannel"
    errors = _require_non_empty_string(s, "utterance", config)
    if "probability" in config:
        # Optional with default 0.35 — only validate if present.
        v = config["probability"]
        if not _in_range(v, 0.0, 1.0):
            errors.append(_err(
                s,
                "probability",
                f"Invalid {s}.probability={v!r}. Must be in [0.0, 1.0].",
            ))
    errors.extend(_validate_offsets(s, config))
    return errors


def validate_barge_in(config: dict) -> list[dict]:
    s = "barge_in"
    errors = _require_non_empty_string(s, "prompt", config)
    if "probability" in config:
        v = config["probability"]
        if not _in_range(v, 0.0, 1.0):
            errors.append(_err(
                s,
                "probability",
                f"Invalid {s}.probability={v!r}. Must be in [0.0, 1.0].",
            ))
    errors.extend(_validate_offsets(s, config))
    return errors


def validate_noise(config: dict) -> list[dict]:
    s = "noise"
    errors: list[dict] = []
    profile = config.get("noise_profile")
    if not isinstance(profile, str) or not profile:
        errors.append(_err(
            s,
            "noise_profile",
            f"{s}.noise_profile is required and must be a non-empty string.",
        ))
    if "noise_snr_db" not in config:
        errors.append(_err(
            s,
            "noise_snr_db",
            f"{s}.noise_snr_db is required.",
        ))
    elif not _is_real_number(config["noise_snr_db"]):
        errors.append(_err(
            s,
            "noise_snr_db",
            (
                f"Invalid {s}.noise_snr_db={config['noise_snr_db']!r}. "
                "Must be a number."
            ),
        ))
    return errors


_STRATEGY_VALIDATORS = {
    "cap": validate_cap,
    "directed_speech": validate_directed_speech,
    "secondary_speaker": validate_secondary_speaker,
    "backchannel": validate_backchannel,
    "barge_in": validate_barge_in,
    "noise": validate_noise,
}


# ---------------------------------------------------------------------------
# Full preflight chain — US6 (FR-026)
# ---------------------------------------------------------------------------

def validate_augmentation(
    augmentation: dict,
) -> list[dict]:
    """Run the full validation chain in the order documented in
    contracts/run_simulation.contract.md:

    1. unknown-keys
    2. per-strategy required fields + ranges + offset ordering

    Returns the list of all error envelopes encountered (empty if pass).
    Composition rule is checked separately by `validate_composition` so
    the caller can format that error differently.
    """
    if not isinstance(augmentation, dict) or not augmentation:
        return []

    errors: list[dict] = []
    errors.extend(validate_known_keys(augmentation))

    for key, config in augmentation.items():
        validator = _STRATEGY_VALIDATORS.get(key)
        if validator is None:
            # Already flagged by validate_known_keys above.
            continue
        if not isinstance(config, dict):
            errors.append({
                "error": (
                    f"augmentation.{key} must be a JSON object/dict "
                    "with the strategy's fields."
                ),
                "field": f"augmentation.{key}",
                "strategy": key,
            })
            continue
        errors.extend(validator(config))

    return errors
