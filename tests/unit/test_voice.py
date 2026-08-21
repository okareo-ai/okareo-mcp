"""Unit tests for voice tools (US3 ingestion, US6 integrations)."""

import json
from unittest.mock import MagicMock, patch

import pytest
from src.okareo_client import ResolvedProject


@pytest.fixture(autouse=True)
def _default_project_list():
    """Make the real project resolver usable in tests that only mock the client.

    036-project-scoping: tools now resolve a project, so a bare MagicMock
    client would otherwise return a MagicMock from get_projects(). A
    single-project organization is the pre-feature default and keeps these
    tests asserting what they were written to assert.
    """
    from unittest.mock import patch as _patch

    from src.okareo_client import ResolvedProject, _reset_for_tests

    _reset_for_tests()
    resolved = ResolvedProject(
        id="00000000-0000-4000-8000-000000000001", name="Global", basis="default",
    )
    with _patch("src.tools.voice.resolve_project", return_value=resolved):
        yield
    _reset_for_tests()


def _register_and_get_tools():
    """Register voice tools on a mock MCP and return the tool functions."""
    from mcp.server.fastmcp import FastMCP

    from src.tools.voice import register_tools

    mcp = FastMCP("test")
    register_tools(mcp)
    return {name: tool.fn for name, tool in mcp._tool_manager._tools.items()}


@pytest.fixture
def tools():
    return _register_and_get_tools()


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setenv("OKAREO_API_KEY", "test-api-key-12345")


# ---------------------------------------------------------------------------
# US3: ingest_conversations
# ---------------------------------------------------------------------------

class TestIngestConversations:
    @patch("src.tools.voice.resolve_project")
    @patch("src.tools.voice.get_okareo_client")
    def test_transcript_conversation_accepted(
        self, mock_client, mock_resolve, tools
    ):
        okareo = MagicMock()
        okareo.ingest_conversations.return_value = {"datapoints_created": 2}
        mock_client.return_value = okareo
        mock_resolve.return_value = ResolvedProject(id="proj-1", name="Global", basis="default")

        result = json.loads(tools["ingest_conversations"](conversations=[
            {
                "call_id": "c1",
                "transcript": [
                    {"role": "assistant", "content": "Hi"},
                    {"role": "user", "content": "Hello"},
                ],
                "tags": ["support"],
            }
        ]))

        assert result["accepted"] == 1
        assert result["rejected"] == []
        call = okareo.ingest_conversations.call_args
        assert call.kwargs["project_id"] == "proj-1"
        assert call.kwargs["mut_id"] is None

    @patch("src.tools.voice.resolve_project")
    @patch("src.tools.voice.get_okareo_client")
    def test_audio_reference_conversation_accepted(
        self, mock_client, mock_resolve, tools
    ):
        okareo = MagicMock()
        okareo.ingest_conversations.return_value = {"datapoints_created": 1}
        mock_client.return_value = okareo
        mock_resolve.return_value = ResolvedProject(id="proj-1", name="Global", basis="default")

        result = json.loads(tools["ingest_conversations"](
            conversations=[
                {"call_id": "c2", "audio": {"type": "url", "url": "https://x/c2.wav"}}
            ],
            mut_id="mut-9",
        ))

        assert result["accepted"] == 1
        assert okareo.ingest_conversations.call_args.kwargs["mut_id"] == "mut-9"

    @patch("src.tools.voice.resolve_project")
    @patch("src.tools.voice.get_okareo_client")
    def test_partial_batch_rejects_invalid_only(
        self, mock_client, mock_resolve, tools
    ):
        okareo = MagicMock()
        okareo.ingest_conversations.return_value = {"datapoints_created": 1}
        mock_client.return_value = okareo
        mock_resolve.return_value = ResolvedProject(id="proj-1", name="Global", basis="default")

        result = json.loads(tools["ingest_conversations"](conversations=[
            {"call_id": "ok", "transcript": [{"role": "user", "content": "hi"}]},
            {"call_id": "no-audio"},
            {"transcript": [{"role": "user", "content": "x"}]},
        ]))

        assert result["accepted"] == 1
        rejected_indices = {r["index"] for r in result["rejected"]}
        assert rejected_indices == {1, 2}
        # The valid conversation was still sent.
        call = okareo.ingest_conversations.call_args
        assert len(call.kwargs["conversations"]) == 1

    @patch("src.tools.voice.resolve_project")
    @patch("src.tools.voice.get_okareo_client")
    def test_all_invalid_does_not_call_api(self, mock_client, mock_resolve, tools):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = ResolvedProject(id="proj-1", name="Global", basis="default")

        result = json.loads(tools["ingest_conversations"](conversations=[
            {"call_id": "no-audio"},
        ]))

        assert result["accepted"] == 0
        assert len(result["rejected"]) == 1

    def test_empty_conversations_rejected(self, tools):
        result = json.loads(tools["ingest_conversations"](conversations=[]))
        assert "error" in result


# ---------------------------------------------------------------------------
# US6: voice provider integrations
# ---------------------------------------------------------------------------

class TestVoiceIntegrations:
    @patch("src.tools.voice.okareo_api_request")
    @patch("src.tools.voice.resolve_project")
    @patch("src.tools.voice.get_okareo_client")
    def test_connect_integration(self, mock_client, mock_resolve, mock_request, tools):
        mock_client.return_value = MagicMock()
        mock_resolve.return_value = ResolvedProject(id="proj-1", name="Global", basis="default")
        mock_request.return_value = {"id": "int-1", "provider": "retell",
                                     "public_id": "pub-9", "status": "active"}

        result = json.loads(tools["connect_voice_integration"](
            provider="retell",
            webhook_auth_type="hmac",
            secrets={"api_key": "x"},
        ))

        assert result["integration"]["id"] == "int-1"
        call = mock_request.call_args
        assert call[0][1] == "post" and call[0][2] == "/v0/voice/integration"
        assert call[1]["json"]["provider"] == "retell"

    def test_connect_integration_bad_provider(self, tools):
        result = json.loads(tools["connect_voice_integration"](
            provider="nope", webhook_auth_type="hmac", secrets={"k": "v"}
        ))
        assert "error" in result

    @patch("src.tools.voice.okareo_api_request")
    @patch("src.tools.voice.get_okareo_client")
    def test_rotate_secret(self, mock_client, mock_request, tools):
        mock_client.return_value = MagicMock()
        mock_request.return_value = {"id": "int-1", "secret_summary": "***"}

        result = json.loads(tools["rotate_voice_integration_secret"](
            integration_id="int-1", secrets={"api_key": "new"}
        ))

        assert "integration" in result
        call = mock_request.call_args
        assert call[0][2] == "/v0/voice/integration/int-1/rotate"

    @patch("src.tools.voice.okareo_api_request")
    @patch("src.tools.voice.get_okareo_client")
    def test_delete_integration(self, mock_client, mock_request, tools):
        mock_client.return_value = MagicMock()
        mock_request.return_value = None

        result = json.loads(tools["delete_voice_integration"](integration_id="int-1"))
        assert result["deleted"] is True

    @patch("src.tools.voice.get_okareo_client")
    def test_webhook_url_retell_requires_public_id(self, mock_client, tools):
        mock_client.return_value = MagicMock()
        result = json.loads(tools["get_voice_webhook_url"](provider="retell"))
        assert "error" in result

    @patch("src.tools.voice.get_okareo_client")
    def test_webhook_url_vapi(self, mock_client, tools):
        okareo = MagicMock()
        okareo.client.get_httpx_client.return_value.base_url = "https://api.okareo.com/"
        mock_client.return_value = okareo

        result = json.loads(tools["get_voice_webhook_url"](provider="vapi"))
        assert result["webhook_url"] == "https://api.okareo.com/v0/voice/vapi/monitor"
