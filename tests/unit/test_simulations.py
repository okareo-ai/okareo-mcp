"""Unit tests for simulation tools."""

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register_and_get_tools():
    """Register simulation tools on a mock MCP and return the tool functions."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")

    from src.tools.simulations import register_tools
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
# T015: Twilio credential all-or-nothing validation
# ---------------------------------------------------------------------------

class TestTwilioCredentialValidation:
    """Test the all-or-nothing credential triple validation for Twilio targets."""

    def test_partial_credentials_account_sid_only(self, tools):
        """Providing only account_sid without auth_token/from_phone_number is rejected."""
        result = json.loads(tools["create_or_update_target"](
            name="test-twilio",
            type="voice",
            edge_type="twilio",
            to_phone_number="+15551234567",
            max_parallel_requests=1,
            account_sid="ACxxx",
        ))
        assert "error" in result
        assert "Custom Twilio requires account_sid, auth_token, and from_phone_number together" in result["error"]

    def test_partial_credentials_auth_token_only(self, tools):
        """Providing only auth_token without account_sid/from_phone_number is rejected."""
        result = json.loads(tools["create_or_update_target"](
            name="test-twilio",
            type="voice",
            edge_type="twilio",
            to_phone_number="+15551234567",
            max_parallel_requests=1,
            auth_token="tok123",
        ))
        assert "error" in result
        assert "Custom Twilio requires account_sid, auth_token, and from_phone_number together" in result["error"]

    def test_partial_credentials_two_of_three(self, tools):
        """Providing two of three credential fields is rejected."""
        result = json.loads(tools["create_or_update_target"](
            name="test-twilio",
            type="voice",
            edge_type="twilio",
            to_phone_number="+15551234567",
            max_parallel_requests=1,
            account_sid="ACxxx",
            auth_token="tok123",
        ))
        assert "error" in result
        assert "Custom Twilio requires account_sid, auth_token, and from_phone_number together" in result["error"]

    @patch("src.tools.simulations.get_okareo_client")
    def test_all_credentials_provided_passes_validation(self, mock_client, tools):
        """Providing all three credential fields passes validation (custom Twilio)."""
        mock_okareo = MagicMock()
        mock_result = MagicMock()
        mock_result.id = "target-123"
        mock_result.name = "test-twilio"
        mock_okareo.create_or_update_target.return_value = mock_result
        mock_client.return_value = mock_okareo

        result = json.loads(tools["create_or_update_target"](
            name="test-twilio",
            type="voice",
            edge_type="twilio",
            to_phone_number="+15551234567",
            max_parallel_requests=1,
            account_sid="ACxxx",
            auth_token="tok123",
            from_phone_number="+15559876543",
        ))
        assert "error" not in result
        assert result["name"] == "test-twilio"

    @patch("src.tools.simulations.get_okareo_client")
    def test_no_credentials_passes_validation(self, mock_client, tools):
        """Omitting all credential fields passes validation (generic Twilio)."""
        mock_okareo = MagicMock()
        mock_result = MagicMock()
        mock_result.id = "target-456"
        mock_result.name = "test-generic"
        mock_okareo.create_or_update_target.return_value = mock_result
        mock_client.return_value = mock_okareo

        result = json.loads(tools["create_or_update_target"](
            name="test-generic",
            type="voice",
            edge_type="twilio",
            to_phone_number="+15551234567",
            max_parallel_requests=1,
        ))
        assert "error" not in result
        assert result["name"] == "test-generic"


# ---------------------------------------------------------------------------
# T016: max_parallel_requests >= 1 validation
# ---------------------------------------------------------------------------

class TestMaxParallelRequestsValidation:
    """Test max_parallel_requests validation for Twilio targets."""

    def test_max_parallel_requests_none_rejected(self, tools):
        """max_parallel_requests omitted (None) is rejected for Twilio."""
        result = json.loads(tools["create_or_update_target"](
            name="test-twilio",
            type="voice",
            edge_type="twilio",
            to_phone_number="+15551234567",
        ))
        assert "error" in result
        assert "max_parallel_requests" in result["error"]

    def test_max_parallel_requests_zero_rejected(self, tools):
        """max_parallel_requests=0 is rejected for Twilio."""
        result = json.loads(tools["create_or_update_target"](
            name="test-twilio",
            type="voice",
            edge_type="twilio",
            to_phone_number="+15551234567",
            max_parallel_requests=0,
        ))
        assert "error" in result
        assert "max_parallel_requests" in result["error"]

    def test_max_parallel_requests_negative_rejected(self, tools):
        """max_parallel_requests=-1 is rejected for Twilio."""
        result = json.loads(tools["create_or_update_target"](
            name="test-twilio",
            type="voice",
            edge_type="twilio",
            to_phone_number="+15551234567",
            max_parallel_requests=-1,
        ))
        assert "error" in result
        assert "max_parallel_requests" in result["error"]

    @patch("src.tools.simulations.get_okareo_client")
    def test_max_parallel_requests_one_accepted(self, mock_client, tools):
        """max_parallel_requests=1 is accepted for Twilio."""
        mock_okareo = MagicMock()
        mock_result = MagicMock()
        mock_result.id = "target-789"
        mock_result.name = "test-twilio"
        mock_okareo.create_or_update_target.return_value = mock_result
        mock_client.return_value = mock_okareo

        result = json.loads(tools["create_or_update_target"](
            name="test-twilio",
            type="voice",
            edge_type="twilio",
            to_phone_number="+15551234567",
            max_parallel_requests=1,
        ))
        assert "error" not in result


# ---------------------------------------------------------------------------
# T017: sensitive_fields injection for Twilio targets
# ---------------------------------------------------------------------------

class TestSensitiveFieldsInjection:
    """Test that sensitive_fields is passed to create_or_update_target for Twilio."""

    @patch("src.tools.simulations.get_okareo_client")
    def test_generic_twilio_has_sensitive_fields(self, mock_client, tools):
        """Generic Twilio target passes sensitive_fields to create_or_update_target."""
        from src.tools.simulations import TWILIO_SENSITIVE_FIELDS

        mock_okareo = MagicMock()
        mock_result = MagicMock()
        mock_result.id = "target-gen"
        mock_result.name = "test-generic"
        mock_okareo.create_or_update_target.return_value = mock_result
        mock_client.return_value = mock_okareo

        tools["create_or_update_target"](
            name="test-generic",
            type="voice",
            edge_type="twilio",
            to_phone_number="+15551234567",
            max_parallel_requests=1,
        )

        mock_okareo.create_or_update_target.assert_called_once()
        call_kwargs = mock_okareo.create_or_update_target.call_args
        assert call_kwargs.kwargs.get("sensitive_fields") == TWILIO_SENSITIVE_FIELDS

    @patch("src.tools.simulations.get_okareo_client")
    def test_custom_twilio_has_sensitive_fields(self, mock_client, tools):
        """Custom Twilio target passes sensitive_fields to create_or_update_target."""
        from src.tools.simulations import TWILIO_SENSITIVE_FIELDS

        mock_okareo = MagicMock()
        mock_result = MagicMock()
        mock_result.id = "target-cust"
        mock_result.name = "test-custom"
        mock_okareo.create_or_update_target.return_value = mock_result
        mock_client.return_value = mock_okareo

        tools["create_or_update_target"](
            name="test-custom",
            type="voice",
            edge_type="twilio",
            to_phone_number="+15551234567",
            max_parallel_requests=2,
            account_sid="ACxxx",
            auth_token="tok123",
            from_phone_number="+15559876543",
        )

        mock_okareo.create_or_update_target.assert_called_once()
        call_kwargs = mock_okareo.create_or_update_target.call_args
        assert call_kwargs.kwargs.get("sensitive_fields") == TWILIO_SENSITIVE_FIELDS

    @patch("src.tools.simulations.get_okareo_client")
    def test_generation_target_no_sensitive_fields(self, mock_client, tools):
        """Generation target does NOT pass sensitive_fields."""
        mock_okareo = MagicMock()
        mock_result = MagicMock()
        mock_result.id = "target-gen"
        mock_result.name = "test-gen"
        mock_okareo.create_or_update_target.return_value = mock_result
        mock_client.return_value = mock_okareo

        tools["create_or_update_target"](
            name="test-gen",
            type="generation",
            model_id="gpt-4o-mini",
        )

        mock_okareo.create_or_update_target.assert_called_once()
        call_kwargs = mock_okareo.create_or_update_target.call_args
        assert "sensitive_fields" not in (call_kwargs.kwargs or {})

    def test_sensitive_fields_constant_has_9_entries(self):
        """TWILIO_SENSITIVE_FIELDS has exactly 9 entries per FR-018."""
        from src.tools.simulations import TWILIO_SENSITIVE_FIELDS

        assert len(TWILIO_SENSITIVE_FIELDS) == 9
        assert "apikey" in TWILIO_SENSITIVE_FIELDS
        assert "authorization" in TWILIO_SENSITIVE_FIELDS
        assert "token" in TWILIO_SENSITIVE_FIELDS
        assert "accesstoken" in TWILIO_SENSITIVE_FIELDS
        assert "refreshtoken" in TWILIO_SENSITIVE_FIELDS


# ---------------------------------------------------------------------------
# T018: get_target rewrite using models_under_test endpoint
# ---------------------------------------------------------------------------

class TestGetTargetRewrite:
    """Test get_target uses the models_under_test endpoint for all target types."""

    @patch("src.tools.simulations.resolve_project")
    @patch("src.tools.simulations.get_okareo_client")
    def test_get_target_found_dict(self, mock_client, mock_project, tools):
        """get_target returns target config when found (dict response)."""
        mock_okareo = MagicMock()
        mock_client.return_value = mock_okareo
        mock_project.return_value = ResolvedProject(id="proj-123", name="Global", basis="default")

        mock_mut = {
            "id": "mut-abc",
            "name": "my-target",
            "models": {"generation": {"model_id": "gpt-4o-mini"}},
        }

        with patch(
            "okareo_api_client.api.default.get_all_models_under_test_v0_models_under_test_get.sync",
            return_value=[mock_mut],
        ):
            result = json.loads(tools["get_target"](name="my-target"))

        assert "error" not in result
        assert result["target_id"] == "mut-abc"
        assert result["name"] == "my-target"
        assert "target" in result

    @patch("src.tools.simulations.resolve_project")
    @patch("src.tools.simulations.get_okareo_client")
    def test_get_target_not_found(self, mock_client, mock_project, tools):
        """get_target returns error when target not found."""
        mock_okareo = MagicMock()
        mock_client.return_value = mock_okareo
        mock_project.return_value = ResolvedProject(id="proj-123", name="Global", basis="default")

        with patch(
            "okareo_api_client.api.default.get_all_models_under_test_v0_models_under_test_get.sync",
            return_value=[
                {"id": "mut-other", "name": "other-target", "models": {}},
            ],
        ):
            result = json.loads(tools["get_target"](name="nonexistent"))

        assert "error" in result
        assert "not found" in result["error"]

    @patch("src.tools.simulations.resolve_project")
    @patch("src.tools.simulations.get_okareo_client")
    def test_get_target_empty_list(self, mock_client, mock_project, tools):
        """get_target returns error when no targets exist."""
        mock_okareo = MagicMock()
        mock_client.return_value = mock_okareo
        mock_project.return_value = ResolvedProject(id="proj-123", name="Global", basis="default")

        with patch(
            "okareo_api_client.api.default.get_all_models_under_test_v0_models_under_test_get.sync",
            return_value=[],
        ):
            result = json.loads(tools["get_target"](name="any-target"))

        assert "error" in result
        assert "not found" in result["error"]

    @patch("src.tools.simulations.resolve_project")
    @patch("src.tools.simulations.get_okareo_client")
    def test_get_target_voice_target_works(self, mock_client, mock_project, tools):
        """get_target works for voice targets (not just generation)."""
        mock_okareo = MagicMock()
        mock_client.return_value = mock_okareo
        mock_project.return_value = ResolvedProject(id="proj-123", name="Global", basis="default")

        mock_mut = {
            "id": "mut-voice",
            "name": "voice-target",
            "models": {"voice": {"edge_type": "twilio", "to_phone_number": "+15551234567"}},
        }

        with patch(
            "okareo_api_client.api.default.get_all_models_under_test_v0_models_under_test_get.sync",
            return_value=[mock_mut],
        ):
            result = json.loads(tools["get_target"](name="voice-target"))

        assert "error" not in result
        assert result["target_id"] == "mut-voice"
        assert result["name"] == "voice-target"


# ---------------------------------------------------------------------------
# T008: list_targets — filtering targets from generation models
# ---------------------------------------------------------------------------

class TestListTargets:
    """Test list_targets filters to voice and custom_endpoint targets only."""

    @patch("src.tools.simulations.resolve_project")
    @patch("src.tools.simulations.get_okareo_client")
    def test_mixed_entries_filters_correctly(self, mock_client, mock_project, tools):
        """Only voice and custom_endpoint entries are returned, not generation."""
        mock_okareo = MagicMock()
        mock_client.return_value = mock_okareo
        mock_project.return_value = ResolvedProject(id="proj-123", name="Global", basis="default")

        muts = [
            {"id": "mut-gen", "name": "gen-model", "models": {"generation": {"model_id": "gpt-4o-mini"}}, "time_created": "2026-02-20T00:00:00"},
            {"id": "mut-voice", "name": "voice-target", "models": {"voice": {"edge_type": "twilio"}}, "time_created": "2026-02-20T01:00:00"},
            {"id": "mut-ce", "name": "api-bot", "models": {"custom_endpoint": {"url": "https://example.com"}}, "time_created": "2026-02-20T02:00:00"},
        ]

        with patch(
            "okareo_api_client.api.default.get_all_models_under_test_v0_models_under_test_get.sync",
            return_value=muts,
        ):
            result = json.loads(tools["list_targets"]())

        assert "error" not in result
        assert result["count"] == 2
        names = {t["name"] for t in result["targets"]}
        assert names == {"voice-target", "api-bot"}
        # Verify generation model excluded
        assert "gen-model" not in names

    @patch("src.tools.simulations.resolve_project")
    @patch("src.tools.simulations.get_okareo_client")
    def test_empty_list_returns_message(self, mock_client, mock_project, tools):
        """Empty MUT list returns empty targets with message."""
        mock_okareo = MagicMock()
        mock_client.return_value = mock_okareo
        mock_project.return_value = ResolvedProject(id="proj-123", name="Global", basis="default")

        with patch(
            "okareo_api_client.api.default.get_all_models_under_test_v0_models_under_test_get.sync",
            return_value=[],
        ):
            result = json.loads(tools["list_targets"]())

        assert result["count"] == 0
        assert result["targets"] == []
        assert "message" in result

    @patch("src.tools.simulations.resolve_project")
    @patch("src.tools.simulations.get_okareo_client")
    def test_only_generation_models_returns_empty(self, mock_client, mock_project, tools):
        """When only generation models exist, list_targets returns empty."""
        mock_okareo = MagicMock()
        mock_client.return_value = mock_okareo
        mock_project.return_value = ResolvedProject(id="proj-123", name="Global", basis="default")

        muts = [
            {"id": "mut-1", "name": "model-a", "models": {"generation": {"model_id": "gpt-4o"}}, "time_created": ""},
            {"id": "mut-2", "name": "model-b", "models": {"generation": {"model_id": "gpt-4o-mini"}}, "time_created": ""},
        ]

        with patch(
            "okareo_api_client.api.default.get_all_models_under_test_v0_models_under_test_get.sync",
            return_value=muts,
        ):
            result = json.loads(tools["list_targets"]())

        assert result["count"] == 0
        assert result["targets"] == []
        assert "message" in result

    @patch("src.tools.simulations.resolve_project")
    @patch("src.tools.simulations.get_okareo_client")
    def test_voice_target_extracts_fields(self, mock_client, mock_project, tools):
        """Voice target entry has correct target_id, name, type, time_created."""
        mock_okareo = MagicMock()
        mock_client.return_value = mock_okareo
        mock_project.return_value = ResolvedProject(id="proj-123", name="Global", basis="default")

        muts = [
            {"id": "mut-v1", "name": "my-phone-agent", "models": {"voice": {"edge_type": "twilio", "to_phone_number": "+15551234567"}}, "time_created": "2026-02-20T10:00:00"},
        ]

        with patch(
            "okareo_api_client.api.default.get_all_models_under_test_v0_models_under_test_get.sync",
            return_value=muts,
        ):
            result = json.loads(tools["list_targets"]())

        assert result["count"] == 1
        target = result["targets"][0]
        assert target["target_id"] == "mut-v1"
        assert target["name"] == "my-phone-agent"
        assert target["type"] == "voice"
        assert "2026-02-20" in target["time_created"]


# ---------------------------------------------------------------------------
# T004: delete_target — mirroring delete_generation_model
# ---------------------------------------------------------------------------

class TestDeleteTarget:
    """Test delete_target mirrors delete_generation_model with target-oriented naming."""

    @patch("src.tools.simulations.resolve_artifact_by_name")
    @patch("src.tools.simulations.get_okareo_client")
    def test_delete_target_success(self, mock_client, mock_resolve_artifact, tools):
        """delete_target returns confirmation when target is found and deleted."""
        mock_okareo = MagicMock()
        # Resolved inside the acting project (036 rev 2) rather than through
        # okareo.get_model, whose lookup carries no project.
        mock_mut = MagicMock()
        mock_mut.id = "mut-abc-123"
        mock_resolve_artifact.return_value = mock_mut
        mock_client.return_value = mock_okareo

        with patch(
            "okareo_api_client.api.default.delete_model_under_test_v0_models_under_test_mut_id_delete.sync",
        ) as mock_delete:
            result = json.loads(tools["delete_target"](name="my-target"))

        assert result["deleted"] is True
        assert result["name"] == "my-target"
        assert "Deleted target" in result["message"]
        mock_delete.assert_called_once()

    @patch("src.tools.simulations.get_okareo_client")
    def test_delete_target_not_found(self, mock_client, tools):
        """delete_target returns error referencing list_targets when target not found."""
        mock_okareo = MagicMock()
        mock_okareo.get_model.side_effect = Exception("not found")
        mock_client.return_value = mock_okareo

        result = json.loads(tools["delete_target"](name="nonexistent"))

        assert "error" in result
        assert "not found" in result["error"]
        assert "list_targets" in result["error"]


# ---------------------------------------------------------------------------
# T009: _build_custom_endpoint_sensitive_fields helper
# ---------------------------------------------------------------------------

class TestBuildCustomEndpointSensitiveFields:
    """Test the sensitive_fields auto-generation helper for custom_endpoint auth."""

    def test_auto_generates_top_level_keys(self):
        """Auto-generates auth_params.<key> for every top-level key."""
        from src.tools.simulations import _build_custom_endpoint_sensitive_fields

        result = _build_custom_endpoint_sensitive_fields(
            {"url": "https://example.com", "method": "POST", "body": "{}"},
        )
        assert "auth_params.url" in result
        assert "auth_params.method" in result
        assert "auth_params.body" in result

    def test_merges_with_caller_supplied_deep_paths(self):
        """Merges auto-generated paths with caller-supplied deep dot-paths."""
        from src.tools.simulations import _build_custom_endpoint_sensitive_fields

        result = _build_custom_endpoint_sensitive_fields(
            {"url": "https://example.com", "body": "{}"},
            caller_sensitive=["auth_params.body.client_id"],
        )
        assert "auth_params.url" in result
        assert "auth_params.body" in result
        assert "auth_params.body.client_id" in result

    def test_deduplicates_overlapping_entries(self):
        """No duplicates when caller supplies a path that matches auto-generated."""
        from src.tools.simulations import _build_custom_endpoint_sensitive_fields

        result = _build_custom_endpoint_sensitive_fields(
            {"url": "https://example.com"},
            caller_sensitive=["auth_params.url"],
        )
        assert result.count("auth_params.url") == 1

    def test_empty_auth_params(self):
        """Empty auth_params dict yields empty list (or just caller-supplied)."""
        from src.tools.simulations import _build_custom_endpoint_sensitive_fields

        result = _build_custom_endpoint_sensitive_fields({})
        assert result == []

    def test_empty_auth_params_with_caller_sensitive(self):
        """Empty auth_params with caller-supplied paths returns only caller paths."""
        from src.tools.simulations import _build_custom_endpoint_sensitive_fields

        result = _build_custom_endpoint_sensitive_fields(
            {}, caller_sensitive=["auth_params.body.secret"]
        )
        assert result == ["auth_params.body.secret"]

    def test_sorted_output(self):
        """Output is sorted for determinism."""
        from src.tools.simulations import _build_custom_endpoint_sensitive_fields

        result = _build_custom_endpoint_sensitive_fields(
            {"z_key": "val", "a_key": "val"},
        )
        assert result == sorted(result)


# ---------------------------------------------------------------------------
# T010: create_or_update_target auth_params validation
# ---------------------------------------------------------------------------

class TestCreateTargetAuthParams:
    """Test auth_params validation and integration for custom_endpoint targets."""

    def test_auth_params_missing_url(self, tools):
        """auth_params without 'url' is rejected."""
        result = json.loads(tools["create_or_update_target"](
            name="test-ce",
            type="custom_endpoint",
            next_message_params={"url": "https://example.com/chat", "method": "POST"},
            auth_params={"method": "POST", "response_access_token_path": "token"},
        ))
        assert "error" in result
        assert "url" in str(result["error"])

    def test_auth_params_missing_method(self, tools):
        """auth_params without 'method' is rejected."""
        result = json.loads(tools["create_or_update_target"](
            name="test-ce",
            type="custom_endpoint",
            next_message_params={"url": "https://example.com/chat", "method": "POST"},
            auth_params={"url": "https://auth.example.com/token", "response_access_token_path": "token"},
        ))
        assert "error" in result
        assert "method" in str(result["error"])

    def test_auth_params_missing_response_access_token_path(self, tools):
        """auth_params without 'response_access_token_path' is rejected."""
        result = json.loads(tools["create_or_update_target"](
            name="test-ce",
            type="custom_endpoint",
            next_message_params={"url": "https://example.com/chat", "method": "POST"},
            auth_params={"url": "https://auth.example.com/token", "method": "POST"},
        ))
        assert "error" in result
        assert "response_access_token_path" in str(result["error"])

    def test_auth_params_not_a_dict(self, tools):
        """auth_params that is not a dict is rejected."""
        result = json.loads(tools["create_or_update_target"](
            name="test-ce",
            type="custom_endpoint",
            next_message_params={"url": "https://example.com/chat", "method": "POST"},
            auth_params="not-a-dict",
        ))
        assert "error" in result
        assert "JSON object" in result["error"]

    @patch("src.tools.simulations.get_okareo_client")
    def test_auth_params_valid_creates_target(self, mock_client, tools):
        """Valid auth_params creates target and passes sensitive_fields."""
        mock_okareo = MagicMock()
        mock_result = MagicMock()
        mock_result.id = "target-auth"
        mock_result.name = "test-auth-ce"
        mock_okareo.create_or_update_target.return_value = mock_result
        mock_client.return_value = mock_okareo

        result = json.loads(tools["create_or_update_target"](
            name="test-auth-ce",
            type="custom_endpoint",
            next_message_params={"url": "https://example.com/chat", "method": "POST"},
            auth_params={
                "url": "https://auth.example.com/token",
                "method": "POST",
                "response_access_token_path": "access_token",
            },
        ))
        assert "error" not in result
        assert result["name"] == "test-auth-ce"
        assert result["has_auth"] is True
        assert result["sensitive_fields_count"] == 3  # url, method, response_access_token_path

        # Verify sensitive_fields were passed to SDK
        call_kwargs = mock_okareo.create_or_update_target.call_args
        sf = call_kwargs.kwargs.get("sensitive_fields")
        assert sf is not None
        assert "auth_params.url" in sf
        assert "auth_params.method" in sf
        assert "auth_params.response_access_token_path" in sf

    @patch("src.tools.simulations.get_okareo_client")
    def test_auth_params_with_caller_sensitive_fields(self, mock_client, tools):
        """auth_params with caller-supplied sensitive_fields merges correctly."""
        mock_okareo = MagicMock()
        mock_result = MagicMock()
        mock_result.id = "target-auth2"
        mock_result.name = "test-auth-sf"
        mock_okareo.create_or_update_target.return_value = mock_result
        mock_client.return_value = mock_okareo

        result = json.loads(tools["create_or_update_target"](
            name="test-auth-sf",
            type="custom_endpoint",
            next_message_params={"url": "https://example.com/chat", "method": "POST"},
            auth_params={
                "url": "https://auth.example.com/token",
                "method": "POST",
                "body": '{"grant_type":"client_credentials"}',
                "response_access_token_path": "access_token",
            },
            sensitive_fields=["auth_params.body.client_id", "auth_params.body.client_secret"],
        ))
        assert "error" not in result
        assert result["has_auth"] is True
        # 4 auto-generated (url, method, body, response_access_token_path) + 2 caller = 6
        assert result["sensitive_fields_count"] == 6

        call_kwargs = mock_okareo.create_or_update_target.call_args
        sf = call_kwargs.kwargs.get("sensitive_fields")
        assert "auth_params.body.client_id" in sf
        assert "auth_params.body.client_secret" in sf
        assert "auth_params.url" in sf

    @patch("src.tools.simulations.get_okareo_client")
    def test_custom_endpoint_without_auth_no_sensitive_fields(self, mock_client, tools):
        """Custom endpoint without auth_params does NOT pass sensitive_fields."""
        mock_okareo = MagicMock()
        mock_result = MagicMock()
        mock_result.id = "target-noauth"
        mock_result.name = "test-noauth"
        mock_okareo.create_or_update_target.return_value = mock_result
        mock_client.return_value = mock_okareo

        result = json.loads(tools["create_or_update_target"](
            name="test-noauth",
            type="custom_endpoint",
            next_message_params={"url": "https://example.com/chat", "method": "POST"},
        ))
        assert "error" not in result
        assert "has_auth" not in result

        call_kwargs = mock_okareo.create_or_update_target.call_args
        assert "sensitive_fields" not in (call_kwargs.kwargs or {})


# ---------------------------------------------------------------------------
# T008: Streaming config on custom_endpoint targets
# ---------------------------------------------------------------------------

class TestStreamingConfig:
    """Test SSE streaming configuration for custom_endpoint targets."""

    @patch("src.tools.simulations.get_okareo_client")
    def test_streaming_config_on_next_message_params(self, mock_client, tools):
        """Streaming config in next_message_params builds TurnConfig with StreamingConfig."""
        mock_okareo = MagicMock()
        mock_result = MagicMock()
        mock_result.id = "target-stream"
        mock_result.name = "streaming-target"
        mock_okareo.create_or_update_target.return_value = mock_result
        mock_client.return_value = mock_okareo

        result = json.loads(tools["create_or_update_target"](
            name="streaming-target",
            type="custom_endpoint",
            next_message_params={
                "url": "https://api.example.com/chat",
                "method": "POST",
                "response_message_path": "response.choices[0].delta.content",
                "streaming": {
                    "stop": [{"value": "[DONE]"}, {"value": "true", "path": "response.is_final"}],
                    "select": [{"path": "response.role", "value": "assistant"}],
                },
            },
        ))
        assert "error" not in result
        assert result["name"] == "streaming-target"

        call_args = mock_okareo.create_or_update_target.call_args
        target = call_args.args[0]
        target_impl = target.target
        assert target_impl.next_turn.streaming is not None
        streaming = target_impl.next_turn.streaming
        assert len(streaming.stop) == 2
        assert streaming.stop[0].value == "[DONE]"
        assert streaming.stop[0].path is None
        assert streaming.stop[1].value == "true"
        assert streaming.stop[1].path == "response.is_final"
        assert len(streaming.select) == 1
        assert streaming.select[0].path == "response.role"
        assert streaming.select[0].value == "assistant"

    @patch("src.tools.simulations.get_okareo_client")
    def test_streaming_config_on_start_session_params(self, mock_client, tools):
        """Streaming config in start_session_params builds SessionConfig with StreamingConfig."""
        mock_okareo = MagicMock()
        mock_result = MagicMock()
        mock_result.id = "target-ssp-stream"
        mock_result.name = "ssp-streaming"
        mock_okareo.create_or_update_target.return_value = mock_result
        mock_client.return_value = mock_okareo

        result = json.loads(tools["create_or_update_target"](
            name="ssp-streaming",
            type="custom_endpoint",
            next_message_params={"url": "https://api.example.com/chat", "method": "POST"},
            start_session_params={
                "url": "https://api.example.com/session",
                "streaming": {
                    "stop": [{"value": "[DONE]"}],
                },
            },
        ))
        assert "error" not in result

        call_args = mock_okareo.create_or_update_target.call_args
        target = call_args.args[0]
        target_impl = target.target
        assert target_impl.start_session is not None
        assert target_impl.start_session.streaming is not None
        assert len(target_impl.start_session.streaming.stop) == 1

    @patch("src.tools.simulations.get_okareo_client")
    def test_no_streaming_config_backward_compat(self, mock_client, tools):
        """Custom endpoint without streaming still works (backward compat)."""
        mock_okareo = MagicMock()
        mock_result = MagicMock()
        mock_result.id = "target-no-stream"
        mock_result.name = "no-stream"
        mock_okareo.create_or_update_target.return_value = mock_result
        mock_client.return_value = mock_okareo

        result = json.loads(tools["create_or_update_target"](
            name="no-stream",
            type="custom_endpoint",
            next_message_params={"url": "https://api.example.com/chat", "method": "POST"},
        ))
        assert "error" not in result

        call_args = mock_okareo.create_or_update_target.call_args
        target = call_args.args[0]
        target_impl = target.target
        assert target_impl.next_turn.streaming is None


# ---------------------------------------------------------------------------
# T009: Streaming config rejected on non-custom_endpoint types
# ---------------------------------------------------------------------------

class TestStreamingValidation:
    """Test streaming config is rejected on non-custom_endpoint targets."""

    def test_streaming_on_generation_rejected(self, tools):
        """Streaming config on generation target type returns error."""
        result = json.loads(tools["create_or_update_target"](
            name="gen-with-streaming",
            type="generation",
            model_id="gpt-4o-mini",
            next_message_params={
                "url": "https://example.com",
                "method": "POST",
                "streaming": {"stop": [{"value": "[DONE]"}]},
            },
        ))
        assert "error" in result
        assert "streaming" in result["error"].lower()
        assert "custom_endpoint" in result["error"]

    def test_streaming_on_voice_rejected(self, tools):
        """Streaming config on voice target type returns error."""
        result = json.loads(tools["create_or_update_target"](
            name="voice-with-streaming",
            type="voice",
            edge_type="twilio",
            to_phone_number="+15551234567",
            max_parallel_requests=1,
            next_message_params={
                "url": "https://example.com",
                "method": "POST",
                "streaming": {"stop": [{"value": "[DONE]"}]},
            },
        ))
        assert "error" in result
        assert "streaming" in result["error"].lower()


# ---------------------------------------------------------------------------
# T013: Auth with native AuthConfig
# ---------------------------------------------------------------------------

class TestNativeAuthConfig:
    """Test auth_params creates CustomEndpointTarget with native AuthConfig."""

    @patch("src.tools.simulations.get_okareo_client")
    def test_auth_creates_custom_endpoint_with_auth_config(self, mock_client, tools):
        """auth_params results in CustomEndpointTarget with auth attribute (not raw dict)."""
        from okareo.model_under_test import CustomEndpointTarget

        mock_okareo = MagicMock()
        mock_result = MagicMock()
        mock_result.id = "target-native-auth"
        mock_result.name = "native-auth"
        mock_okareo.create_or_update_target.return_value = mock_result
        mock_client.return_value = mock_okareo

        result = json.loads(tools["create_or_update_target"](
            name="native-auth",
            type="custom_endpoint",
            next_message_params={"url": "https://api.example.com/chat", "method": "POST"},
            auth_params={
                "url": "https://auth.example.com/token",
                "method": "POST",
                "response_access_token_path": "response.access_token",
                "body": {"grant_type": "client_credentials"},
            },
        ))
        assert "error" not in result
        assert result["has_auth"] is True

        call_args = mock_okareo.create_or_update_target.call_args
        target = call_args.args[0]
        target_impl = target.target
        assert isinstance(target_impl, CustomEndpointTarget)
        assert target_impl.auth is not None
        assert target_impl.auth.url == "https://auth.example.com/token"
        assert target_impl.auth.method == "POST"
        assert target_impl.auth.response_access_token_path == "response.access_token"


# ---------------------------------------------------------------------------
# T014: Auth + streaming combined
# ---------------------------------------------------------------------------

class TestAuthPlusStreaming:
    """Test auth_params and streaming compose correctly."""

    @patch("src.tools.simulations.get_okareo_client")
    def test_auth_and_streaming_combined(self, mock_client, tools):
        """Both AuthConfig and StreamingConfig present on the same target."""
        mock_okareo = MagicMock()
        mock_result = MagicMock()
        mock_result.id = "target-combo"
        mock_result.name = "combo-target"
        mock_okareo.create_or_update_target.return_value = mock_result
        mock_client.return_value = mock_okareo

        result = json.loads(tools["create_or_update_target"](
            name="combo-target",
            type="custom_endpoint",
            next_message_params={
                "url": "https://api.example.com/chat",
                "method": "POST",
                "response_message_path": "response.delta.text",
                "streaming": {"stop": [{"value": "[DONE]"}]},
            },
            auth_params={
                "url": "https://auth.example.com/token",
                "method": "POST",
                "response_access_token_path": "response.access_token",
            },
        ))
        assert "error" not in result

        call_args = mock_okareo.create_or_update_target.call_args
        target = call_args.args[0]
        target_impl = target.target
        assert target_impl.auth is not None
        assert target_impl.next_turn.streaming is not None
        assert target_impl.next_turn.streaming.stop[0].value == "[DONE]"


# ---------------------------------------------------------------------------
# T016-T017: Optional session (no fallback SessionConfig)
# ---------------------------------------------------------------------------

class TestOptionalSession:
    """Test start_session_params is truly optional."""

    @patch("src.tools.simulations.get_okareo_client")
    def test_no_start_session_params_creates_none(self, mock_client, tools):
        """Omitting start_session_params results in start_session=None."""
        mock_okareo = MagicMock()
        mock_result = MagicMock()
        mock_result.id = "target-nosess"
        mock_result.name = "no-session"
        mock_okareo.create_or_update_target.return_value = mock_result
        mock_client.return_value = mock_okareo

        result = json.loads(tools["create_or_update_target"](
            name="no-session",
            type="custom_endpoint",
            next_message_params={"url": "https://api.example.com/chat", "method": "POST"},
        ))
        assert "error" not in result

        call_args = mock_okareo.create_or_update_target.call_args
        target = call_args.args[0]
        target_impl = target.target
        assert target_impl.start_session is None

    @patch("src.tools.simulations.get_okareo_client")
    def test_explicit_start_session_params_creates_session_config(self, mock_client, tools):
        """Providing start_session_params creates a proper SessionConfig."""
        mock_okareo = MagicMock()
        mock_result = MagicMock()
        mock_result.id = "target-withsess"
        mock_result.name = "with-session"
        mock_okareo.create_or_update_target.return_value = mock_result
        mock_client.return_value = mock_okareo

        result = json.loads(tools["create_or_update_target"](
            name="with-session",
            type="custom_endpoint",
            next_message_params={"url": "https://api.example.com/chat", "method": "POST"},
            start_session_params={
                "url": "https://api.example.com/session",
                "method": "POST",
                "response_session_id_path": "response.id",
            },
        ))
        assert "error" not in result

        call_args = mock_okareo.create_or_update_target.call_args
        target = call_args.args[0]
        target_impl = target.target
        assert target_impl.start_session is not None
        assert target_impl.start_session.url == "https://api.example.com/session"
        assert target_impl.start_session.response_session_id_path == "response.id"


# ---------------------------------------------------------------------------
# T019: response_session_id_path on TurnConfig
# ---------------------------------------------------------------------------

class TestResponseSessionIdPath:
    """Test response_session_id_path passthrough on TurnConfig."""

    @patch("src.tools.simulations.get_okareo_client")
    def test_response_session_id_path_on_turn_config(self, mock_client, tools):
        """response_session_id_path in next_message_params is passed to TurnConfig."""
        mock_okareo = MagicMock()
        mock_result = MagicMock()
        mock_result.id = "target-sidpath"
        mock_result.name = "sid-path"
        mock_okareo.create_or_update_target.return_value = mock_result
        mock_client.return_value = mock_okareo

        result = json.loads(tools["create_or_update_target"](
            name="sid-path",
            type="custom_endpoint",
            next_message_params={
                "url": "https://api.example.com/chat",
                "method": "POST",
                "response_session_id_path": "response.result.contextId",
            },
        ))
        assert "error" not in result

        call_args = mock_okareo.create_or_update_target.call_args
        target = call_args.args[0]
        target_impl = target.target
        assert target_impl.next_turn.response_session_id_path == "response.result.contextId"


# ---------------------------------------------------------------------------
# US7: voice-configured drivers and the voice catalog
# ---------------------------------------------------------------------------

class TestVoiceDrivers:
    @patch("src.tools.simulations.okareo_api_request")
    @patch("src.tools.simulations.get_okareo_client")
    def test_create_driver_with_language(self, mock_client, mock_request, tools):
        mock_client.return_value = MagicMock()
        # First call: catalog fetch (voices), second: catalog (profiles),
        # third: POST /v0/driver.
        mock_request.side_effect = [
            [{"id": "nova", "language": "es-ES"}],
            [{"profile_name": "calm"}],
            {"id": "drv-1", "name": "es-caller", "voice": "nova",
             "language": "es-ES"},
        ]

        result = json.loads(tools["create_or_update_driver"](
            name="es-caller",
            prompt_template="You are a caller. {scenario_input}",
            voice="nova",
            voice_profile="calm",
            language="es-ES",
        ))

        assert result["created"] is True
        assert result["language"] == "es-ES"
        # The POST body carried language.
        post_call = mock_request.call_args_list[-1]
        assert post_call[0][1] == "post"
        assert post_call[0][2] == "/v0/driver"
        assert post_call[1]["json"]["language"] == "es-ES"

    @patch("src.tools.simulations.okareo_api_request")
    @patch("src.tools.simulations.get_okareo_client")
    def test_create_driver_rejects_unknown_voice(
        self, mock_client, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_request.side_effect = [
            [{"id": "nova"}, {"id": "alloy"}],   # /v0/driver_voices
            [{"profile_name": "calm"}],          # /v0/driver_profiles
        ]

        result = json.loads(tools["create_or_update_driver"](
            name="bad",
            prompt_template="You are a caller. {scenario_input}",
            voice="not-a-voice",
        ))

        assert "error" in result
        assert result["available_voices"] == ["alloy", "nova"]

    @patch("src.tools.simulations.okareo_api_request")
    @patch("src.tools.simulations.get_okareo_client")
    def test_create_driver_no_voice_skips_catalog(
        self, mock_client, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_request.return_value = {"id": "drv-2", "name": "plain"}

        result = json.loads(tools["create_or_update_driver"](
            name="plain",
            prompt_template="You are a caller. {scenario_input}",
        ))

        assert result["created"] is True
        # No voice/profile → no catalog fetch, only the POST.
        assert mock_request.call_count == 1

    @patch("src.tools.simulations.okareo_api_request")
    @patch("src.tools.simulations.get_okareo_client")
    def test_create_driver_rejects_missing_scenario_input(
        self, mock_client, mock_request, tools
    ):
        mock_client.return_value = MagicMock()

        result = json.loads(tools["create_or_update_driver"](
            name="no-ref",
            prompt_template="You are an angry customer who wants a refund.",
        ))

        assert "error" in result
        assert "scenario_input" in result["error"]
        # Rejected before any backend call.
        assert mock_request.call_count == 0

    @patch("src.tools.simulations.okareo_api_request")
    @patch("src.tools.simulations.get_okareo_client")
    def test_create_driver_accepts_property_path_reference(
        self, mock_client, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_request.return_value = {"id": "drv-3", "name": "pathed"}

        result = json.loads(tools["create_or_update_driver"](
            name="pathed",
            prompt_template="Play the role described in {scenario_input.persona.goal}.",
        ))

        assert result["created"] is True
        assert mock_request.call_count == 1

    @patch("src.tools.simulations.okareo_api_request")
    @patch("src.tools.simulations.get_okareo_client")
    def test_list_driver_voices(self, mock_client, mock_request, tools):
        mock_client.return_value = MagicMock()
        mock_request.side_effect = [
            [{"id": "nova", "language": "es-ES"},
             {"id": "alloy", "language": "en-US"}],
            [{"profile_name": "calm"}, {"profile_name": "energetic"}],
        ]

        result = json.loads(tools["list_driver_voices"]())

        assert result["voice_count"] == 2
        assert result["voice_profile_count"] == 2
        assert sorted(result["languages"]) == ["en-US", "es-ES"]


class TestAnalyticsAnnotations:
    """034: entity / project / usage attributes for driver + simulation."""

    @patch("src.tools.simulations.resolve_project", return_value=ResolvedProject(id="proj-1", name="Global", basis="default"))
    @patch("src.tools.simulations.okareo_api_request")
    @patch("src.tools.simulations.get_okareo_client")
    def test_create_or_update_driver_annotations(
        self, mock_client, mock_request, mock_resolve, tools
    ):
        from src.analytics_context import call_scope

        mock_client.return_value = MagicMock()
        mock_request.return_value = {
            "id": "drv-99", "name": "plain", "language": "en",
        }

        with call_scope() as annotations:
            result = json.loads(tools["create_or_update_driver"](
                name="plain",
                prompt_template="You are a caller. {scenario_input}",
                language="en",
            ))

        assert result["created"] is True
        assert annotations["entity_type"] == "driver"
        assert annotations["entity_id"] == "drv-99"
        assert annotations["project_id"] == "proj-1"
        assert annotations["is_voice"] is False
        assert annotations["language"] == "en"
        mock_resolve.assert_called_once()

    @patch("src.tools.simulations.resolve_project", return_value=ResolvedProject(id="proj-1", name="Global", basis="default"))
    @patch("src.tools.simulations.okareo_api_request")
    @patch("src.tools.simulations.get_okareo_client")
    def test_create_or_update_driver_is_voice(
        self, mock_client, mock_request, mock_resolve, tools
    ):
        from src.analytics_context import call_scope

        mock_client.return_value = MagicMock()
        mock_request.side_effect = [
            [{"id": "nova", "language": "en"}],
            [{"profile_name": "calm"}],
            {"id": "drv-v", "name": "voice", "voice": "nova", "language": "en"},
        ]

        with call_scope() as annotations:
            tools["create_or_update_driver"](
                name="voice",
                prompt_template="You are a caller. {scenario_input}",
                voice="nova",
                language="en",
            )

        assert annotations["is_voice"] is True

    @patch("src.tools.simulations._buffered_submit")
    @patch("src.tools.simulations.resolve_project", return_value=ResolvedProject(id="proj-1", name="Global", basis="default"))
    @patch("src.tools.simulations.get_okareo_client")
    def test_run_simulation_annotations_finished(
        self, mock_client, mock_resolve, mock_buffered, tools, sim_submission):
        from okareo_api_client.api import default as _default_pkg

        from src.analytics_context import call_scope

        okareo = MagicMock()
        mock_client.return_value = okareo
        scenario = MagicMock()
        scenario.name = "my-scenario"
        scenario.scenario_count = 5
        scenario.scenario_id = "sc-1"
        scen_mod = MagicMock()
        scen_mod.sync.return_value = [scenario]
        mock_buffered.return_value = (
            "finished", MagicMock(), "run-xyz", "https://app.okareo.com/r",
        )

        with patch.object(
            _default_pkg, "get_scenario_sets_v0_scenario_sets_get",
            scen_mod, create=True,
        ), call_scope() as annotations:
            out = json.loads(tools["run_simulation"](
                name="sim-1",
                scenario_name="my-scenario",
                target_name="tgt",
                repeats=2,
                max_turns=7,
            ))

        assert "error" not in out, out
        assert annotations["entity_type"] == "test_run"
        assert annotations["entity_id"] == "run-xyz"
        assert annotations["run_status"] == "finished"
        assert annotations["repeats"] == 2
        assert annotations["max_turns"] == 7
        assert annotations["is_rerun"] is False
        assert annotations["project_id"] == "proj-1"

    @patch("src.tools.simulations._buffered_submit")
    @patch("src.tools.simulations.resolve_project", return_value=ResolvedProject(id="proj-1", name="Global", basis="default"))
    @patch("src.tools.simulations.get_okareo_client")
    def test_run_simulation_running_and_rerun(
        self, mock_client, mock_resolve, mock_buffered, tools, sim_submission):
        from okareo_api_client.api import default as _default_pkg

        from src.analytics_context import call_scope

        okareo = MagicMock()
        mock_client.return_value = okareo
        scenario = MagicMock()
        scenario.name = "my-scenario"
        scenario.scenario_count = 5
        scenario.scenario_id = "sc-1"
        scen_mod = MagicMock()
        scen_mod.sync.return_value = [scenario]
        mock_buffered.return_value = (
            "running", None, "run-live", "https://app.okareo.com/r",
        )

        with patch.object(
            _default_pkg, "get_scenario_sets_v0_scenario_sets_get",
            scen_mod, create=True,
        ), patch(
            "src.tools.simulations.find_test_runs",
            return_value=[{
                "id": "old-run",
                "name": "old",
                "scenario_set_id": "sc-1",
                "mut_id": "m1",
            }],
        ), call_scope() as annotations:
            out = json.loads(tools["run_simulation"](
                name="sim-rerun",
                scenario_name="my-scenario",
                target_name="tgt",
                based_on_run_id="old-run",
            ))

        assert "error" not in out, out
        assert annotations["run_status"] == "running"
        assert annotations["is_rerun"] is True
        assert annotations["entity_id"] == "run-live"

    @patch("src.tools.simulations._buffered_submit")
    @patch("src.tools.simulations.resolve_project", return_value=ResolvedProject(id="proj-1", name="Global", basis="default"))
    @patch("src.tools.simulations.get_okareo_client")
    def test_run_simulation_failed_status(
        self, mock_client, mock_resolve, mock_buffered, tools, sim_submission):
        from okareo_api_client.api import default as _default_pkg

        from src.analytics_context import call_scope

        okareo = MagicMock()
        mock_client.return_value = okareo
        scenario = MagicMock()
        scenario.name = "my-scenario"
        scenario.scenario_count = 5
        scenario.scenario_id = "sc-1"
        scen_mod = MagicMock()
        scen_mod.sync.return_value = [scenario]
        mock_buffered.return_value = (
            "failed", RuntimeError("boom"), None, None,
        )

        with patch.object(
            _default_pkg, "get_scenario_sets_v0_scenario_sets_get",
            scen_mod, create=True,
        ), call_scope() as annotations:
            out = json.loads(tools["run_simulation"](
                name="sim-fail",
                scenario_name="my-scenario",
                target_name="tgt",
            ))

        assert "error" in out
        assert annotations["run_status"] == "failed"
        assert annotations["project_id"] == "proj-1"


class TestRunSimulationResolutionErrors:
    """Missing/unresolvable scenario and target must be recoverable in one retry."""

    @staticmethod
    def _scenario(name, scenario_id, rows=5):
        s = MagicMock()
        s.name = name
        s.scenario_id = scenario_id
        s.scenario_count = rows
        return s

    @patch("src.tools.simulations.resolve_project", return_value=ResolvedProject(id="proj-1", name="Global", basis="default"))
    @patch("src.tools.simulations.get_okareo_client")
    def test_missing_scenario_name_lists_candidates(
        self, mock_client, mock_resolve, tools
    ):
        from okareo_api_client.api import default as _default_pkg

        mock_client.return_value = MagicMock()
        scen_mod = MagicMock()
        scen_mod.sync.return_value = [
            self._scenario("billing-cases", "sc-1", rows=12),
            self._scenario("returns-cases", "sc-2", rows=3),
        ]

        with patch.object(
            _default_pkg, "get_scenario_sets_v0_scenario_sets_get",
            scen_mod, create=True,
        ):
            out = json.loads(tools["run_simulation"](name="sim", target_name="tgt"))

        assert "scenario_name is required" in out["error"]
        assert out["available_scenarios"] == [
            {"name": "billing-cases", "rows": 12},
            {"name": "returns-cases", "rows": 3},
        ]

    @patch("src.tools.simulations.resolve_project", return_value=ResolvedProject(id="proj-1", name="Global", basis="default"))
    @patch("src.tools.simulations.get_okareo_client")
    def test_unknown_scenario_name_suggests_near_match(
        self, mock_client, mock_resolve, tools
    ):
        from okareo_api_client.api import default as _default_pkg

        mock_client.return_value = MagicMock()
        scen_mod = MagicMock()
        scen_mod.sync.return_value = [self._scenario("billing-cases", "sc-1")]

        with patch.object(
            _default_pkg, "get_scenario_sets_v0_scenario_sets_get",
            scen_mod, create=True,
        ):
            out = json.loads(tools["run_simulation"](
                name="sim", scenario_name="billing_cases", target_name="tgt",
            ))

        assert "not found" in out["error"]
        assert out["did_you_mean"] == ["billing-cases"]
        assert out["available_scenarios"] == [{"name": "billing-cases", "rows": 5}]

    @patch("src.tools.simulations._fetch_targets")
    @patch("src.tools.simulations.resolve_project", return_value=ResolvedProject(id="proj-1", name="Global", basis="default"))
    @patch("src.tools.simulations.get_okareo_client")
    def test_missing_target_name_lists_candidates(
        self, mock_client, mock_resolve, mock_targets, tools
    ):
        from okareo_api_client.api import default as _default_pkg

        mock_client.return_value = MagicMock()
        mock_targets.return_value = [
            {"id": "m1", "name": "support-bot", "type": "voice"},
        ]
        scen_mod = MagicMock()
        scen_mod.sync.return_value = [self._scenario("billing-cases", "sc-1")]

        with patch.object(
            _default_pkg, "get_scenario_sets_v0_scenario_sets_get",
            scen_mod, create=True,
        ):
            out = json.loads(tools["run_simulation"](
                name="sim", scenario_name="billing-cases",
            ))

        assert "target_name is required" in out["error"]
        assert out["available_targets"] == [
            {"name": "support-bot", "type": "voice"},
        ]

    @patch("src.tools.simulations._fetch_drivers")
    @patch("src.tools.simulations._fetch_targets")
    @patch("src.tools.simulations._buffered_submit")
    @patch("src.tools.simulations.resolve_project", return_value=ResolvedProject(id="proj-1", name="Global", basis="default"))
    @patch("src.tools.simulations.get_okareo_client")
    def test_rerun_resolves_target_and_driver_by_id(
        self, mock_client, mock_resolve, mock_buffered, mock_targets,
        mock_drivers, tools, sim_submission,):
        """mut_id/driver_id from the original run map to names, not to the run name."""
        from okareo_api_client.api import default as _default_pkg

        okareo = MagicMock()
        mock_client.return_value = okareo
        mock_targets.return_value = [
            {"id": "m1", "name": "support-bot", "type": "voice"},
        ]
        mock_drivers.return_value = [{"id": "d1", "name": "angry-caller"}]
        scen_mod = MagicMock()
        scen_mod.sync.return_value = [self._scenario("billing-cases", "sc-1")]
        mock_buffered.return_value = (
            "running", None, "run-live", "https://app.okareo.com/r",
        )

        with patch.object(
            _default_pkg, "get_scenario_sets_v0_scenario_sets_get",
            scen_mod, create=True,
        ), patch(
            "src.tools.simulations.find_test_runs",
            return_value=[{
                "id": "old-run",
                "name": "a run name that is not a target name",
                "scenario_set_id": "sc-1",
                "mut_id": "m1",
                "driver_id": "d1",
            }],
        ):
            out = json.loads(tools["run_simulation"](
                name="sim-rerun", based_on_run_id="old-run",
            ))

        assert "error" not in out, out
        assert out["target"] == "support-bot"
        assert out["driver"] == "angry-caller"

    @patch("src.tools.simulations._fetch_targets", return_value=[])
    @patch("src.tools.simulations.resolve_project", return_value=ResolvedProject(id="proj-1", name="Global", basis="default"))
    @patch("src.tools.simulations.get_okareo_client")
    def test_rerun_with_deleted_scenario_explains_why(
        self, mock_client, mock_resolve, mock_targets, tools
    ):
        from okareo_api_client.api import default as _default_pkg

        mock_client.return_value = MagicMock()
        scen_mod = MagicMock()
        scen_mod.sync.return_value = [self._scenario("billing-cases", "sc-1")]

        with patch.object(
            _default_pkg, "get_scenario_sets_v0_scenario_sets_get",
            scen_mod, create=True,
        ), patch(
            "src.tools.simulations.find_test_runs",
            return_value=[{
                "id": "old-run",
                "name": "old",
                "scenario_set_id": "sc-deleted",
                "mut_id": "m1",
            }],
        ):
            out = json.loads(tools["run_simulation"](
                name="sim-rerun", based_on_run_id="old-run",
            ))

        assert "scenario_name is required" in out["error"]
        assert out["based_on_run_id"] == "old-run"
        assert any("no longer exists" in n for n in out["rerun_notes"])
        assert out["available_scenarios"] == [{"name": "billing-cases", "rows": 5}]


class TestListSimulationsMetricsRequest:
    """Summary mode documents itself as returning results without model_metrics and
    discards them when formatting, so it must not ask the API for them. Detailed
    mode renders them and must. See TestListToolsSkipRowLevelMetrics in test_tests.py.
    """

    def _run(self, detail_level):
        from unittest.mock import MagicMock, patch

        tools = _register_and_get_tools()
        find = MagicMock(return_value=[])

        with patch("src.tools.simulations.get_okareo_client", return_value=MagicMock()), \
             patch("src.tools.simulations.resolve_project", return_value=ResolvedProject(id="proj-1", name="Global", basis="default")), \
             patch("src.tools.simulations.find_test_runs", find):
            tools["list_simulations"](detail_level=detail_level)

        return find.call_args[0][1]

    def test_summary_does_not_request_metrics(self):
        assert self._run("summary").return_model_metrics is False

    def test_detailed_does_not_request_metrics_either(self):
        # detailed mode's cap to 5 is a client-side slice applied after the whole
        # response has arrived, so it bounds the output but not the request.
        assert self._run("detailed").return_model_metrics is False
