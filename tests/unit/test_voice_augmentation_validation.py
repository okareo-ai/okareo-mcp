"""Unit tests for src/voice_augmentation.py validators (spec 023-tool-fixes
US5, US6).

Covers:
- validate_composition (T021)
- validate_known_keys + per-strategy validators (T027)
- Error envelope shape (T028)
"""

import pytest

from src.voice_augmentation import (
    KNOWN_STRATEGIES,
    preflight_augmentation,
    validate_augmentation,
    validate_barge_in,
    validate_backchannel,
    validate_cap,
    validate_composition,
    validate_directed_speech,
    validate_dropout,
    validate_known_keys,
    validate_noise,
    validate_secondary_speaker,
)


# ---------------------------------------------------------------------------
# T021 — Composition rule
# ---------------------------------------------------------------------------

class TestComposition:
    def test_empty_dict_is_no_conflict(self):
        assert validate_composition({}) == []

    def test_noise_only_is_no_conflict(self):
        assert validate_composition({"noise": {"noise_profile": "cafeteria"}}) == []

    def test_single_non_noise_is_no_conflict(self):
        assert validate_composition({"cap": {"probability": 0.3}}) == []

    def test_single_non_noise_plus_noise_is_no_conflict(self):
        assert validate_composition(
            {"barge_in": {"prompt": "hi"}, "noise": {"noise_profile": "cafeteria"}}
        ) == []

    def test_two_non_noise_returns_conflict_naming_both(self):
        conflicts = validate_composition({"cap": {}, "barge_in": {}})
        assert set(conflicts) == {"cap", "barge_in"}

    def test_three_non_noise_with_noise_still_lists_only_non_noise(self):
        conflicts = validate_composition(
            {
                "cap": {},
                "secondary_speaker": {},
                "noise": {"noise_profile": "cafeteria"},
            }
        )
        assert set(conflicts) == {"cap", "secondary_speaker"}
        assert "noise" not in conflicts


# ---------------------------------------------------------------------------
# T027 — Per-strategy + known-key validators
# ---------------------------------------------------------------------------

class TestKnownKeys:
    def test_unknown_key_flagged(self):
        errors = validate_known_keys({"echo": {}})
        assert len(errors) == 1
        assert errors[0]["strategy"] == "echo"
        assert errors[0]["known"] == list(KNOWN_STRATEGIES)

    def test_all_known_keys_accepted(self):
        errors = validate_known_keys(
            {k: {} for k in KNOWN_STRATEGIES}
        )
        assert errors == []


class TestCapValidator:
    def test_missing_probability_required(self):
        errors = validate_cap({})
        assert len(errors) == 1
        assert errors[0]["field"] == "augmentation.cap.probability"
        assert errors[0]["strategy"] == "cap"
        assert "required" in errors[0]["error"]

    def test_probability_out_of_range_high(self):
        errors = validate_cap({"probability": 1.5})
        assert errors[0]["field"] == "augmentation.cap.probability"
        assert "[0.0, 1.0]" in errors[0]["error"]

    def test_probability_out_of_range_low(self):
        errors = validate_cap({"probability": -0.1})
        assert errors[0]["field"] == "augmentation.cap.probability"

    def test_probability_at_boundaries_accepted(self):
        assert validate_cap({"probability": 0.0}) == []
        assert validate_cap({"probability": 1.0}) == []

    def test_pause_ms_out_of_range(self):
        errors = validate_cap({"probability": 0.3, "pause_ms": 20000})
        assert any(e["field"] == "augmentation.cap.pause_ms" for e in errors)


class TestDirectedSpeechValidator:
    def test_missing_probability(self):
        errors = validate_directed_speech({})
        assert any(e["field"] == "augmentation.directed_speech.probability" for e in errors)

    def test_gain_db_out_of_range_positive(self):
        errors = validate_directed_speech({"probability": 0.3, "gain_db": 5.0})
        assert any(e["field"] == "augmentation.directed_speech.gain_db" for e in errors)
        assert any("[-40.0, 0.0]" in e["error"] for e in errors)

    def test_lpf_cutoff_must_be_positive(self):
        errors = validate_directed_speech({"probability": 0.3, "lpf_cutoff_hz": 0})
        assert any(e["field"] == "augmentation.directed_speech.lpf_cutoff_hz" for e in errors)


class TestSecondarySpeakerValidator:
    def test_missing_secondary_voice(self):
        errors = validate_secondary_speaker({"probability": 0.3})
        assert any(e["field"] == "augmentation.secondary_speaker.secondary_voice" for e in errors)

    def test_empty_secondary_voice_rejected(self):
        errors = validate_secondary_speaker({"probability": 0.3, "secondary_voice": ""})
        assert any(e["field"] == "augmentation.secondary_speaker.secondary_voice" for e in errors)

    def test_inter_speaker_pause_ms_out_of_range(self):
        errors = validate_secondary_speaker(
            {"probability": 0.3, "secondary_voice": "x", "inter_speaker_pause_ms": 99999}
        )
        assert any(e["field"] == "augmentation.secondary_speaker.inter_speaker_pause_ms" for e in errors)


class TestBackchannelValidator:
    def test_missing_utterance(self):
        errors = validate_backchannel({})
        assert any(e["field"] == "augmentation.backchannel.utterance" for e in errors)

    def test_swapped_offsets_rejected(self):
        # T025: max_offset_ms < min_offset_ms
        errors = validate_backchannel(
            {"utterance": "mm", "min_offset_ms": 500, "max_offset_ms": 200}
        )
        assert any(
            e["field"] == "augmentation.backchannel.max_offset_ms"
            and "min_offset_ms" in e["error"]
            for e in errors
        )

    def test_equal_offsets_accepted(self):
        errors = validate_backchannel(
            {"utterance": "mm", "min_offset_ms": 200, "max_offset_ms": 200}
        )
        assert errors == []


class TestBargeInValidator:
    def test_missing_prompt(self):
        errors = validate_barge_in({})
        assert any(e["field"] == "augmentation.barge_in.prompt" for e in errors)

    def test_swapped_offsets_rejected(self):
        errors = validate_barge_in(
            {"prompt": "Interrupt.", "min_offset_ms": 600, "max_offset_ms": 100}
        )
        assert any(
            e["field"] == "augmentation.barge_in.max_offset_ms"
            for e in errors
        )

    def test_probability_out_of_range(self):
        errors = validate_barge_in({"prompt": "Interrupt.", "probability": 1.4})
        assert any(e["field"] == "augmentation.barge_in.probability" for e in errors)


class TestNoiseValidator:
    def test_missing_noise_profile(self):
        errors = validate_noise({"noise_snr_db": 10})
        assert any(e["field"] == "augmentation.noise.noise_profile" for e in errors)

    def test_missing_noise_snr_db(self):
        errors = validate_noise({"noise_profile": "cafeteria"})
        assert any(e["field"] == "augmentation.noise.noise_snr_db" for e in errors)

    def test_unknown_profile_passes_preflight(self):
        # FR-024: do NOT hard-code allowlist; server is source of truth.
        errors = validate_noise({"noise_profile": "airport", "noise_snr_db": 10})
        assert errors == []


# ---------------------------------------------------------------------------
# T028 — Error envelope shape
# ---------------------------------------------------------------------------

class TestErrorEnvelopeShape:
    @pytest.mark.parametrize(
        "augmentation,expected_strategy",
        [
            ({"cap": {"probability": 1.5}}, "cap"),
            ({"directed_speech": {"probability": 0.3, "gain_db": 5}}, "directed_speech"),
            ({"backchannel": {}}, "backchannel"),
            ({"barge_in": {}}, "barge_in"),
            ({"noise": {}}, "noise"),
        ],
    )
    def test_every_error_envelope_has_required_fields(
        self, augmentation, expected_strategy
    ):
        errors = validate_augmentation(augmentation)
        assert errors
        for err in errors:
            assert "error" in err
            assert "field" in err
            assert "strategy" in err
            assert err["strategy"] == expected_strategy
            assert err["field"].startswith(f"augmentation.{expected_strategy}.")

    def test_unknown_key_envelope_lists_known_set(self):
        errors = validate_augmentation({"echo": {}})
        assert errors
        assert errors[0]["strategy"] == "echo"
        assert errors[0]["known"] == list(KNOWN_STRATEGIES)


# ---------------------------------------------------------------------------
# Full chain — validate_augmentation
# ---------------------------------------------------------------------------

class TestValidateAugmentationChain:
    def test_empty_block_is_no_op(self):
        assert validate_augmentation({}) == []

    def test_none_block_is_no_op(self):
        assert validate_augmentation(None) == []  # type: ignore[arg-type]

    def test_valid_single_strategy_passes(self):
        assert validate_augmentation({"cap": {"probability": 0.5}}) == []

    def test_valid_composed_passes(self):
        assert validate_augmentation({
            "barge_in": {"prompt": "Interrupt."},
            "noise": {"noise_profile": "cafeteria", "noise_snr_db": 10},
        }) == []

    def test_missing_required_short_circuits_before_range(self):
        """Required-field error appears before any range error on same strategy."""
        # cap is missing probability entirely.
        errors = validate_augmentation({"cap": {"pause_ms": 800}})
        # The first error should be the required-field error.
        assert errors[0]["field"] == "augmentation.cap.probability"
        assert "required" in errors[0]["error"]

    def test_strategy_value_must_be_dict(self):
        errors = validate_augmentation({"cap": "not-a-dict"})
        assert errors
        assert errors[0]["strategy"] == "cap"
        assert "object/dict" in errors[0]["error"]


# ---------------------------------------------------------------------------
# 044 — dropout, start_at_turn, seed
# ---------------------------------------------------------------------------

class TestDropoutValidator:
    def test_probability_only_passes(self):
        assert validate_dropout({"probability": 0.3}) == []

    def test_missing_probability_required(self):
        errors = validate_dropout({"start_at_turn": 2})
        assert errors[0]["error"] == "dropout.probability is required."
        assert errors[0]["field"] == "augmentation.dropout.probability"

    @pytest.mark.parametrize("p", [1.5, -0.1, True])
    def test_probability_out_of_range_rejected(self, p):
        errors = validate_dropout({"probability": p})
        assert errors and errors[0]["field"] == "augmentation.dropout.probability"

    @pytest.mark.parametrize("p", [0, 1])
    def test_probability_boundaries_accepted(self, p):
        assert validate_dropout({"probability": p}) == []

    @pytest.mark.parametrize("turn", [1, 3])
    def test_start_at_turn_accepted(self, turn):
        assert validate_dropout({"probability": 0.3, "start_at_turn": turn}) == []

    @pytest.mark.parametrize("turn", [0, -1, 2.5, True])
    def test_start_at_turn_rejected(self, turn):
        errors = validate_dropout({"probability": 0.3, "start_at_turn": turn})
        assert errors[0]["field"] == "augmentation.dropout.start_at_turn"
        assert "Must be an int >= 1" in errors[0]["error"]

    @pytest.mark.parametrize("seed", [7, None])
    def test_seed_accepted(self, seed):
        assert validate_dropout({"probability": 0.3, "seed": seed}) == []

    @pytest.mark.parametrize("seed", ["abc", 1.5, True])
    def test_seed_rejected(self, seed):
        errors = validate_dropout({"probability": 0.3, "seed": seed})
        assert errors[0]["field"] == "augmentation.dropout.seed"
        assert "Must be an int or null" in errors[0]["error"]

    def test_dropout_with_barge_in_conflicts(self):
        assert validate_composition({
            "dropout": {"probability": 0.3},
            "barge_in": {"prompt": "x"},
        }) == ["barge_in", "dropout"]

    def test_dropout_with_noise_composes(self):
        assert validate_composition({
            "dropout": {"probability": 0.3},
            "noise": {"noise_profile": "cafeteria", "noise_snr_db": 10},
        }) == []


_START_AT_TURN_STRATEGIES = [
    ("directed_speech", {"probability": 0.3}),
    ("secondary_speaker", {"probability": 0.3, "secondary_voice": "alloy"}),
    ("backchannel", {"utterance": "mm-hmm"}),
    ("barge_in", {"prompt": "ask about fees"}),
    ("dropout", {"probability": 0.3}),
]


class TestStartAtTurnAcrossStrategies:
    @pytest.mark.parametrize("strategy,config", _START_AT_TURN_STRATEGIES)
    def test_valid_start_at_turn_accepted(self, strategy, config):
        assert validate_augmentation(
            {strategy: {**config, "start_at_turn": 2}}
        ) == []

    @pytest.mark.parametrize("strategy,config", _START_AT_TURN_STRATEGIES)
    def test_zero_rejected(self, strategy, config):
        errors = validate_augmentation({strategy: {**config, "start_at_turn": 0}})
        assert errors[0]["field"] == f"augmentation.{strategy}.start_at_turn"

    def test_cap_does_not_take_start_at_turn(self):
        errors = validate_augmentation(
            {"cap": {"probability": 0.3, "start_at_turn": 2}}
        )
        assert errors[0]["error"] == (
            "Unknown field cap.start_at_turn. "
            "cap accepts: ['pause_ms', 'probability']."
        )
        assert errors[0]["field"] == "augmentation.cap.start_at_turn"

    def test_noise_does_not_take_start_at_turn(self):
        errors = validate_augmentation({"noise": {
            "noise_profile": "cafeteria", "noise_snr_db": 10, "start_at_turn": 2,
        }})
        assert errors[0]["error"].startswith("Unknown field noise.start_at_turn.")
        assert "noise_profile" in errors[0]["error"]


class TestFieldAllowlist:
    def test_misspelled_field_rejected_by_name(self):
        errors = validate_augmentation(
            {"barge_in": {"prompt": "x", "probabilty": 0.2}}
        )
        assert errors[0]["field"] == "augmentation.barge_in.probabilty"
        assert errors[0]["error"].startswith("Unknown field barge_in.probabilty.")

    @pytest.mark.parametrize(
        "block,field,message",
        [
            (
                {"noise": {"profile": "cafeteria", "snr_db": 10, "probability": 0.5}},
                "augmentation.noise.probability",
                "noise.probability is published by the SDK but ignored by "
                "Okareo: noise plays for the whole call. Remove it.",
            ),
            (
                {"barge_in": {"prompt": "x", "replacement_text": "wait"}},
                "augmentation.barge_in.replacement_text",
                "barge_in.replacement_text is published by the SDK but ignored "
                "by Okareo. Use barge_in.prompt to steer what the interruption "
                "says.",
            ),
            (
                {"barge_in": {"prompt": "x", "utterance": "wait"}},
                "augmentation.barge_in.utterance",
                "barge_in.utterance is published by the SDK but ignored by "
                "Okareo. Use barge_in.prompt to steer what the interruption says.",
            ),
        ],
    )
    def test_sdk_settings_ignored_by_server_rejected(self, block, field, message):
        errors = validate_augmentation(block)
        assert errors[0]["field"] == field
        assert errors[0]["error"] == message

    @pytest.mark.parametrize(
        "block",
        [
            {"noise": {"profile": "cafeteria", "snr_db": 10}},
            {"secondary_speaker": {"probability": 0.2, "voice": "alloy"}},
            {"secondary_speaker": {
                "probability": 0.2, "secondary_voice": "alloy",
                "reverb_preset": "room_teleco",
            }},
        ],
    )
    def test_sdk_spellings_accepted(self, block):
        assert validate_augmentation(block) == []

    def test_both_spellings_rejected(self):
        errors = validate_augmentation({"noise": {
            "profile": "a", "noise_profile": "b", "snr_db": 10,
        }})
        assert errors[0]["error"] == (
            "Set either noise.profile or noise.noise_profile, not both."
        )
        assert errors[0]["field"] == "augmentation.noise.profile"

    def test_required_check_is_alias_aware(self):
        errors = validate_augmentation({"noise": {"snr_db": 10}})
        assert errors[0]["field"] == "augmentation.noise.noise_profile"
        assert "noise.noise_profile" in errors[0]["error"]
        assert "noise.profile" in errors[0]["error"]

    def test_secondary_voice_required_via_either_spelling(self):
        errors = validate_augmentation({"secondary_speaker": {"probability": 0.2}})
        assert errors[0]["field"] == "augmentation.secondary_speaker.secondary_voice"
        assert "secondary_speaker.voice" in errors[0]["error"]


class TestPreflightMaxTurns:
    def test_start_at_turn_beyond_max_turns_rejected(self):
        err = preflight_augmentation(
            {"dropout": {"probability": 0.3, "start_at_turn": 6}}, 5
        )
        assert err == {
            "error": (
                "augmentation.dropout.start_at_turn=6 is beyond max_turns=5, "
                "so dropout would never fire. Lower start_at_turn or raise "
                "max_turns."
            ),
            "field": "augmentation.dropout.start_at_turn",
            "strategy": "dropout",
        }

    def test_start_at_turn_equal_max_turns_accepted(self):
        assert preflight_augmentation(
            {"dropout": {"probability": 0.3, "start_at_turn": 5}}, 5
        ) is None

    def test_bound_skipped_when_max_turns_not_final(self):
        assert preflight_augmentation(
            {"dropout": {"probability": 0.3, "start_at_turn": 50}}, None
        ) is None
