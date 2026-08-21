"""Unit tests for the clone_project tool (039-server-side-clone).

Mock-based, mirroring tests/unit/test_move_scenario.py: the FastMCP registry
is built for real, the Okareo client / project resolution / raw HTTP helper
are patched at the src.tools.clone seam. The server owns every clone rule
(POST /v0/projects/{id}/clone); these tests pin the tool to a single call
that relays the server's ProjectClonePlan verbatim.
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.error_handling import ProjectNotFound
from src.okareo_client import ResolvedProject

_PATCH_GET_CLIENT = "src.tools.clone.get_okareo_client"
_PATCH_RESOLVE_PROJECT = "src.tools.clone.resolve_project"
_PATCH_API_REQUEST = "src.tools.clone.okareo_api_request"
_PATCH_INVALIDATE = "src.tools.clone.invalidate_projects_cache"

SOURCE_ID = "11111111-1111-4111-8111-111111111111"
DEST_ID = "22222222-2222-4222-8222-222222222222"

SOURCE = ResolvedProject(id=SOURCE_ID, name="WISMO Golden", basis="explicit")


def _register_and_get_tools():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    from src.tools.clone import register_tools

    register_tools(mcp)
    return {name: tool.fn for name, tool in mcp._tool_manager._tools.items()}


@pytest.fixture
def tools():
    return _register_and_get_tools()


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setenv("OKAREO_API_KEY", "test-api-key-12345")


def _make_plan(**overrides):
    plan = {
        "source_project_id": SOURCE_ID,
        "source_project_name": "WISMO Golden",
        "new_project_name": "ADP",
        "destination_project_id": None,
        "counts": {"project": 1, "scenario_set": 3, "scenario_data_point": 340},
        "scenarios": [
            {"name": "order-status-happy-path", "row_count": 120},
            {"name": "order-status-adversarial", "row_count": 200},
            {"name": "seed-smalltalk", "row_count": 20},
        ],
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


class TestSchemaAndDescription:
    def _tool(self):
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test")
        from src.tools.clone import register_tools

        register_tools(mcp)
        return mcp._tool_manager._tools["clone_project"]

    def test_parameters_are_exactly_the_contracted_three(self):
        props = self._tool().parameters["properties"]
        assert set(props) == {"source_project", "new_project_name", "dry_run"}
        assert set(self._tool().parameters["required"]) == {
            "source_project",
            "new_project_name",
        }

    def test_dry_run_defaults_to_true(self):
        """039 flips 037's default: a bare call can never mutate, matching
        move_scenario's dry-run-first mechanics."""
        props = self._tool().parameters["properties"]
        assert props["dry_run"]["default"] is True

    def test_description_carries_the_dry_run_first_protocol(self):
        description = self._tool().description.lower()
        assert "dry" in description
        assert "confirm" in description
        assert "clone" in description
        # 039: the rules live server-side, and the description says so.
        assert "server" in description

    def test_annotations_match_move_scenario(self):
        """A repeat real clone now 409s on the name collision — no longer
        idempotent (unlike 037's resume semantics)."""
        annotations = self._tool().annotations
        assert annotations.readOnlyHint is False
        assert annotations.destructiveHint is False
        assert annotations.idempotentHint is False
        assert annotations.openWorldHint is False


class TestCloneDryRunFirst:
    @patch(_PATCH_INVALIDATE)
    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT, return_value=SOURCE)
    @patch(_PATCH_GET_CLIENT)
    def test_dry_run_is_the_default_and_relays_the_plan(
        self, mock_client, mock_resolve, mock_api, mock_invalidate, tools
    ):
        plan = _make_plan()
        mock_api.return_value = plan

        result = json.loads(
            tools["clone_project"](
                source_project="WISMO Golden", new_project_name="ADP"
            )
        )

        # One call, the pinned contract exactly: path, body, query.
        assert mock_api.call_count == 1
        args, kwargs = mock_api.call_args
        assert args[1] == "post"
        assert args[2] == f"/v0/projects/{SOURCE_ID}/clone"
        assert kwargs["json"] == {"new_project_name": "ADP"}
        assert kwargs["params"] == {"dry_run": "true"}

        assert result["executed"] is False
        assert result["plan"] == plan  # verbatim relay
        assert result["source_project"] == SOURCE.as_dict()
        assert "dry run" in result["next_step"].lower()
        assert "confirm" in result["next_step"].lower()
        assert not mock_invalidate.called

    @patch(_PATCH_INVALIDATE)
    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT, return_value=SOURCE)
    @patch(_PATCH_GET_CLIENT)
    def test_source_resolves_explicitly(
        self, mock_client, mock_resolve, mock_api, mock_invalidate, tools
    ):
        mock_api.return_value = _make_plan()

        tools["clone_project"](source_project="WISMO Golden", new_project_name="ADP")

        assert mock_resolve.call_args[0][1] == "WISMO Golden"

    @patch(_PATCH_INVALIDATE)
    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT, return_value=SOURCE)
    @patch(_PATCH_GET_CLIENT)
    def test_padded_new_name_is_stripped_before_sending(
        self, mock_client, mock_resolve, mock_api, mock_invalidate, tools
    ):
        mock_api.return_value = _make_plan()

        tools["clone_project"](
            source_project="WISMO Golden", new_project_name="  ADP  "
        )

        _, kwargs = mock_api.call_args
        assert kwargs["json"] == {"new_project_name": "ADP"}

    @patch(_PATCH_INVALIDATE)
    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT, return_value=SOURCE)
    @patch(_PATCH_GET_CLIENT)
    def test_execute_clone_invalidates_the_projects_cache(
        self, mock_client, mock_resolve, mock_api, mock_invalidate, tools
    ):
        """After a real clone the new Project must be immediately visible to
        select_project — the 60-second project cache is dropped (037 CR-1,
        unchanged in 039)."""
        mock_api.return_value = _make_plan(
            executed=True, destination_project_id=DEST_ID
        )

        result = json.loads(
            tools["clone_project"](
                source_project="WISMO Golden",
                new_project_name="ADP",
                dry_run=False,
            )
        )

        _, kwargs = mock_api.call_args
        assert kwargs["params"] is None
        assert result["executed"] is True
        assert result["plan"]["destination_project_id"] == DEST_ID
        assert "next_step" not in result  # executed style matches move_scenario
        assert mock_invalidate.call_count == 1

    @patch(_PATCH_INVALIDATE)
    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT, return_value=SOURCE)
    @patch(_PATCH_GET_CLIENT)
    def test_failed_execute_does_not_invalidate_the_cache(
        self, mock_client, mock_resolve, mock_api, mock_invalidate, tools
    ):
        mock_api.side_effect = _http_error(
            409,
            _make_plan(
                blockers=[
                    {"code": "destination_name_collision", "detail": "taken"}
                ]
            ),
        )

        tools["clone_project"](
            source_project="WISMO Golden", new_project_name="ADP", dry_run=False
        )

        assert not mock_invalidate.called


class TestCloneRefusals:
    @patch(_PATCH_INVALIDATE)
    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT, return_value=SOURCE)
    @patch(_PATCH_GET_CLIENT)
    def test_409_is_a_structured_blocked_response_never_a_retry(
        self, mock_client, mock_resolve, mock_api, mock_invalidate, tools
    ):
        blocked_plan = _make_plan(
            blockers=[
                {
                    "code": "destination_name_collision",
                    "detail": (
                        "A Project named 'ADP' already exists. "
                        "Choose a different name."
                    ),
                }
            ]
        )
        mock_api.side_effect = _http_error(409, blocked_plan)

        result = json.loads(
            tools["clone_project"](
                source_project="WISMO Golden",
                new_project_name="ADP",
                dry_run=False,
            )
        )

        assert result["blocked"] is True
        assert result["plan"] == blocked_plan  # the 409 detail, verbatim
        assert (
            result["plan"]["blockers"][0]["code"] == "destination_name_collision"
        )
        # The message is the server's own blocker copy, joined — never a
        # client-side paraphrase.
        assert "A Project named 'ADP' already exists." in result["message"]
        assert result["source_project"] == SOURCE.as_dict()
        assert mock_api.call_count == 1  # no silent retry

    @patch(_PATCH_INVALIDATE)
    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT, return_value=SOURCE)
    @patch(_PATCH_GET_CLIENT)
    def test_409_joins_every_blocker_detail(
        self, mock_client, mock_resolve, mock_api, mock_invalidate, tools
    ):
        blocked_plan = _make_plan(
            new_project_name="Global",
            blockers=[
                {
                    "code": "destination_name_collision",
                    "detail": (
                        "A Project named 'Global' already exists. "
                        "Choose a different name."
                    ),
                },
                {
                    "code": "destination_name_reserved",
                    "detail": (
                        "'Global' is reserved for the default Project. "
                        "Choose a different name."
                    ),
                },
            ],
        )
        mock_api.side_effect = _http_error(409, blocked_plan)

        result = json.loads(
            tools["clone_project"](
                source_project="WISMO Golden",
                new_project_name="Global",
                dry_run=False,
            )
        )

        assert "A Project named 'Global' already exists." in result["message"]
        assert "'Global' is reserved for the default Project." in result["message"]

    @patch(_PATCH_INVALIDATE)
    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT, return_value=SOURCE)
    @patch(_PATCH_GET_CLIENT)
    def test_503_relays_the_rollback_and_retry_guidance(
        self, mock_client, mock_resolve, mock_api, mock_invalidate, tools
    ):
        mock_api.side_effect = _http_error(
            503, "The clone timed out and was rolled back — nothing was created."
        )

        result = json.loads(
            tools["clone_project"](
                source_project="WISMO Golden",
                new_project_name="ADP",
                dry_run=False,
            )
        )

        assert "timed out" in result["error"]
        assert result["retryable"] is True
        assert mock_api.call_count == 1

    @patch(_PATCH_INVALIDATE)
    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT, return_value=SOURCE)
    @patch(_PATCH_GET_CLIENT)
    def test_other_statuses_relay_the_server_detail(
        self, mock_client, mock_resolve, mock_api, mock_invalidate, tools
    ):
        mock_api.side_effect = _http_error(404, "Project not found.")

        result = json.loads(
            tools["clone_project"](
                source_project="WISMO Golden", new_project_name="ADP"
            )
        )

        assert result["error"] == "Project not found."

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_unknown_source_project_reports_not_found(
        self, mock_client, mock_api, tools
    ):
        with patch(
            _PATCH_RESOLVE_PROJECT,
            side_effect=ProjectNotFound("No project named 'Nope'", projects=[]),
        ):
            result = json.loads(
                tools["clone_project"](
                    source_project="Nope", new_project_name="ADP"
                )
            )

        assert result["error"]["code"] == "project_not_found"
        assert mock_api.call_count == 0


class TestBlankInputRefusals:
    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT)
    @patch(_PATCH_GET_CLIENT)
    def test_blank_source_project_is_rejected_not_defaulted(
        self, mock_client, mock_resolve, mock_api, tools
    ):
        """The source is explicit always — never pin/default fallback
        (037 FR-007, unchanged in 039)."""
        result = json.loads(
            tools["clone_project"](source_project="   ", new_project_name="ADP")
        )

        assert result["error"]["code"] == "clone_source_required"
        assert not mock_resolve.called
        assert not mock_api.called

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_RESOLVE_PROJECT)
    @patch(_PATCH_GET_CLIENT)
    def test_blank_new_project_name_is_rejected(
        self, mock_client, mock_resolve, mock_api, tools
    ):
        result = json.loads(
            tools["clone_project"](
                source_project="WISMO Golden", new_project_name="   "
            )
        )

        assert result["error"]["code"] == "clone_destination_required"
        assert not mock_api.called
