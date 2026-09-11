"""Unit tests for the calibrate_check tool (042-calibrate-check).

Mock-based, modelled on tests/unit/test_clone_project.py: the FastMCP registry
is built for real and the Okareo client / raw HTTP helper are patched at the
``src.tools.checks`` seam. The backend owns calibration; this tool is a thin,
honest relay of ``POST /v0/test_runs/{test_run_id}/calibrate_check`` plus a
set of local refusals that must never reach the network.

The both-keys refusal is the load-bearing one: a ``check_config`` carrying
both ``code_contents`` and ``prompt_template`` is the request shape that, if a
server ever routed on the caller-declared type instead of key presence, would
reach an unrestricted ``exec``. The tool refuses to construct it at all.
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

_PATCH_GET_CLIENT = "src.tools.checks.get_okareo_client"
_PATCH_API_REQUEST = "src.tools.checks.okareo_api_request"

TEST_RUN_ID = "33333333-3333-4333-8333-333333333333"
TDP_ID = "44444444-4444-4444-8444-444444444444"

PROMPT = "Does {model_output} answer {scenario_input}? Answer true or false."
CODE = (
    "from okareo.checks import CodeBasedCheck, CheckResponse\n"
    "class Check(CodeBasedCheck):\n"
    "    @staticmethod\n"
    "    def evaluate(model_output, metadata):\n"
    "        return CheckResponse(score=True, explanation='ok')\n"
)

# The 10 current check template variables and the 5 rejected aliases.
CURRENT_PLACEHOLDERS = [
    "model_output",
    "scenario_input",
    "scenario_result",
    "model_input",
    "message_history",
    "tool_calls",
    "tools",
    "model_output_metadata",
    "simulation_message_history",
    "user_only_audio",
]
DEPRECATED_ALIASES = ["generation", "input", "result", "audio_messages", "audio_output"]


def _register_and_get_tools():
    from mcp.server.fastmcp import FastMCP

    from src.tools.checks import register_tools

    mcp = FastMCP("test")
    register_tools(mcp)
    return {name: tool.fn for name, tool in mcp._tool_manager._tools.items()}


def _registered_tool(name: str):
    from mcp.server.fastmcp import FastMCP

    from src.tools.checks import register_tools

    mcp = FastMCP("test")
    register_tools(mcp)
    return mcp._tool_manager._tools[name]


@pytest.fixture
def tools():
    return _register_and_get_tools()


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setenv("OKAREO_API_KEY", "test-api-key-12345")


def _response(**overrides):
    payload = {
        "test_run_id": TEST_RUN_ID,
        "name": "draft_check",
        "check_config": {"prompt_template": PROMPT, "type": "pass_fail", "audio": False},
        "check_flow": "model",
        "inspect_only": False,
        "row_count": 1,
        "total_row_count": 1,
        "truncated": False,
        "rows": [
            {
                "test_data_point_id": TDP_ID,
                "arguments": {"model_output": "Yes."},
                "result": {
                    "score": 1.0,
                    "explanation": "The answer is responsive.",
                    "check_metadata": {"latency": 812.0, "cost": 0.0004},
                    "error": None,
                },
                "error": None,
            }
        ],
    }
    payload.update(overrides)
    return payload


def _http_error(status: int, detail):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = {"detail": detail}
    return httpx.HTTPStatusError("boom", request=MagicMock(), response=response)


class TestSchemaAndDescription:
    def test_title_is_human_readable(self):
        assert _registered_tool("calibrate_check").title == "Calibrate Check"

    def test_all_four_annotation_hints_are_explicit_bools(self):
        annotations = _registered_tool("calibrate_check").annotations
        assert annotations.readOnlyHint is True  # FR-9: persists nothing
        assert annotations.destructiveHint is False
        # Judge verdicts are not reproducible run to run.
        assert annotations.idempotentHint is False
        # Every evaluated row makes a real LLM judge call.
        assert annotations.openWorldHint is True

    def test_parameters_are_exactly_the_contracted_set(self):
        props = _registered_tool("calibrate_check").parameters["properties"]
        assert set(props) == {
            "test_run_id",
            "check_type",
            "prompt_template",
            "code_contents",
            "output_type",
            "is_audio",
            "inspect_only",
            "name",
        }
        assert set(_registered_tool("calibrate_check").parameters["required"]) == {
            "test_run_id",
            "check_type",
        }

    def test_no_project_parameter(self):
        """No scoping decorator: nothing is saved, and the Test Run id already
        identifies the project (precedent: clone_project)."""
        props = _registered_tool("calibrate_check").parameters["properties"]
        assert "project" not in props

    def test_description_names_every_current_placeholder(self):
        description = _registered_tool("calibrate_check").description
        for placeholder in CURRENT_PLACEHOLDERS:
            assert "{" + placeholder + "}" in description

    def test_description_never_offers_a_rejected_alias(self):
        """The aliases appear only inside the rejection note, which says they
        are refused — never as something to use."""
        description = _registered_tool("calibrate_check").description
        note_start = description.index("Okareo rejects any placeholder")
        offered = description[:note_start]
        for alias in DEPRECATED_ALIASES:
            assert "{" + alias + "}" not in offered

    def test_description_states_the_expensive_and_free_modes(self):
        description = _registered_tool("calibrate_check").description.lower()
        assert "inspect_only" in description
        assert "persists nothing" in description or "nothing is saved" in description
        assert "100" in description


class TestRequestShape:
    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_model_draft_posts_the_four_request_keys(self, mock_client, mock_api, tools):
        mock_api.return_value = _response()

        tools["calibrate_check"](
            test_run_id=TEST_RUN_ID,
            check_type="model",
            prompt_template=PROMPT,
            output_type="pass_fail",
        )

        assert mock_api.call_count == 1
        args, kwargs = mock_api.call_args
        assert args[1] == "post"
        assert args[2] == f"/v0/test_runs/{TEST_RUN_ID}/calibrate_check"
        assert set(kwargs["json"]) == {
            "check_config",
            "check_type",
            "name",
            "inspect_only",
        }

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_model_config_matches_create_or_update_check(
        self, mock_client, mock_api, tools
    ):
        """Byte-for-byte the config create_or_update_check builds, so a
        calibrated draft saves unchanged."""
        mock_api.return_value = _response()

        tools["calibrate_check"](
            test_run_id=TEST_RUN_ID,
            check_type="model",
            prompt_template=PROMPT,
            output_type="score",
        )

        _, kwargs = mock_api.call_args
        assert kwargs["json"]["check_config"] == {
            "prompt_template": PROMPT,
            "type": "score",
            "audio": False,
        }
        assert kwargs["json"]["check_type"] == "model"
        assert kwargs["json"]["name"] == "draft_check"

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_code_config_carries_only_code_contents(self, mock_client, mock_api, tools):
        """No inferred output type: FR-13 echoes the config verbatim, and
        create_or_update_check sends no 'type' for code checks either."""
        mock_api.return_value = _response(check_flow="code")

        tools["calibrate_check"](
            test_run_id=TEST_RUN_ID, check_type="code", code_contents=CODE
        )

        _, kwargs = mock_api.call_args
        assert kwargs["json"]["check_config"] == {"code_contents": CODE}
        assert kwargs["json"]["check_type"] == "code"

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_audio_draft_declares_the_audio_wire_type(
        self, mock_client, mock_api, tools
    ):
        mock_api.return_value = _response()

        tools["calibrate_check"](
            test_run_id=TEST_RUN_ID,
            check_type="model",
            prompt_template=PROMPT,
            output_type="pass_fail",
            is_audio=True,
        )

        _, kwargs = mock_api.call_args
        assert kwargs["json"]["check_type"] == "audio"
        assert kwargs["json"]["check_config"]["audio"] is True

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_inspect_only_and_name_are_forwarded(self, mock_client, mock_api, tools):
        mock_api.return_value = _response(inspect_only=True)

        tools["calibrate_check"](
            test_run_id=TEST_RUN_ID,
            check_type="model",
            prompt_template=PROMPT,
            output_type="pass_fail",
            inspect_only=True,
            name="wismo_tone_v3",
        )

        _, kwargs = mock_api.call_args
        assert kwargs["json"]["inspect_only"] is True
        assert kwargs["json"]["name"] == "wismo_tone_v3"

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_client_timeout_outlives_the_server_wall_clock(
        self, mock_client, mock_api, tools
    ):
        """The server's own 600s timeout must win the race, so the client
        waits longer. okareo_api_request has no timeout by default (httpx
        would wait forever)."""
        from src.tools.checks import _CALIBRATE_TIMEOUT_SECONDS

        mock_api.return_value = _response()

        tools["calibrate_check"](
            test_run_id=TEST_RUN_ID,
            check_type="model",
            prompt_template=PROMPT,
            output_type="pass_fail",
        )

        _, kwargs = mock_api.call_args
        assert kwargs["timeout"] == _CALIBRATE_TIMEOUT_SECONDS
        # The server tests its wall clock between rows, never inside one, so a
        # row starting at 599s still runs to completion: one judge call plus
        # its retry, each bounded by the backend's LLM_TIMEOUT. The client must
        # outlast that worst case, not just the 600s clock itself.
        server_wall_clock_seconds = 600
        judge_call_timeout_seconds = 30
        worst_case_final_row_seconds = 2 * judge_call_timeout_seconds
        assert (
            _CALIBRATE_TIMEOUT_SECONDS
            > server_wall_clock_seconds + worst_case_final_row_seconds
        )


class TestResponseRelay:
    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_response_is_relayed_verbatim_with_only_next_step_added(
        self, mock_client, mock_api, tools
    ):
        payload = _response()
        mock_api.return_value = payload

        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID,
                check_type="model",
                prompt_template=PROMPT,
                output_type="pass_fail",
            )
        )

        assert set(result) - set(payload) == {"next_step"}
        for key, value in payload.items():
            assert result[key] == value

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_next_step_points_at_create_or_update_check(
        self, mock_client, mock_api, tools
    ):
        mock_api.return_value = _response()

        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID,
                check_type="model",
                prompt_template=PROMPT,
                output_type="pass_fail",
            )
        )

        assert "create_or_update_check" in result["next_step"]

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_next_step_names_every_argument_create_or_update_check_requires(
        self, mock_client, mock_api, tools
    ):
        """An agent that follows next_step verbatim must not be missing a
        required argument when it calls the save tool."""
        mock_api.return_value = _response()

        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID,
                check_type="model",
                prompt_template=PROMPT,
                output_type="pass_fail",
            )
        )

        next_step = result["next_step"]
        for argument in ("name", "description", "check_type", "output_type"):
            assert argument in next_step
        assert "prompt_template" in next_step
        assert "code_contents" in next_step

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_inspect_only_next_step_points_at_calibrating_not_saving(
        self, mock_client, mock_api, tools
    ):
        """inspect_only runs no judge, so there are no verdicts to look right.
        Telling the caller to save on the strength of them is nonsense."""
        mock_api.return_value = _response(inspect_only=True, rows=[])

        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID,
                check_type="model",
                prompt_template=PROMPT,
                output_type="pass_fail",
                inspect_only=True,
            )
        )

        next_step = result["next_step"]
        assert "inspect_only=false" in next_step
        assert "no verdicts" in next_step
        assert "When the verdicts look right" not in next_step

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_echoed_check_config_is_not_rewritten(self, mock_client, mock_api, tools):
        """FR-13: whatever the server echoes back is what the caller sees."""
        echoed = {"prompt_template": PROMPT, "type": "pass_fail", "audio": False}
        mock_api.return_value = _response(check_config=echoed)

        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID,
                check_type="model",
                prompt_template=PROMPT,
                output_type="pass_fail",
            )
        )

        assert result["check_config"] == echoed


class TestLocalRefusals:
    """Every one of these must refuse without touching the network."""

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_both_config_keys_is_never_constructed(self, mock_client, mock_api, tools):
        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID,
                check_type="model",
                prompt_template=PROMPT,
                code_contents=CODE,
                output_type="pass_fail",
            )
        )

        assert "error" in result
        assert not mock_api.called

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_bad_check_type(self, mock_client, mock_api, tools):
        result = json.loads(
            tools["calibrate_check"](test_run_id=TEST_RUN_ID, check_type="judge")
        )

        assert "check_type" in result["error"]
        assert not mock_api.called

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_model_draft_without_a_prompt(self, mock_client, mock_api, tools):
        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID, check_type="model", output_type="pass_fail"
            )
        )

        assert "prompt_template" in result["error"]
        assert not mock_api.called

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_code_draft_without_code(self, mock_client, mock_api, tools):
        result = json.loads(
            tools["calibrate_check"](test_run_id=TEST_RUN_ID, check_type="code")
        )

        assert "code_contents" in result["error"]
        assert not mock_api.called

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_model_draft_without_an_output_type(self, mock_client, mock_api, tools):
        """An absent config 'type' errors on every row server-side."""
        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID, check_type="model", prompt_template=PROMPT
            )
        )

        assert "output_type" in result["error"]
        assert not mock_api.called

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_model_draft_with_a_bad_output_type(self, mock_client, mock_api, tools):
        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID,
                check_type="model",
                prompt_template=PROMPT,
                output_type="rubric",
            )
        )

        assert "output_type" in result["error"]
        assert not mock_api.called

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_audio_with_a_code_draft(self, mock_client, mock_api, tools):
        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID,
                check_type="code",
                code_contents=CODE,
                is_audio=True,
            )
        )

        assert "error" in result
        assert not mock_api.called

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_name_is_sent_stripped(self, mock_client, mock_api, tools):
        """The blank check already strips; the value sent did not."""
        mock_api.return_value = _response()

        tools["calibrate_check"](
            test_run_id=TEST_RUN_ID,
            check_type="model",
            prompt_template=PROMPT,
            output_type="pass_fail",
            name="  wismo_tone_v3\n",
        )

        _, kwargs = mock_api.call_args
        assert kwargs["json"]["name"] == "wismo_tone_v3"

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_name_longer_than_the_route_allows_is_refused_locally(
        self, mock_client, mock_api, tools
    ):
        """Mirrors CheckCalibrateRequest.name's max_length of 200."""
        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID,
                check_type="model",
                prompt_template=PROMPT,
                output_type="pass_fail",
                name="x" * 201,
            )
        )

        assert "name" in result["error"]
        assert "200" in result["error"]
        assert not mock_api.called

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_a_name_at_the_bound_is_allowed(self, mock_client, mock_api, tools):
        mock_api.return_value = _response()

        tools["calibrate_check"](
            test_run_id=TEST_RUN_ID,
            check_type="model",
            prompt_template=PROMPT,
            output_type="pass_fail",
            name="x" * 200,
        )

        assert mock_api.call_count == 1

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_test_run_id_must_be_a_uuid(self, mock_client, mock_api, tools):
        result = json.loads(
            tools["calibrate_check"](
                test_run_id="my latest run",
                check_type="model",
                prompt_template=PROMPT,
                output_type="pass_fail",
            )
        )

        assert "test_run_id" in result["error"]
        assert "list_test_runs" in json.dumps(result)
        assert not mock_api.called


class TestServerRefusals:
    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_422_detail_reaches_the_caller_verbatim(self, mock_client, mock_api, tools):
        """The feedback loop the feature exists for: the message names the
        offending placeholder, and the agent fixes it and re-calibrates."""
        detail = (
            "Unrecognized template variable(s): tool_call. Valid variables: "
            "model_output, scenario_input, scenario_result, model_input, "
            "message_history, tool_calls, tools, model_output_metadata, "
            "simulation_message_history, user_only_audio."
        )
        mock_api.side_effect = _http_error(422, detail)

        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID,
                check_type="model",
                prompt_template="Judge {tool_call}.",
                output_type="pass_fail",
            )
        )

        assert result["error"] == detail
        assert mock_api.call_count == 1  # no silent retry

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_422_for_a_still_running_run_says_wait_not_rewrite(
        self, mock_client, mock_api, tools
    ):
        """The route 422s on two unrelated things. TestRunLookup raises this
        one when the Run has not finished — the draft is fine, and telling the
        agent to rewrite it is the one instruction that cannot help."""
        detail = (
            f"Source test run '{TEST_RUN_ID}' is not re-evaluatable while "
            "status is 'RUNNING'."
        )
        mock_api.side_effect = _http_error(422, detail)

        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID,
                check_type="model",
                prompt_template=PROMPT,
                output_type="pass_fail",
            )
        )

        assert result["error"] == detail
        assert result["retryable"] is True
        suggestion = result["suggestion"]
        assert "wait for it to finish" in suggestion
        assert "same draft" in suggestion
        assert "Do not rewrite the draft." in suggestion

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_422_for_an_unsupported_run_type_is_not_a_draft_problem(
        self, mock_client, mock_api, tools
    ):
        """The other run-eligibility 422: the Run's type is one rescore does
        not handle. Same draft, different run — not a rewrite."""
        detail = (
            f"Source test run '{TEST_RUN_ID}' has unsupported type "
            "'MULTI_CLASS_CLASSIFICATION' for re-evaluate. Allowed types: "
            "MULTI_TURN, NL_GENERATION."
        )
        mock_api.side_effect = _http_error(422, detail)

        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID,
                check_type="model",
                prompt_template=PROMPT,
                output_type="pass_fail",
            )
        )

        assert result["error"] == detail
        assert result["retryable"] is True
        assert "Do not rewrite the draft." in result["suggestion"]

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_draft_shaped_422_still_says_rewrite_and_is_not_retryable(
        self, mock_client, mock_api, tools
    ):
        """The other 422 shape must keep its own advice: the run is eligible,
        the draft is not."""
        mock_api.side_effect = _http_error(
            422, "Unrecognized template variable(s): tool_call."
        )

        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID,
                check_type="model",
                prompt_template="Judge {tool_call}.",
                output_type="pass_fail",
            )
        )

        assert result["retryable"] is False
        assert "Fix the draft" in result["suggestion"]

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_404_points_at_list_test_runs(self, mock_client, mock_api, tools):
        mock_api.side_effect = _http_error(404, "Test run not found.")

        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID,
                check_type="model",
                prompt_template=PROMPT,
                output_type="pass_fail",
            )
        )

        assert "list_test_runs" in json.dumps(result)

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_504_is_retryable_and_suggests_inspect_only(
        self, mock_client, mock_api, tools
    ):
        """Pins the Phase 04 seam: the server's wall clock returns 504, not
        422. If that status changes, this test fails."""
        mock_api.side_effect = _http_error(
            504, "Calibration exceeded its 600 second time limit."
        )

        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID,
                check_type="model",
                prompt_template=PROMPT,
                output_type="pass_fail",
            )
        )

        assert result["retryable"] is True
        assert "inspect_only" in json.dumps(result)
        assert mock_api.call_count == 1

    @patch(_PATCH_API_REQUEST)
    @patch(_PATCH_GET_CLIENT)
    def test_other_statuses_relay_the_server_detail(self, mock_client, mock_api, tools):
        mock_api.side_effect = _http_error(400, "Something went wrong.")

        result = json.loads(
            tools["calibrate_check"](
                test_run_id=TEST_RUN_ID,
                check_type="model",
                prompt_template=PROMPT,
                output_type="pass_fail",
            )
        )

        assert result["error"] == "Something went wrong."
