"""E4 (spec 032): voice target types are 'twilio' (phone) and 'sip'.

'openai' and 'deepgram' voice targets are hard-removed: creation is rejected
with an error naming the supported types. SIP targets (SDK SipTarget) are
fully supported. Read paths for pre-existing targets of any type keep working.
"""

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
    with _patch("src.tools.simulations.resolve_project", return_value=resolved):
        yield
    _reset_for_tests()


def _register_and_get_tools():
    from mcp.server.fastmcp import FastMCP

    from src.tools.simulations import register_tools

    mcp = FastMCP("test")
    register_tools(mcp)
    return {name: tool.fn for name, tool in mcp._tool_manager._tools.items()}


@pytest.fixture
def tools():
    return _register_and_get_tools()


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("OKAREO_API_KEY", "test-api-key-12345")


def _mock_okareo(mock_client, target_name="t"):
    okareo = MagicMock()
    result = MagicMock()
    result.id = "target-1"
    result.name = target_name
    okareo.create_or_update_target.return_value = result
    mock_client.return_value = okareo
    return okareo


class TestRemovedTypesRejected:
    @pytest.mark.parametrize("removed", ["openai", "deepgram"])
    def test_removed_type_rejected_naming_supported(self, tools, removed):
        result = json.loads(tools["create_or_update_target"](
            name="legacy-voice",
            type="voice",
            edge_type=removed,
        ))
        assert "error" in result
        assert "no longer supported" in result["error"]
        assert "'twilio'" in result["error"]
        assert "'sip'" in result["error"]

    def test_unknown_type_rejected_naming_supported(self, tools):
        result = json.loads(tools["create_or_update_target"](
            name="bad-voice",
            type="voice",
            edge_type="carrier-pigeon",
        ))
        assert "error" in result
        assert "'twilio' or 'sip'" in result["error"]


class TestSipTarget:
    @patch("src.tools.simulations.get_okareo_client")
    def test_sip_target_created_with_sip_uri(self, mock_client, tools):
        from okareo.model_under_test import SipTarget

        okareo = _mock_okareo(mock_client, "sip-agent")
        result = json.loads(tools["create_or_update_target"](
            name="sip-agent",
            type="voice",
            edge_type="sip",
            sip_uri="sip:agent@example.com",
        ))

        assert "error" not in result, result
        target_arg = okareo.create_or_update_target.call_args[0][0]
        assert isinstance(target_arg.target, SipTarget)
        assert target_arg.target.sip_uri == "sip:agent@example.com"

    @patch("src.tools.simulations.get_okareo_client")
    def test_sip_credentials_forwarded(self, mock_client, tools):
        okareo = _mock_okareo(mock_client)
        result = json.loads(tools["create_or_update_target"](
            name="sip-agent-auth",
            type="voice",
            edge_type="sip",
            sip_uri="sip:agent@example.com",
            sip_username="user",
            sip_password="secret",
            max_parallel_requests=2,
        ))

        assert "error" not in result, result
        impl = okareo.create_or_update_target.call_args[0][0].target
        assert impl.sip_username == "user"
        assert impl.sip_password == "secret"
        assert impl.max_parallel_requests == 2

    def test_sip_requires_sip_uri(self, tools):
        result = json.loads(tools["create_or_update_target"](
            name="sip-agent",
            type="voice",
            edge_type="sip",
        ))
        assert "error" in result
        assert "sip_uri" in result["error"]

    @patch("src.tools.simulations.get_okareo_client")
    def test_sip_password_registered_sensitive(self, mock_client, tools):
        okareo = _mock_okareo(mock_client)
        result = json.loads(tools["create_or_update_target"](
            name="sip-agent-auth",
            type="voice",
            edge_type="sip",
            sip_uri="sip:agent@example.com",
            sip_password="secret",
        ))

        assert "error" not in result, result
        kwargs = okareo.create_or_update_target.call_args.kwargs
        assert "sip_password" in kwargs.get("sensitive_fields", [])


class TestTwilioUnchanged:
    @patch("src.tools.simulations.get_okareo_client")
    def test_managed_twilio_still_works(self, mock_client, tools):
        okareo = _mock_okareo(mock_client)
        result = json.loads(tools["create_or_update_target"](
            name="phone-target",
            type="voice",
            edge_type="twilio",
            to_phone_number="+15551234567",
            max_parallel_requests=1,
        ))

        assert "error" not in result, result
        params = okareo.create_or_update_target.call_args[0][0].target
        # Managed Twilio is sent as a plain dict so unsupplied credentials can
        # be OMITTED. Previously the SDK emitted account_sid="" and
        # auth_token="", and the backend rejects that shape — a managed target
        # must simply not carry the keys.
        assert isinstance(params, dict)
        assert params["type"] == "voice"
        assert params["edge_type"] == "twilio"
        assert params["to_phone_number"] == "+15551234567"
        assert "account_sid" not in params
        assert "auth_token" not in params
        assert "from_phone_number" not in params

    @patch("src.tools.simulations.get_okareo_client")
    def test_custom_twilio_credentials_are_still_sent(self, mock_client, tools):
        """Omitting blanks must not drop credentials the caller did supply."""
        okareo = _mock_okareo(mock_client)
        result = json.loads(tools["create_or_update_target"](
            name="phone-target",
            type="voice",
            edge_type="twilio",
            to_phone_number="+15551234567",
            account_sid="AC123",
            auth_token="tok",
            from_phone_number="+15559999999",
            max_parallel_requests=1,
        ))

        assert "error" not in result, result
        params = okareo.create_or_update_target.call_args[0][0].target
        assert params["account_sid"] == "AC123"
        assert params["auth_token"] == "tok"
        assert params["from_phone_number"] == "+15559999999"

    def test_twilio_validation_unchanged(self, tools):
        result = json.loads(tools["create_or_update_target"](
            name="phone-target",
            type="voice",
            edge_type="twilio",
            max_parallel_requests=1,
        ))
        assert "error" in result
        assert "to_phone_number" in result["error"]


class TestReadPathsUntouched:
    @patch("src.tools.simulations.resolve_project")
    @patch("src.tools.simulations.get_okareo_client")
    def test_get_target_returns_legacy_openai_target(
        self, mock_client, mock_project, tools
    ):
        """Pre-existing openai/deepgram targets remain retrievable."""
        mock_client.return_value = MagicMock()
        mock_project.return_value = ResolvedProject(id="proj-1", name="Global", basis="default")
        mut = {
            "id": "legacy-1",
            "name": "old-openai-target",
            "models": {
                "voice": {
                    "edge_type": "openai",
                    "model": "gpt-4o-realtime",
                }
            },
        }

        with patch(
            "okareo_api_client.api.default.get_all_models_under_test_v0_models_under_test_get.sync",
            return_value=[mut],
        ):
            result = json.loads(tools["get_target"](name="old-openai-target"))

        assert "error" not in result, result
        assert json.dumps(result).count("openai") >= 1
