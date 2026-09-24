"""044 US5: keep the MCP's augmentation settings and the pinned SDK's in step.

The MCP builds the augmentation block itself and checks it against what the
Okareo server applies (ALLOWED_FIELDS), so nothing forces it to follow the SDK.
This compares the two in both directions. Every known difference is listed
with its reason; a new difference, or a listed one that has gone away, fails
and names the strategy and setting. The SDK is imported here only — the MCP
server never imports okareo.augmentations.
"""

import attrs
import pytest
from okareo import augmentations as sdk_aug

from src.voice_augmentation import (
    ALLOWED_FIELDS,
    KNOWN_STRATEGIES,
    SDK_IGNORED_BY_SERVER,
)

_SDK_CLASSES = {
    "cap": "CAPAugmentation",
    "directed_speech": "DirectedSpeechAugmentation",
    "noise": "NoiseAugmentation",
    "secondary_speaker": "SecondarySpeakerAugmentation",
    "backchannel": "BackchannelAugmentation",
    "barge_in": "BargeInAugmentation",
    "dropout": "DropoutAugmentation",
}

# Settings the Okareo server applies that the SDK does not publish.
SERVER_ONLY: dict[str, dict[str, str]] = {
    "cap": {},
    "directed_speech": {
        "sample_rate": "Okareo server audio setting; not in the SDK",
        "reverb_preset": "Okareo server audio setting; not in the SDK",
    },
    "secondary_speaker": {
        "secondary_voice": "canonical Okareo server name; SDK publishes alias `voice`",
        "secondary_prompt": "canonical Okareo server name; SDK publishes alias `prompt`",
        "secondary_reverb_preset": "canonical Okareo server name; alias `reverb_preset`",
        "reverb_preset": "alias the Okareo server normalizes; not in the SDK",
        "secondary_voice_instructions": "Okareo server TTS setting; not in the SDK",
        "sample_rate": "Okareo server audio setting; not in the SDK",
    },
    "backchannel": {"seed": "Okareo server reproducibility knob"},
    "barge_in": {"seed": "Okareo server reproducibility knob"},
    "dropout": {"seed": "Okareo server reproducibility knob"},
    "noise": {
        "noise_profile": "canonical Okareo server name; SDK publishes alias `profile`",
        "noise_snr_db": "canonical Okareo server name; SDK publishes alias `snr_db`",
        "seed": "Okareo server reproducibility knob",
    },
}


def _sdk_fields() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for field in attrs.fields(sdk_aug.Augmentation):
        class_name = _SDK_CLASSES.get(field.name)
        assert class_name, (
            f"SDK Augmentation has a strategy '{field.name}' this test cannot "
            "map to a class; add it to _SDK_CLASSES and to the MCP."
        )
        cls = getattr(sdk_aug, class_name)
        out[field.name] = {f.name for f in attrs.fields(cls)}
    return out


SDK_FIELDS = _sdk_fields()


def test_strategy_sets_match():
    sdk, mcp = set(SDK_FIELDS), set(KNOWN_STRATEGIES)
    assert sdk == mcp, (
        f"Only in the SDK: {sorted(sdk - mcp)}; only in the MCP: {sorted(mcp - sdk)}"
    )


@pytest.mark.parametrize("strategy", sorted(SDK_FIELDS))
def test_sdk_fields_are_accepted_or_listed_ignored(strategy):
    unaccepted = SDK_FIELDS[strategy] - ALLOWED_FIELDS[strategy]
    listed = set(SDK_IGNORED_BY_SERVER.get(strategy, {}))
    assert unaccepted == listed, (
        f"{strategy}: the SDK publishes {sorted(unaccepted - listed)} which the MCP "
        f"neither accepts nor lists in SDK_IGNORED_BY_SERVER; "
        f"stale SDK_IGNORED_BY_SERVER entries: {sorted(listed - unaccepted)}"
    )


@pytest.mark.parametrize("strategy", sorted(SDK_FIELDS))
def test_server_only_fields_are_listed(strategy):
    extra = ALLOWED_FIELDS[strategy] - SDK_FIELDS[strategy]
    listed = set(SERVER_ONLY.get(strategy, {}))
    assert extra == listed, (
        f"{strategy}: the MCP accepts {sorted(extra - listed)} which the SDK does "
        f"not publish and SERVER_ONLY does not list; stale SERVER_ONLY "
        f"entries: {sorted(listed - extra)}"
    )
