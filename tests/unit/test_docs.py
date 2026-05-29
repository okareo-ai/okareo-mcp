"""Unit tests for documentation and template tools."""

import json
from unittest.mock import patch, MagicMock

import httpx
import pytest

from src.tools.docs import TEMPLATE_NAMES, _load_template


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register_and_get_tools():
    """Register docs tools on a mock MCP and return the tool functions."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")

    from src.tools.docs import register_tools
    register_tools(mcp)

    # Extract registered tool functions from the MCP internal registry
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


# ---------------------------------------------------------------------------
# get_docs tests
# ---------------------------------------------------------------------------

class TestGetDocsConceptual:
    """Test get_docs with conceptual mode."""

    def test_successful_conceptual_query_default_top_k(self, tools):
        """(1) Successful conceptual query with default topK=5."""
        mock_docs = MagicMock()
        mock_docs.status_code = 200
        mock_docs.raise_for_status = MagicMock()
        mock_docs.json.return_value = [
            {"content": "Checks are evaluation functions..."},
            {"content": "There are two types of checks..."},
        ]

        with patch("src.tools.docs.httpx.post", return_value=mock_docs) as mock_post:
            result = json.loads(tools["get_docs"]("How do Checks work?", "conceptual"))

        assert result["mode"] == "conceptual"
        assert result["top_k"] == 5
        assert result["count"] == 2
        assert len(result["results"]) == 2

        # Verify the POST was called with topK=5
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs.kwargs["json"]["topK"] == 5

    def test_successful_user_legible_query_default_top_k(self, tools):
        """(2) Successful user_legible query with default topK=3."""
        mock_docs = MagicMock()
        mock_docs.raise_for_status = MagicMock()
        mock_docs.json.return_value = [{"content": "Doc 1"}]

        with patch("src.tools.docs.httpx.post", return_value=mock_docs) as mock_post:
            result = json.loads(tools["get_docs"]("What is a scenario?", "user_legible"))

        assert result["mode"] == "user_legible"
        assert result["top_k"] == 3
        mock_post.assert_called_once()
        assert mock_post.call_args.kwargs["json"]["topK"] == 3


class TestGetDocsTopK:
    """Test topK resolution and clamping."""

    def test_top_k_clamping_above_10(self, tools):
        """(3) topK clamping above 10."""
        mock_docs = MagicMock()
        mock_docs.raise_for_status = MagicMock()
        mock_docs.json.return_value = []

        with patch("src.tools.docs.httpx.post", return_value=mock_docs) as mock_post:
            result = json.loads(tools["get_docs"]("query", "conceptual", 25))

        assert result["top_k"] == 10
        assert mock_post.call_args.kwargs["json"]["topK"] == 10

    def test_top_k_zero_falls_back_to_mode_default(self, tools):
        """(4) topK ≤0 falls back to mode default."""
        mock_docs = MagicMock()
        mock_docs.raise_for_status = MagicMock()
        mock_docs.json.return_value = []

        with patch("src.tools.docs.httpx.post", return_value=mock_docs):
            result = json.loads(tools["get_docs"]("query", "conceptual", 0))

        assert result["top_k"] == 5

        with patch("src.tools.docs.httpx.post", return_value=mock_docs):
            result = json.loads(tools["get_docs"]("query", "user_legible", -1))

        assert result["top_k"] == 3


class TestGetDocsValidation:
    """T018: Test input validation."""

    def test_empty_query_returns_error(self, tools):
        """(5) Empty query returns error."""
        result = json.loads(tools["get_docs"]("", "conceptual"))
        assert "error" in result
        assert "query is required" in result["error"]

    def test_whitespace_query_returns_error(self, tools):
        """(5b) Whitespace-only query returns error."""
        result = json.loads(tools["get_docs"]("   ", "conceptual"))
        assert "error" in result
        assert "query is required" in result["error"]

    def test_invalid_mode_returns_error(self, tools):
        """(6) Invalid mode returns error."""
        result = json.loads(tools["get_docs"]("query", "invalid_mode"))
        assert "error" in result
        assert "mode must be" in result["error"]


class TestGetDocsAirgap:
    """Test airgap mode gate."""

    def test_airgap_true_returns_immediately(self, tools, monkeypatch):
        """Airgap=true returns error with get_templates suggestion, no HTTP calls."""
        monkeypatch.setenv("AIRGAP", "true")
        with patch("src.tools.docs.httpx.post") as mock_post:
            result = json.loads(tools["get_docs"]("query", "conceptual"))

        assert "error" in result
        assert "airgap mode" in result["error"]
        assert "get_templates" in result["suggestion"]
        mock_post.assert_not_called()

    def test_airgap_1_returns_immediately(self, tools, monkeypatch):
        """AIRGAP=1 also triggers airgap gate."""
        monkeypatch.setenv("AIRGAP", "1")
        result = json.loads(tools["get_docs"]("query", "conceptual"))
        assert "airgap mode" in result["error"]

    def test_airgap_false_allows_docs_call(self, tools, monkeypatch):
        """AIRGAP=false allows normal docs endpoint call."""
        monkeypatch.setenv("AIRGAP", "false")
        mock_docs = MagicMock()
        mock_docs.raise_for_status = MagicMock()
        mock_docs.json.return_value = [{"content": "doc"}]

        with patch("src.tools.docs.httpx.post", return_value=mock_docs) as mock_post:
            result = json.loads(tools["get_docs"]("query", "conceptual"))

        assert result["count"] == 1
        mock_post.assert_called_once()

    def test_airgap_unset_allows_docs_call(self, tools, monkeypatch):
        """AIRGAP unset allows normal docs endpoint call."""
        monkeypatch.delenv("AIRGAP", raising=False)
        mock_docs = MagicMock()
        mock_docs.raise_for_status = MagicMock()
        mock_docs.json.return_value = []

        with patch("src.tools.docs.httpx.post", return_value=mock_docs) as mock_post:
            tools["get_docs"]("query", "conceptual")

        mock_post.assert_called_once()


class TestGetDocsNetworkErrors:
    """Test network error handling (replaces health check tests)."""

    def test_timeout_returns_graceful_error(self, tools):
        """Timeout on docs endpoint returns error with connectivity category."""
        with patch("src.tools.docs.httpx.post", side_effect=httpx.TimeoutException("timed out")):
            result = json.loads(tools["get_docs"]("query", "conceptual"))

        assert "error" in result
        assert result["error"]["category"] == "connectivity"
        assert "timed out" in result["error"]["message"].lower()
        assert "suggestion" in result["error"]

    def test_connection_error_returns_graceful_error(self, tools):
        """Connection error on docs endpoint returns graceful error."""
        with patch("src.tools.docs.httpx.post", side_effect=httpx.ConnectError("connection refused")):
            result = json.loads(tools["get_docs"]("query", "conceptual"))

        assert "error" in result
        assert result["error"]["category"] == "connectivity"
        assert "suggestion" in result["error"]


class TestGetDocsEndpointErrors:
    """Test docs endpoint error handling."""

    def test_docs_endpoint_error_returns_structured_error(self, tools):
        """(9) Docs endpoint error returns structured error."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=mock_response
        )

        with patch("src.tools.docs.httpx.post", return_value=mock_response):
            result = json.loads(tools["get_docs"]("query", "conceptual"))

        assert "error" in result
        assert result["error"]["category"] == "server_error"
        assert "suggestion" in result["error"]

    def test_unexpected_response_format_handled(self, tools):
        """(10) Unexpected response format handled without crash."""
        mock_docs = MagicMock()
        mock_docs.raise_for_status = MagicMock()
        mock_docs.json.side_effect = ValueError("not json")

        with patch("src.tools.docs.httpx.post", return_value=mock_docs):
            result = json.loads(tools["get_docs"]("query", "conceptual"))

        assert "error" in result
        assert "unexpected response format" in result["error"]


# ---------------------------------------------------------------------------
# get_templates tests
# ---------------------------------------------------------------------------

class TestGetTemplatesAll:
    """T019: Test get_templates returning all templates."""

    def test_get_all_templates_returns_all_registered_entries(self, tools):
        """Get all templates returns one entry per registered name with
        descriptions only (no content).
        """
        result = json.loads(tools["get_templates"]())
        assert result["count"] == len(TEMPLATE_NAMES)
        assert len(result["templates"]) == len(TEMPLATE_NAMES)

        for template in result["templates"]:
            assert "template_name" in template
            assert "description" in template
            assert "content" not in template
            assert template["template_name"] in TEMPLATE_NAMES

    def test_get_all_templates_descriptions_are_nonempty(self, tools):
        """Listing descriptions are all non-empty strings."""
        result = json.loads(tools["get_templates"]())
        for template in result["templates"]:
            assert isinstance(template["description"], str)
            assert len(template["description"]) > 0


class TestGetTemplatesSingle:
    """T019: Test get_templates for single template retrieval."""

    def test_get_single_template_by_name(self, tools):
        """(2) Get single template by valid name returns correct content."""
        result = json.loads(tools["get_templates"]("basic_scenario"))
        assert result["template_name"] == "basic_scenario"
        assert "content" in result
        assert len(result["content"]) > 0
        assert "Scenario" in result["content"]

    def test_invalid_template_name_returns_error(self, tools):
        """(3) Invalid template name returns error with available_templates list."""
        result = json.loads(tools["get_templates"]("nonexistent_template"))
        assert "error" in result
        assert "not found" in result["error"]
        assert "available_templates" in result
        assert result["available_templates"] == TEMPLATE_NAMES


class TestGetTemplatesContent:
    """T019: Test template content matches files on disk."""

    @pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
    def test_template_content_matches_file(self, tools, template_name):
        """(4) Template content matches file on disk."""
        # Load directly via the helper
        expected_content = _load_template(template_name)

        # Load via the tool
        result = json.loads(tools["get_templates"](template_name))
        assert result["content"] == expected_content


class TestVoiceAugmentationsTemplate:
    """Spec 023-tool-fixes US7 / T032 — `voice_augmentations` template
    surfaces the augmentation reference (5 strategies + noise, composition
    rule, examples) for copilots.
    """

    def test_voice_augmentations_template_registered(self):
        assert "voice_augmentations" in TEMPLATE_NAMES

    def test_voice_augmentations_template_loads(self, tools):
        result = json.loads(tools["get_templates"]("voice_augmentations"))
        assert "content" in result
        body = result["content"]
        # All five non-noise strategies named
        assert "cap" in body
        assert "directed_speech" in body
        assert "secondary_speaker" in body
        assert "backchannel" in body
        assert "barge_in" in body
        # Noise add-on named
        assert "noise_profile" in body
        # Composition rule documented
        assert "Only noise + one other strategy" in body or "one non-noise" in body
