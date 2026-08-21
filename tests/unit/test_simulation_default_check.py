"""E2 (spec 032): simulations always run with at least one check.

When run_simulation is called with no checks, the benign code-based
"latency" check is auto-applied and the substitution is disclosed in the
response. Supplied checks pass through untouched.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from src.okareo_client import ResolvedProject


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
    from okareo_api_client.api import default as _default_pkg

    mock_module = MagicMock()
    with patch.object(
        _default_pkg,
        "get_scenario_sets_v0_scenario_sets_get",
        mock_module,
        create=True,
    ):
        yield mock_module


def _make_scenario():
    s = MagicMock()
    s.name = "my-scenario"
    s.scenario_count = 5
    return s


def _mock_sdk(mock_client, mock_project, mock_get_scenario_sets, run_id="run-1"):
    mock_project.return_value = ResolvedProject(id="00000000-0000-0000-0000-000000000111", name="Global", basis="default")
    okareo = MagicMock()
    okareo.run_simulation.return_value = MagicMock(
        id=run_id, name="x", app_link="link"
    )
    mock_client.return_value = okareo
    mock_get_scenario_sets.sync.return_value = [_make_scenario()]
    return okareo


class TestDefaultCheckInjection:
    @patch("src.tools.simulations.resolve_project")
    @patch("src.tools.simulations.get_okareo_client")
    def test_omitted_checks_get_latency_default_with_disclosure(
        self, mock_client, mock_project, tools, mock_get_scenario_sets, sim_submission,):
        _mock_sdk(mock_client, mock_project, mock_get_scenario_sets)

        result = json.loads(tools["run_simulation"](
            name="no-checks-run",
            scenario_name="my-scenario",
            target_name="some-target",
        ))

        assert "error" not in result, result
        assert sim_submission.call_args.kwargs["checks"] == ["latency"]
        assert result["default_check_applied"] == "latency"
        assert "check" in result["default_check_note"].lower()

    @patch("src.tools.simulations.resolve_project")
    @patch("src.tools.simulations.get_okareo_client")
    def test_empty_checks_list_gets_latency_default(
        self, mock_client, mock_project, tools, mock_get_scenario_sets, sim_submission,):
        _mock_sdk(mock_client, mock_project, mock_get_scenario_sets)

        result = json.loads(tools["run_simulation"](
            name="empty-checks-run",
            scenario_name="my-scenario",
            target_name="some-target",
            checks=[],
        ))

        assert "error" not in result, result
        assert sim_submission.call_args.kwargs["checks"] == ["latency"]
        assert result["default_check_applied"] == "latency"

    @patch("src.tools.simulations.resolve_project")
    @patch("src.tools.simulations.get_okareo_client")
    def test_supplied_checks_pass_through_without_disclosure(
        self, mock_client, mock_project, tools, mock_get_scenario_sets, sim_submission,):
        _mock_sdk(mock_client, mock_project, mock_get_scenario_sets)

        result = json.loads(tools["run_simulation"](
            name="explicit-checks-run",
            scenario_name="my-scenario",
            target_name="some-target",
            checks=["coherence", "fluency"],
        ))

        assert "error" not in result, result
        assert sim_submission.call_args.kwargs["checks"] == [
            "coherence", "fluency",
        ]
        assert "default_check_applied" not in result
        assert "default_check_note" not in result

    @patch("src.tools.simulations.resolve_project")
    @patch("src.tools.simulations.get_okareo_client")
    def test_empty_checks_with_peer_settings_still_get_default(
        self, mock_client, mock_project, tools, mock_get_scenario_sets, sim_submission,):
        """Per-turn evaluation / early-stop with an empty check list still
        receives the benign default, keeping those settings meaningful."""
        _mock_sdk(mock_client, mock_project, mock_get_scenario_sets)

        result = json.loads(tools["run_simulation"](
            name="peer-settings-run",
            scenario_name="my-scenario",
            target_name="some-target",
            checks=[],
            checks_at_every_turn=True,
            stop_check={"check_name": "latency", "stop_on": True},
        ))

        assert "error" not in result, result
        kwargs = sim_submission.call_args.kwargs
        assert kwargs["checks"] == ["latency"]
        assert kwargs["simulation_params"].checks_at_every_turn is True
        assert result["default_check_applied"] == "latency"

    @patch("src.tools.simulations.resolve_project")
    @patch("src.tools.simulations.get_okareo_client")
    def test_backend_unknown_latency_check_surfaces_named_error(
        self, mock_client, mock_project, tools, mock_get_scenario_sets, sim_submission,):
        """If the backend rejects the default check, the tool errors naming
        it — it never falls back to a check-less run."""
        _mock_sdk(mock_client, mock_project, mock_get_scenario_sets)
        # The submission now goes through ModelUnderTest.run_test rather than
        # okareo.run_simulation (036 rev 2 — the target is resolved inside the
        # acting project first), so the backend rejection is raised there.
        sim_submission.side_effect = Exception(
            "checks entered was invalid: latency"
        )

        result = json.loads(tools["run_simulation"](
            name="missing-latency-run",
            scenario_name="my-scenario",
            target_name="some-target",
        ))

        assert "error" in result
        assert "latency" in json.dumps(result)
        # A check-less retry never happened: the single call carried the default.
        assert sim_submission.call_count == 1
        assert sim_submission.call_args.kwargs["checks"] == ["latency"]
