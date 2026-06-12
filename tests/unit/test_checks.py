"""Unit tests for check management tools."""

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register_and_get_tools():
    """Register check tools on a mock MCP and return the tool functions."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")

    from src.tools.checks import register_tools
    register_tools(mcp)

    tools = {}
    for name, tool in mcp._tool_manager._tools.items():
        tools[name] = tool.fn
    return tools


@pytest.fixture
def tools():
    return _register_and_get_tools()


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setenv("OKAREO_API_KEY", "test-api-key-12345")


def _make_mock_response(check_id="test-uuid-123", name="test-check"):
    """Create a mock EvaluatorDetailedResponse."""
    resp = MagicMock()
    resp.id = check_id
    resp.name = name
    resp.description = "Test description"
    resp.output_data_type = "bool"
    resp.check_config = MagicMock()
    resp.check_config.additional_properties = {"prompt_template": "test", "type": "pass_fail"}
    resp.code_contents = ""
    resp.requires_scenario_input = False
    resp.requires_scenario_result = False
    resp.is_predefined = False
    resp.time_created = "2026-03-03T10:00:00Z"
    return resp


def _make_mock_check_brief(name="test-check", check_id="test-uuid-123", is_predefined=False):
    """Create a mock check brief (from get_all_checks)."""
    brief = MagicMock()
    brief.name = name
    brief.id = check_id
    brief.description = "Test description"
    brief.output_data_type = "bool"
    brief.is_predefined = is_predefined
    return brief


# ---------------------------------------------------------------------------
# Tool Registration
# ---------------------------------------------------------------------------

class TestToolRegistration:
    def test_four_tools_registered(self, tools):
        assert len(tools) == 4

    def test_tool_names(self, tools):
        expected = ["create_or_update_check", "generate_check", "get_check", "delete_check"]
        for name in expected:
            assert name in tools, f"Expected tool '{name}' to be registered"


# ---------------------------------------------------------------------------
# create_or_update_check — Validation
# ---------------------------------------------------------------------------

class TestCreateOrUpdateCheckValidation:
    def test_invalid_check_type(self, tools):
        result = json.loads(tools["create_or_update_check"](
            name="test", description="test", check_type="invalid", output_type="pass_fail",
        ))
        assert "error" in result
        assert "check_type" in result["error"]

    def test_invalid_output_type(self, tools):
        result = json.loads(tools["create_or_update_check"](
            name="test", description="test", check_type="model", output_type="invalid",
        ))
        assert "error" in result
        assert "output_type" in result["error"]

    def test_empty_name(self, tools):
        result = json.loads(tools["create_or_update_check"](
            name="", description="test", check_type="model", output_type="pass_fail",
        ))
        assert "error" in result
        assert "name" in result["error"]

    def test_analysis_with_code_rejected(self, tools):
        result = json.loads(tools["create_or_update_check"](
            name="test", description="test", check_type="code", output_type="analysis",
        ))
        assert "error" in result
        assert "analysis" in result["error"]

    def test_audio_with_code_rejected(self, tools):
        result = json.loads(tools["create_or_update_check"](
            name="test", description="test", check_type="code", output_type="pass_fail",
            code_contents="def test(): return True", is_audio=True,
        ))
        assert "error" in result
        assert "Audio" in result["error"]

    def test_model_missing_prompt_template(self, tools):
        result = json.loads(tools["create_or_update_check"](
            name="test", description="test", check_type="model", output_type="pass_fail",
        ))
        assert "error" in result
        assert "prompt_template" in result["error"]

    def test_code_missing_code_contents(self, tools):
        result = json.loads(tools["create_or_update_check"](
            name="test", description="test", check_type="code", output_type="pass_fail",
        ))
        assert "error" in result
        assert "code_contents" in result["error"]


# ---------------------------------------------------------------------------
# create_or_update_check — Model-based (pass_fail, score, analysis)
# ---------------------------------------------------------------------------

class TestCreateModelBasedCheck:
    @patch("src.tools.checks.get_okareo_client")
    def test_model_pass_fail(self, mock_get_client, tools):
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo
        mock_okareo.create_or_update_check.return_value = _make_mock_response()

        result = json.loads(tools["create_or_update_check"](
            name="politeness-check",
            description="Check politeness",
            check_type="model",
            output_type="pass_fail",
            prompt_template="Is this polite? {model_output}",
        ))

        assert result["created"] is True
        assert result["name"] == "politeness-check"
        assert result["check_type"] == "model"
        assert result["output_type"] == "pass_fail"
        mock_okareo.create_or_update_check.assert_called_once()

    @patch("src.tools.checks.get_okareo_client")
    def test_model_score(self, mock_get_client, tools):
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo
        mock_okareo.create_or_update_check.return_value = _make_mock_response()

        result = json.loads(tools["create_or_update_check"](
            name="quality-score",
            description="Score quality 1-5",
            check_type="model",
            output_type="score",
            prompt_template="Score this: {model_output}",
        ))

        assert result["created"] is True
        assert result["output_type"] == "score"

    @patch("src.tools.checks.get_okareo_client")
    def test_model_analysis(self, mock_get_client, tools):
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo

        # For analysis, we bypass SDK and use low-level API
        with patch(
            "okareo_api_client.api.default.check_create_or_update_v0_check_create_or_update_post.sync"
        ) as mock_api:
            mock_api.return_value = _make_mock_response()

            result = json.loads(tools["create_or_update_check"](
                name="analysis-check",
                description="Analyze quality",
                check_type="model",
                output_type="analysis",
                prompt_template="Analyze this: {model_output}",
            ))

            assert result["created"] is True
            assert result["output_type"] == "analysis"
            mock_api.assert_called_once()


# ---------------------------------------------------------------------------
# create_or_update_check — Code-based (pass_fail, score)
# ---------------------------------------------------------------------------

class TestCreateCodeBasedCheck:
    @patch("src.tools.checks.get_okareo_client")
    def test_code_pass_fail(self, mock_get_client, tools):
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo

        with patch(
            "okareo_api_client.api.default.check_create_or_update_v0_check_create_or_update_post.sync"
        ) as mock_api:
            mock_api.return_value = _make_mock_response()

            result = json.loads(tools["create_or_update_check"](
                name="json-check",
                description="Check JSON validity",
                check_type="code",
                output_type="pass_fail",
                code_contents="import json\ndef evaluate(model_output):\n    try:\n        json.loads(model_output)\n        return True\n    except:\n        return False",
            ))

            assert result["created"] is True
            assert result["check_type"] == "code"
            assert result["output_type"] == "pass_fail"

    @patch("src.tools.checks.get_okareo_client")
    def test_code_score(self, mock_get_client, tools):
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo

        with patch(
            "okareo_api_client.api.default.check_create_or_update_v0_check_create_or_update_post.sync"
        ) as mock_api:
            mock_api.return_value = _make_mock_response()

            result = json.loads(tools["create_or_update_check"](
                name="word-count",
                description="Count words",
                check_type="code",
                output_type="score",
                code_contents="def evaluate(model_output):\n    return len(model_output.split())",
            ))

            assert result["created"] is True
            assert result["output_type"] == "score"


# ---------------------------------------------------------------------------
# create_or_update_check — Audio
# ---------------------------------------------------------------------------

class TestCreateAudioCheck:
    @patch("src.tools.checks.get_okareo_client")
    def test_audio_model_pass_fail(self, mock_get_client, tools):
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo
        mock_okareo.create_or_update_check.return_value = _make_mock_response()

        result = json.loads(tools["create_or_update_check"](
            name="audio-empathy",
            description="Check empathy in voice",
            check_type="model",
            output_type="pass_fail",
            prompt_template="Is this empathetic? {model_output}",
            is_audio=True,
        ))

        assert result["created"] is True
        assert result["is_audio"] is True

        # Verify ModelBasedCheck was called with is_audio=True
        call_args = mock_okareo.create_or_update_check.call_args
        check_arg = call_args.kwargs.get("check") or call_args[1].get("check")
        assert check_arg.is_audio is True


# ---------------------------------------------------------------------------
# generate_check
# ---------------------------------------------------------------------------

class TestGenerateCheck:
    def test_invalid_check_type(self, tools):
        result = json.loads(tools["generate_check"](
            name="test", description="test", check_type="invalid",
        ))
        assert "error" in result

    def test_invalid_output_type(self, tools):
        result = json.loads(tools["generate_check"](
            name="test", description="test", output_type="invalid",
        ))
        assert "error" in result

    @patch("src.tools.checks.get_okareo_client")
    def test_generate_model_check(self, mock_get_client, tools):
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo

        # Mock generate response
        gen_response = MagicMock()
        gen_response.generated_prompt = "You are an evaluator..."
        gen_response.generated_code = None
        gen_response.description = "Checks toxicity"
        gen_response.warning = None
        mock_okareo.generate_check.return_value = gen_response

        # Mock the subsequent create_or_update_check call
        mock_okareo.create_or_update_check.return_value = _make_mock_response(
            name="toxicity-check"
        )

        result = json.loads(tools["generate_check"](
            name="toxicity-check",
            description="check if the response is toxic",
            output_type="pass_fail",
            check_type="model",
        ))

        assert result["name"] == "toxicity-check"
        assert result["generated_prompt"] == "You are an evaluator..."
        assert "generated and saved" in result["message"]

    @patch("src.tools.checks.get_okareo_client")
    def test_generate_code_check(self, mock_get_client, tools):
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo

        gen_response = MagicMock()
        gen_response.generated_prompt = None
        gen_response.generated_code = "def evaluate(model_output): return len(model_output.split())"
        gen_response.description = "Counts sentences"
        gen_response.warning = None
        mock_okareo.generate_check.return_value = gen_response

        # For code-based, we go through the low-level API
        with patch(
            "okareo_api_client.api.default.check_create_or_update_v0_check_create_or_update_post.sync"
        ) as mock_api:
            mock_api.return_value = _make_mock_response(name="sentence-count")

            result = json.loads(tools["generate_check"](
                name="sentence-count",
                description="count sentences",
                output_type="score",
                check_type="code",
            ))

            assert result["name"] == "sentence-count"
            assert result["generated_code"] is not None


# ---------------------------------------------------------------------------
# get_check
# ---------------------------------------------------------------------------

class TestGetCheck:
    @patch("src.tools.checks.get_okareo_client")
    def test_get_check_success(self, mock_get_client, tools):
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo

        mock_okareo.get_all_checks.return_value = [
            _make_mock_check_brief("my-check", "uuid-123"),
        ]
        mock_okareo.get_check.return_value = _make_mock_response("uuid-123", "my-check")

        result = json.loads(tools["get_check"](name="my-check"))

        assert result["name"] == "my-check"
        assert result["id"] == "uuid-123"
        assert "check_config" in result

    @patch("src.tools.checks.get_okareo_client")
    def test_get_check_not_found(self, mock_get_client, tools):
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo
        mock_okareo.get_all_checks.return_value = []

        result = json.loads(tools["get_check"](name="nonexistent"))

        assert "error" in result
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# delete_check
# ---------------------------------------------------------------------------

class TestDeleteCheck:
    @patch("src.tools.checks.get_okareo_client")
    def test_delete_check_success(self, mock_get_client, tools):
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo

        mock_okareo.get_all_checks.return_value = [
            _make_mock_check_brief("my-check", "uuid-123", is_predefined=False),
        ]
        mock_okareo.delete_check.return_value = "Check deletion was successful"

        result = json.loads(tools["delete_check"](name="my-check"))

        assert result["deleted"] is True
        assert result["name"] == "my-check"
        mock_okareo.delete_check.assert_called_once_with("uuid-123", "my-check")

    @patch("src.tools.checks.get_okareo_client")
    def test_delete_check_not_found(self, mock_get_client, tools):
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo
        mock_okareo.get_all_checks.return_value = []

        result = json.loads(tools["delete_check"](name="nonexistent"))

        assert "error" in result
        assert "not found" in result["error"]

    @patch("src.tools.checks.get_okareo_client")
    def test_delete_predefined_check_rejected(self, mock_get_client, tools):
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo

        mock_okareo.get_all_checks.return_value = [
            _make_mock_check_brief("coherence", "uuid-456", is_predefined=True),
        ]

        result = json.loads(tools["delete_check"](name="coherence"))

        assert "error" in result
        assert "predefined" in result["error"]
        mock_okareo.delete_check.assert_not_called()


# ---------------------------------------------------------------------------
# US2: check versioning and tags
# ---------------------------------------------------------------------------

def _versioned_brief(name, check_id, version):
    """A check brief carrying a version in additional_properties."""
    brief = _make_mock_check_brief(name, check_id)
    brief.additional_properties = {"version": version}
    return brief


def _register_tests_tools():
    """Register the test-run tools (list_checks lives in src/tools/tests.py)."""
    from mcp.server.fastmcp import FastMCP

    from src.tools.tests import register_tools

    mcp = FastMCP("test")
    register_tools(mcp)
    return {name: tool.fn for name, tool in mcp._tool_manager._tools.items()}


class TestCheckVersioningAndTags:
    @patch("src.tools.checks.get_okareo_client")
    def test_get_check_specific_version(self, mock_get_client, tools):
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo
        mock_okareo.get_all_checks.return_value = [
            _versioned_brief("my-check", "uuid-1", 1),
            _versioned_brief("my-check", "uuid-2", 2),
        ]
        detail = _make_mock_response("uuid-1", "my-check")
        detail.additional_properties = {"version": 1}
        mock_okareo.get_check.return_value = detail

        result = json.loads(tools["get_check"](name="my-check", version=1))

        assert result["version"] == 1
        assert result["available_versions"] == [1, 2]
        # The SDK call must receive the name + version for version selection.
        _, kwargs = mock_okareo.get_check.call_args
        assert kwargs.get("version") == 1

    @patch("src.tools.checks.get_okareo_client")
    def test_get_check_latest_when_no_version(self, mock_get_client, tools):
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo
        mock_okareo.get_all_checks.return_value = [
            _versioned_brief("my-check", "uuid-1", 1),
            _versioned_brief("my-check", "uuid-2", 2),
        ]
        mock_okareo.get_check.return_value = _make_mock_response("uuid-2", "my-check")

        result = json.loads(tools["get_check"](name="my-check"))

        assert result["available_versions"] == [1, 2]
        _, kwargs = mock_okareo.get_check.call_args
        assert kwargs.get("version") is None

    @patch("src.tools.checks.get_okareo_client")
    def test_get_check_unknown_version_lists_available(self, mock_get_client, tools):
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo
        mock_okareo.get_all_checks.return_value = [
            _versioned_brief("my-check", "uuid-1", 1),
            _versioned_brief("my-check", "uuid-2", 2),
        ]
        mock_okareo.get_check.side_effect = ValueError(
            "No check found with name 'my-check' and version 99. "
            "Available versions: [1, 2]"
        )

        result = json.loads(tools["get_check"](name="my-check", version=99))

        assert "error" in result
        assert result["available_versions"] == [1, 2]

    @patch("src.tools.checks.get_okareo_client")
    def test_create_check_with_tags(self, mock_get_client, tools):
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo
        mock_okareo.create_or_update_check.return_value = _make_mock_response()

        result = json.loads(tools["create_or_update_check"](
            name="tagged-check",
            description="A tagged check",
            check_type="model",
            output_type="pass_fail",
            prompt_template="Is this polite? {model_output}",
            tags=["prod", "voice"],
        ))

        assert result["tags"] == ["prod", "voice"]
        _, kwargs = mock_okareo.create_or_update_check.call_args
        assert kwargs.get("tags") == ["prod", "voice"]

    @patch("src.tools.tests.get_okareo_client")
    def test_list_checks_all_versions_annotates_version(self, mock_get_client):
        tests_tools = _register_tests_tools()
        mock_okareo = MagicMock()
        mock_get_client.return_value = mock_okareo
        mock_okareo.get_all_checks.return_value = [
            _versioned_brief("my-check", "uuid-1", 1),
            _versioned_brief("my-check", "uuid-2", 2),
        ]

        result = json.loads(tests_tools["list_checks"](all_versions=True))

        mock_okareo.get_all_checks.assert_called_once_with(all_versions=True)
        versions = sorted(c["version"] for c in result["checks"])
        assert versions == [1, 2]
