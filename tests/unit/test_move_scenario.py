"""Unit tests for the move_scenario tool (038-scenario-move).

Mock-based, mirroring tests/unit/test_scenarios.py: the FastMCP registry is
built for real, the Okareo client / project resolution / raw HTTP helper are
patched at the src.tools.scenarios seam.
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from src.okareo_client import ResolvedProject

_PATCH_GET_CLIENT = "src.tools.scenarios.get_okareo_client"
_PATCH_RESOLVE_PROJECT = "src.tools.scenarios.resolve_project"
_PATCH_API_REQUEST = "src.tools.scenarios.okareo_api_request"

SOURCE = ResolvedProject(id="proj-global", name="Global", basis="pin")
DESTINATION = ResolvedProject(id="proj-adp", name="ADP", basis="explicit")
SCENARIO_ID = "3b9f2c1e-7a4d-4c5b-9e2f-1a2b3c4d5e6f"


def _register_and_get_tools():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    from src.tools.scenarios import register_tools

    register_tools(mcp)
    return {name: tool.fn for name, tool in mcp._tool_manager._tools.items()}


@pytest.fixture
def tools():
    return _register_and_get_tools()


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setenv("OKAREO_API_KEY", "test-api-key-12345")


@pytest.fixture
def mock_get_scenarios():
    from okareo_api_client.api import default as _default_pkg

    mock_module = MagicMock()
    with patch.object(
        _default_pkg,
        "get_scenario_sets_v0_scenario_sets_get",
        mock_module,
        create=True,
    ):
        yield mock_module


def _make_plan(**overrides):
    plan = {
        "scenario_id": SCENARIO_ID,
        "scenario_name": "ADP checkout",
        "source_project_id": SOURCE.id,
        "source_project_name": SOURCE.name,
        "destination_project_id": DESTINATION.id,
        "destination_project_name": DESTINATION.name,
        "counts": {"scenario_set": 1, "test_run": 5, "datapoint": 1240},
        "running_runs": [],
        "blockers": [],
        "executed": False,
    }
    plan.update(overrides)
    return plan


def _http_error(status: int, detail):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = {"detail": detail}
    return httpx.HTTPStatusError("boom", request=MagicMock(), response=response)


class TestMoveScenarioDryRunFirst:
    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT, side_effect=[SOURCE, DESTINATION])
    @patch(_PATCH_GET_CLIENT)
    def test_dry_run_is_the_default_and_relays_the_plan(
        self, mock_client, mock_resolve, mock_api, tools
    ):
        mock_api.return_value = _make_plan()

        result = json.loads(tools["move_scenario"](scenario=SCENARIO_ID, to_project="ADP"))

        assert mock_api.call_count == 1
        args, kwargs = mock_api.call_args
        assert args[1] == "post"
        assert args[2] == f"/v0/scenario_sets/{SCENARIO_ID}/move"
        assert kwargs["params"] == {"dry_run": "true"}
        assert kwargs["json"] == {"destination_project_id": DESTINATION.id}

        assert result["executed"] is False
        assert result["plan"]["counts"]["test_run"] == 5
        assert result["source_project"] == SOURCE.as_dict()
        assert result["destination_project"] == DESTINATION.as_dict()
        assert "dry run" in result["next_step"].lower()
        assert "confirm" in result["next_step"].lower()

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT, side_effect=[SOURCE, DESTINATION])
    @patch(_PATCH_GET_CLIENT)
    def test_resolves_source_then_destination(
        self, mock_client, mock_resolve, mock_api, tools
    ):
        mock_api.return_value = _make_plan()

        tools["move_scenario"](scenario=SCENARIO_ID, to_project="ADP", project="Global")

        first, second = mock_resolve.call_args_list
        assert first.args[1] == "Global"  # source: the standard `project` argument
        assert second.args[1] == "ADP"  # destination: always explicit

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT, side_effect=[SOURCE, DESTINATION])
    @patch(_PATCH_GET_CLIENT)
    def test_execute_move(self, mock_client, mock_resolve, mock_api, tools):
        mock_api.return_value = _make_plan(executed=True)

        result = json.loads(
            tools["move_scenario"](scenario=SCENARIO_ID, to_project="ADP", dry_run=False)
        )

        _, kwargs = mock_api.call_args
        assert kwargs["params"] is None
        assert result["executed"] is True
        assert "next_step" not in result


class TestMoveScenarioNameResolution:
    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT, side_effect=[SOURCE, DESTINATION])
    @patch(_PATCH_GET_CLIENT)
    def test_name_resolves_within_the_source_project(
        self, mock_client, mock_resolve, mock_api, tools, mock_get_scenarios
    ):
        """Scenario-by-name lookup is confined to the SOURCE project —
        unchanged in Revision 2 (Scenarios stay project-scoped; restored per
        review CR-4)."""
        row = MagicMock()
        row.name = "ADP checkout"
        row.scenario_id = SCENARIO_ID
        mock_get_scenarios.sync.return_value = [row]
        mock_api.return_value = _make_plan()

        result = json.loads(
            tools["move_scenario"](scenario="ADP checkout", to_project="ADP")
        )

        assert mock_get_scenarios.sync.call_args.kwargs["project_id"] == SOURCE.id
        args, _ = mock_api.call_args
        assert args[2] == f"/v0/scenario_sets/{SCENARIO_ID}/move"
        assert result["plan"]["scenario_name"] == "ADP checkout"

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT, side_effect=[SOURCE, DESTINATION])
    @patch(_PATCH_GET_CLIENT)
    def test_unknown_name_is_an_error_naming_the_project(
        self, mock_client, mock_resolve, mock_api, tools, mock_get_scenarios
    ):
        other = MagicMock()
        other.name = "something else"
        other.scenario_id = "other-id"
        mock_get_scenarios.sync.return_value = [other]

        result = json.loads(
            tools["move_scenario"](scenario="ADP checkout", to_project="ADP")
        )

        assert "error" in result
        assert "ADP checkout" in result["error"]
        assert SOURCE.name in result["error"]
        assert mock_api.call_count == 0


class TestMoveScenarioRefusals:
    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT, side_effect=[SOURCE, DESTINATION])
    @patch(_PATCH_GET_CLIENT)
    def test_409_is_a_structured_blocked_response_never_a_retry(
        self, mock_client, mock_resolve, mock_api, tools
    ):
        blocked_plan = _make_plan(
            blockers=[
                {
                    "code": "running_simulations",
                    "detail": "2 simulations still running; move when they finish.",
                }
            ]
        )
        mock_api.side_effect = _http_error(409, blocked_plan)

        result = json.loads(
            tools["move_scenario"](scenario=SCENARIO_ID, to_project="ADP", dry_run=False)
        )

        assert result["blocked"] is True
        assert result["plan"]["blockers"][0]["code"] == "running_simulations"
        # Revision 2: targets are org-shared — no keep_both/entangled choices
        assert "keep_both" not in result["message"]
        assert "entangled" not in result["message"]
        assert mock_api.call_count == 1  # no silent retry

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT, side_effect=[SOURCE, DESTINATION])
    @patch(_PATCH_GET_CLIENT)
    def test_503_relays_the_rollback_and_retry_guidance(
        self, mock_client, mock_resolve, mock_api, tools
    ):
        mock_api.side_effect = _http_error(
            503, "The move timed out and was rolled back — nothing moved. Retry."
        )

        result = json.loads(
            tools["move_scenario"](scenario=SCENARIO_ID, to_project="ADP", dry_run=False)
        )

        assert "timed out" in result["error"]
        assert result["retryable"] is True
        assert mock_api.call_count == 1

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT, side_effect=[SOURCE, DESTINATION])
    @patch(_PATCH_GET_CLIENT)
    def test_400_same_project_relays_the_detail(
        self, mock_client, mock_resolve, mock_api, tools
    ):
        mock_api.side_effect = _http_error(
            400, "Scenario 'ADP checkout' is already in the 'ADP' Project."
        )

        result = json.loads(
            tools["move_scenario"](scenario=SCENARIO_ID, to_project="ADP", dry_run=False)
        )

        assert result["error"] == "Scenario 'ADP checkout' is already in the 'ADP' Project."


class TestMoveScenarioAnnotations:
    def test_declared_non_destructive_non_readonly_non_idempotent(self):
        from mcp.server.fastmcp import FastMCP
        from src.tools.scenarios import register_tools

        mcp = FastMCP("test")
        register_tools(mcp)
        tool = mcp._tool_manager._tools["move_scenario"]

        assert tool.annotations.readOnlyHint is False
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is False

    def test_description_carries_the_dry_run_first_protocol(self):
        from mcp.server.fastmcp import FastMCP
        from src.tools.scenarios import register_tools

        mcp = FastMCP("test")
        register_tools(mcp)
        description = mcp._tool_manager._tools["move_scenario"].description

        assert "dry" in description.lower()
        assert "confirm" in description.lower()
        assert "per Scenario" in description
