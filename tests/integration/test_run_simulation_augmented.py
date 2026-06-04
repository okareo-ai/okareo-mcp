"""Integration: run_simulation augmented path (spec 023-tool-fixes US4, US5, US8).

Mocks the okareo SDK boundary and verifies:
- Each of the 5 non-noise strategies produces a run_test call carrying the
  augmentation block on simulation_params (T017).
- Composing noise with a non-noise strategy works; two non-noise strategies
  are rejected before any SDK call (T022).
- The four peer simulation_params knobs reach the backend (T034).
"""

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _register_and_get_tools():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    from src.tools.simulations import register_tools
    register_tools(mcp)
    return {name: tool.fn for name, tool in mcp._tool_manager._tools.items()}


@pytest.fixture
def tools():
    return _register_and_get_tools()


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("OKAREO_API_KEY", "test-api-key-12345")


@pytest.fixture
def mock_get_scenario_sets():
    """Patch the get_scenario_sets openapi submodule on its parent package.

    A `from X import Y` for a submodule Y permanently binds Y onto X's
    __dict__ at first import — so sys.modules-only patching is fragile
    after the first call. patch.object on the parent works regardless of
    prior binding state and cleans up correctly.
    """
    from okareo_api_client.api import default as _default_pkg

    mock_module = MagicMock()
    with patch.object(
        _default_pkg,
        "get_scenario_sets_v0_scenario_sets_get",
        mock_module,
        create=True,
    ):
        yield mock_module


def _make_voice_target():
    """A mock target_model whose `.target['type']` is 'voice'."""
    tm = MagicMock()
    tm.id = "00000000-0000-0000-0000-000000000001"
    tm.name = "voice-target"
    tm.target = {"type": "voice", "edge_type": "openai", "model": "gpt-4o"}
    return tm


def _make_custom_endpoint_target():
    tm = MagicMock()
    tm.id = "00000000-0000-0000-0000-000000000002"
    tm.name = "custom-endpoint-target"
    tm.target = {
        "type": "custom_endpoint",
        "next_message_params": {"url": "https://x.example.com", "method": "POST"},
    }
    return tm


def _make_scenario():
    s = MagicMock()
    s.name = "my-scenario"
    s.scenario_count = 5
    return s


def _make_driver():
    d = MagicMock()
    d.id = "00000000-0000-0000-0000-000000000099"
    d.name = "my-driver"
    return d


def _mock_okareo_for_augmented_path(target=None, driver=None):
    """Wire up the mocks for an augmented run. Returns (okareo, mut)."""
    okareo = MagicMock()
    okareo.api_key = "test-api-key-12345"
    okareo.client = MagicMock()
    okareo.get_target_by_name.return_value = target or _make_voice_target()
    okareo.get_driver_by_name.return_value = driver or _make_driver()
    okareo.create_or_update_driver.return_value = driver or _make_driver()
    # The bypass path constructs its own ModelUnderTest, so mock the class.
    mut_instance = MagicMock()
    mut_result = MagicMock()
    mut_result.id = "test-run-id-xyz"
    mut_result.name = "augmented-run"
    mut_result.app_link = "https://app.okareo.com/test_runs/xyz"
    mut_instance.run_test.return_value = mut_result
    return okareo, mut_instance


# ---------------------------------------------------------------------------
# T017 — Each of the 5 non-noise strategies submits an augmented run
# ---------------------------------------------------------------------------

class TestEachStrategy:
    """Verify that each augmentation strategy reaches the SDK's run_test."""

    @pytest.mark.parametrize(
        "strategy_name,strategy_config",
        [
            ("cap", {"probability": 0.4, "pause_ms": 800}),
            ("directed_speech", {"probability": 0.3}),
            ("secondary_speaker", {
                "probability": 0.3, "secondary_voice": "Cathy - Coworker",
            }),
            ("backchannel", {"utterance": "mm-hmm", "probability": 0.35}),
            ("barge_in", {
                "prompt": "Politely interrupt.", "probability": 0.2,
                "min_offset_ms": 200, "max_offset_ms": 600,
            }),
        ],
    )
    @patch("okareo.model_under_test.ModelUnderTest")
    @patch("src.tools.simulations.resolve_project_id")
    @patch("src.tools.simulations.get_okareo_client")
    def test_strategy_reaches_run_test(
        self, mock_client, mock_project, mock_mut_class, strategy_name,
        strategy_config, tools, mock_get_scenario_sets,
    ):
        mock_project.return_value = "00000000-0000-0000-0000-000000000111"
        okareo, mut_instance = _mock_okareo_for_augmented_path()
        mock_client.return_value = okareo
        mock_mut_class.return_value = mut_instance

        mock_get_scenario_sets.sync.return_value = [_make_scenario()]

        result = json.loads(tools["run_simulation"](
            name=f"voice-{strategy_name}-run",
            scenario_name="my-scenario",
            target_name="voice-target",
            driver_name="my-driver",
            augmentation={strategy_name: strategy_config},
        ))

        assert "error" not in result, result
        assert result["test_run_id"] == "test-run-id-xyz"

        # Verify the augmentation block was forwarded.
        mut_instance.run_test.assert_called_once()
        kwargs = mut_instance.run_test.call_args.kwargs
        sim_params = kwargs["simulation_params"]
        emitted = sim_params.to_dict()
        assert "augmentation" in emitted
        assert emitted["augmentation"][strategy_name] == strategy_config
        # The legacy concurrent_ask_probability MUST NOT auto-mirror from cap
        # (contracts/run_simulation.contract.md).
        assert emitted.get("concurrent_ask_probability", 0.0) == 0.0


# ---------------------------------------------------------------------------
# T022 — Composition: noise + non-noise succeeds; two non-noise rejected
# ---------------------------------------------------------------------------

class TestComposition:
    @patch("okareo.model_under_test.ModelUnderTest")
    @patch("src.tools.simulations.resolve_project_id")
    @patch("src.tools.simulations.get_okareo_client")
    def test_noise_plus_barge_in_succeeds(
        self, mock_client, mock_project, mock_mut_class, tools, mock_get_scenario_sets,
    ):
        mock_project.return_value = "00000000-0000-0000-0000-000000000111"
        okareo, mut_instance = _mock_okareo_for_augmented_path()
        mock_client.return_value = okareo
        mock_mut_class.return_value = mut_instance
        mock_get_scenario_sets.sync.return_value = [_make_scenario()]

        result = json.loads(tools["run_simulation"](
            name="noisy-barge-in",
            scenario_name="my-scenario",
            target_name="voice-target",
            driver_name="my-driver",
            augmentation={
                "barge_in": {"prompt": "Interrupt.", "probability": 0.2},
                "noise": {"noise_profile": "cafeteria", "noise_snr_db": 10},
            },
        ))

        assert "error" not in result, result
        mut_instance.run_test.assert_called_once()
        emitted = mut_instance.run_test.call_args.kwargs["simulation_params"].to_dict()
        assert "barge_in" in emitted["augmentation"]
        assert "noise" in emitted["augmentation"]

    @patch("okareo.model_under_test.ModelUnderTest")
    @patch("src.tools.simulations.get_okareo_client")
    def test_two_non_noise_strategies_rejected_before_sdk(
        self, mock_client, mock_mut_class, tools,
    ):
        okareo, mut_instance = _mock_okareo_for_augmented_path()
        mock_client.return_value = okareo
        mock_mut_class.return_value = mut_instance

        result = json.loads(tools["run_simulation"](
            name="bad-combo",
            scenario_name="my-scenario",
            target_name="voice-target",
            driver_name="my-driver",
            augmentation={
                "cap": {"probability": 0.3},
                "secondary_speaker": {
                    "probability": 0.3, "secondary_voice": "x",
                },
            },
        ))

        assert "error" in result
        assert "Unsupported augmentation combination" in result["error"]
        assert set(result["conflicting_strategies"]) == {"cap", "secondary_speaker"}
        # SDK / mut MUST NOT be touched (FR-026).
        okareo.get_target_by_name.assert_not_called()
        mut_instance.run_test.assert_not_called()


# ---------------------------------------------------------------------------
# Voice-target preflight (FR-025)
# ---------------------------------------------------------------------------

class TestVoiceTargetPreflight:
    @patch("okareo.model_under_test.ModelUnderTest")
    @patch("src.tools.simulations.resolve_project_id")
    @patch("src.tools.simulations.get_okareo_client")
    def test_augmentation_on_non_voice_target_rejected(
        self, mock_client, mock_project, mock_mut_class, tools, mock_get_scenario_sets,
    ):
        mock_project.return_value = "00000000-0000-0000-0000-000000000111"
        okareo, mut_instance = _mock_okareo_for_augmented_path(
            target=_make_custom_endpoint_target()
        )
        mock_client.return_value = okareo
        mock_mut_class.return_value = mut_instance
        mock_get_scenario_sets.sync.return_value = [_make_scenario()]

        result = json.loads(tools["run_simulation"](
            name="bad-target",
            scenario_name="my-scenario",
            target_name="custom-endpoint-target",
            driver_name="my-driver",
            augmentation={"cap": {"probability": 0.3}},
        ))

        assert "error" in result
        assert "voice Targets" in result["error"]
        assert result["target_type"] == "custom_endpoint"
        mut_instance.run_test.assert_not_called()


# ---------------------------------------------------------------------------
# Range-error preflight (FR-021..023, FR-026)
# ---------------------------------------------------------------------------

class TestRangePreflight:
    @patch("okareo.model_under_test.ModelUnderTest")
    @patch("src.tools.simulations.get_okareo_client")
    def test_out_of_range_probability_returns_named_error_without_sdk_call(
        self, mock_client, mock_mut_class, tools,
    ):
        okareo, mut_instance = _mock_okareo_for_augmented_path()
        mock_client.return_value = okareo
        mock_mut_class.return_value = mut_instance

        result = json.loads(tools["run_simulation"](
            name="bad-range",
            scenario_name="my-scenario",
            target_name="voice-target",
            driver_name="my-driver",
            augmentation={"cap": {"probability": 1.5}},
        ))

        assert "error" in result
        assert result["field"] == "augmentation.cap.probability"
        assert result["strategy"] == "cap"
        assert "[0.0, 1.0]" in result["error"]
        # No network calls at all (FR-026).
        okareo.get_target_by_name.assert_not_called()
        mut_instance.run_test.assert_not_called()

    @patch("okareo.model_under_test.ModelUnderTest")
    @patch("src.tools.simulations.get_okareo_client")
    def test_unknown_strategy_returns_known_list(
        self, mock_client, mock_mut_class, tools,
    ):
        okareo, mut_instance = _mock_okareo_for_augmented_path()
        mock_client.return_value = okareo
        mock_mut_class.return_value = mut_instance

        result = json.loads(tools["run_simulation"](
            name="bad-key",
            scenario_name="my-scenario",
            target_name="voice-target",
            driver_name="my-driver",
            augmentation={"echo": {}},
        ))

        assert "error" in result
        assert "echo" in result["error"]
        assert result["strategy"] == "echo"
        assert "known" in result
        assert "cap" in result["known"]
        mut_instance.run_test.assert_not_called()


# ---------------------------------------------------------------------------
# T034 — Peer simulation_params knobs reach the backend
# ---------------------------------------------------------------------------

class TestPeerKnobs:
    @patch("okareo.model_under_test.ModelUnderTest")
    @patch("src.tools.simulations.resolve_project_id")
    @patch("src.tools.simulations.get_okareo_client")
    def test_silence_timeout_ms_reaches_augmented_payload(
        self, mock_client, mock_project, mock_mut_class, tools, mock_get_scenario_sets,
    ):
        mock_project.return_value = "00000000-0000-0000-0000-000000000111"
        okareo, mut_instance = _mock_okareo_for_augmented_path()
        mock_client.return_value = okareo
        mock_mut_class.return_value = mut_instance
        mock_get_scenario_sets.sync.return_value = [_make_scenario()]

        result = json.loads(tools["run_simulation"](
            name="silence-test",
            scenario_name="my-scenario",
            target_name="voice-target",
            driver_name="my-driver",
            silence_timeout_ms=8000,
        ))

        assert "error" not in result, result
        emitted = mut_instance.run_test.call_args.kwargs[
            "simulation_params"].to_dict()
        assert emitted["silence_timeout_ms"] == 8000

    @patch("src.tools.simulations.resolve_project_id")
    @patch("src.tools.simulations.get_okareo_client")
    def test_turn_transition_time_threaded_through_non_augmented_path(
        self, mock_client, mock_project, tools, mock_get_scenario_sets,
    ):
        """turn_transition_time without augmentation goes through the SDK's
        run_simulation helper (the non-bypass path).
        """
        mock_project.return_value = "00000000-0000-0000-0000-000000000111"
        okareo = MagicMock()
        okareo.run_simulation.return_value = MagicMock(
            id="run-1", name="x", app_link="link"
        )
        mock_client.return_value = okareo
        mock_get_scenario_sets.sync.return_value = [_make_scenario()]

        result = json.loads(tools["run_simulation"](
            name="ttt-test",
            scenario_name="my-scenario",
            target_name="some-target",
            turn_transition_time=2500,
        ))

        assert "error" not in result, result
        okareo.run_simulation.assert_called_once()
        assert okareo.run_simulation.call_args.kwargs["turn_transition_time"] == 2500

    @patch("src.tools.simulations.resolve_project_id")
    @patch("src.tools.simulations.get_okareo_client")
    def test_checks_at_every_turn_threaded_through_non_augmented_path(
        self, mock_client, mock_project, tools, mock_get_scenario_sets,
    ):
        mock_project.return_value = "00000000-0000-0000-0000-000000000111"
        okareo = MagicMock()
        okareo.run_simulation.return_value = MagicMock(
            id="run-2", name="x", app_link="link"
        )
        mock_client.return_value = okareo
        mock_get_scenario_sets.sync.return_value = [_make_scenario()]

        result = json.loads(tools["run_simulation"](
            name="caet-test",
            scenario_name="my-scenario",
            target_name="some-target",
            checks_at_every_turn=True,
        ))

        assert "error" not in result, result
        assert okareo.run_simulation.call_args.kwargs["checks_at_every_turn"] is True

    @patch("src.tools.simulations.resolve_project_id")
    @patch("src.tools.simulations.get_okareo_client")
    def test_stop_check_object_threaded_through_non_augmented_path(
        self, mock_client, mock_project, tools, mock_get_scenario_sets,
    ):
        mock_project.return_value = "00000000-0000-0000-0000-000000000111"
        okareo = MagicMock()
        okareo.run_simulation.return_value = MagicMock(
            id="run-3", name="x", app_link="link"
        )
        mock_client.return_value = okareo
        mock_get_scenario_sets.sync.return_value = [_make_scenario()]

        result = json.loads(tools["run_simulation"](
            name="stop-test",
            scenario_name="my-scenario",
            target_name="some-target",
            stop_check={"check_name": "red_flag", "stop_on": True},
        ))

        assert "error" not in result, result
        stop = okareo.run_simulation.call_args.kwargs["stop_check"]
        assert stop == {"check_name": "red_flag", "stop_on": True}


# ---------------------------------------------------------------------------
# Backward compatibility — existing callers unaffected (FR-031, FR-032)
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    @patch("src.tools.simulations.resolve_project_id")
    @patch("src.tools.simulations.get_okareo_client")
    def test_no_new_params_uses_legacy_path(
        self, mock_client, mock_project, tools, mock_get_scenario_sets,
    ):
        """When the caller passes only the pre-feature parameters, we still
        call okareo.run_simulation (the SDK's high-level helper).
        """
        mock_project.return_value = "00000000-0000-0000-0000-000000000111"
        okareo = MagicMock()
        okareo.run_simulation.return_value = MagicMock(
            id="run-legacy", name="x", app_link="link"
        )
        mock_client.return_value = okareo
        mock_get_scenario_sets.sync.return_value = [_make_scenario()]

        result = json.loads(tools["run_simulation"](
            name="legacy-run",
            scenario_name="my-scenario",
            target_name="some-target",
        ))

        assert "error" not in result, result
        okareo.run_simulation.assert_called_once()
        # No new params leaked into the SDK call.
        kwargs = okareo.run_simulation.call_args.kwargs
        assert "augmentation" not in kwargs
        assert "silence_timeout_ms" not in kwargs
