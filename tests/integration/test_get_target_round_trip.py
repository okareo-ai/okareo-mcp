"""Integration: get_target → modify → create_or_update_target round-trip.

Validates spec 023-tool-fixes US1 acceptance scenarios end-to-end against a
mocked Okareo SDK. Confirms:
- get_target returns a flat envelope matching create_or_update_target kwargs.
- Sensitive paths come back marked with the redaction sentinel.
- A payload with the sentinel still in place is rejected before any SDK call.
- A payload with sentinels substituted reaches the SDK with the expected kwargs.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.target_redaction import REDACTION_SENTINEL


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


# A realistic custom_endpoint Target with auth + streaming + concurrency.
SAMPLE_MUT = {
    "id": "mut-prod-chatbot",
    "name": "prod-chatbot",
    "tags": ["prod", "chatbot"],
    "sensitive_fields": [
        "auth_params.headers.Authorization",
        "auth_params.body.client_secret",
    ],
    "models": {
        "custom_endpoint": {
            "next_message_params": {
                "url": "https://prod.example.com/chat",
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "streaming": {
                    "stop": [{"value": "[DONE]"}],
                    "select": [
                        {"path": "response.choices[0].delta.content", "value": True}
                    ],
                },
            },
            "auth_params": {
                "url": "https://prod.example.com/oauth/token",
                "method": "POST",
                "headers": {"Authorization": ""},
                "body": {"client_id": "abc", "client_secret": ""},
                "status_code": 200,
                "response_access_token_path": "response.access_token",
            },
            "max_parallel_requests": 5,
        }
    },
}


# ---------------------------------------------------------------------------
# get_target shape — Phase 3 US1 acceptance scenarios 1–4
# ---------------------------------------------------------------------------

@patch("src.tools.simulations.resolve_project_id")
@patch("src.tools.simulations.get_okareo_client")
def test_get_target_returns_flat_envelope_for_custom_endpoint(
    mock_client, mock_project, tools
):
    mock_client.return_value = MagicMock()
    mock_project.return_value = "proj-123"

    with patch(
        "okareo_api_client.api.default.get_all_models_under_test_v0_models_under_test_get.sync",
        return_value=[SAMPLE_MUT],
    ):
        response = json.loads(tools["get_target"](name="prod-chatbot"))

    # Top-level metadata
    assert response["target_id"] == "mut-prod-chatbot"
    assert response["name"] == "prod-chatbot"
    assert response["type"] == "custom_endpoint"
    assert response["tags"] == ["prod", "chatbot"]

    # Custom-endpoint kwargs flattened to top level (no "target" wrapper)
    assert "target" not in response
    assert "has_auth" not in response
    assert "sensitive_fields_count" not in response

    # auth_params present with create-kwargs shape (FR-001)
    assert "auth_params" in response
    assert response["auth_params"]["url"] == "https://prod.example.com/oauth/token"
    assert response["auth_params"]["method"] == "POST"
    assert (
        response["auth_params"]["response_access_token_path"]
        == "response.access_token"
    )

    # Sensitive paths replaced with the sentinel (FR-003, FR-004)
    assert response["auth_params"]["headers"]["Authorization"] == REDACTION_SENTINEL
    assert response["auth_params"]["body"]["client_secret"] == REDACTION_SENTINEL
    # Non-sensitive sibling preserved
    assert response["auth_params"]["body"]["client_id"] == "abc"

    # Streaming nested inside next_message_params (FR-007)
    assert "streaming" not in response
    assert "streaming" in response["next_message_params"]

    # max_parallel_requests promoted to top level (FR-008)
    assert response["max_parallel_requests"] == 5

    # sensitive_fields returned verbatim (FR-005)
    assert response["sensitive_fields"] == [
        "auth_params.headers.Authorization",
        "auth_params.body.client_secret",
    ]


@patch("src.tools.simulations.resolve_project_id")
@patch("src.tools.simulations.get_okareo_client")
def test_get_target_omits_auth_params_when_absent(mock_client, mock_project, tools):
    """A Target with no auth_params returns no auth_params key (not null, not {})."""
    mock_client.return_value = MagicMock()
    mock_project.return_value = "proj-123"

    mut = {
        "id": "mut-no-auth",
        "name": "no-auth-chatbot",
        "models": {
            "custom_endpoint": {
                "next_message_params": {"url": "https://x.example.com", "method": "POST"},
            }
        },
    }

    with patch(
        "okareo_api_client.api.default.get_all_models_under_test_v0_models_under_test_get.sync",
        return_value=[mut],
    ):
        response = json.loads(tools["get_target"](name="no-auth-chatbot"))

    assert "auth_params" not in response  # FR-002
    assert "sensitive_fields" not in response
    assert "max_parallel_requests" not in response


@patch("src.tools.simulations.resolve_project_id")
@patch("src.tools.simulations.get_okareo_client")
def test_get_target_generation_shape_unchanged(mock_client, mock_project, tools):
    """Generation Targets keep today's response shape (FR-013)."""
    mock_client.return_value = MagicMock()
    mock_project.return_value = "proj-123"

    mut = {
        "id": "mut-gen",
        "name": "my-gen",
        "models": {"generation": {"model_id": "gpt-4o-mini", "temperature": 0.0}},
    }

    with patch(
        "okareo_api_client.api.default.get_all_models_under_test_v0_models_under_test_get.sync",
        return_value=[mut],
    ):
        response = json.loads(tools["get_target"](name="my-gen"))

    # Generation targets retain the "target" wrapper key
    assert response["type"] == "generation"
    assert "target" in response
    assert response["target"]["model_id"] == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# create_or_update_target sentinel rejection — Phase 3 US1 acceptance 5 (negative)
# ---------------------------------------------------------------------------

@patch("src.tools.simulations.get_okareo_client")
def test_create_rejects_unsubstituted_sentinel(mock_client, tools):
    """A clone payload still carrying ***REDACTED*** is rejected pre-SDK."""
    mock_okareo = MagicMock()
    mock_client.return_value = mock_okareo

    result = json.loads(tools["create_or_update_target"](
        name="staging-chatbot",
        type="custom_endpoint",
        next_message_params={
            "url": "https://staging.example.com/chat",
            "method": "POST",
        },
        auth_params={
            "url": "https://staging.example.com/oauth/token",
            "method": "POST",
            "headers": {"Authorization": REDACTION_SENTINEL},  # not replaced
            "body": {"client_id": "abc", "client_secret": "new-secret"},
            "response_access_token_path": "response.access_token",
        },
        sensitive_fields=["auth_params.headers.Authorization", "auth_params.body.client_secret"],
        max_parallel_requests=5,
    ))

    assert "error" in result
    assert "Redaction sentinel still present" in result["error"]
    assert "auth_params.headers.Authorization" in result["sentinel_paths"]
    # SDK MUST NOT be called when sentinels are present (FR-013, FR-026, SC-004)
    mock_okareo.create_or_update_target.assert_not_called()


# ---------------------------------------------------------------------------
# Full round-trip — Phase 3 US1 acceptance 5 (positive)
# ---------------------------------------------------------------------------

@patch("src.tools.simulations.resolve_project_id")
@patch("src.tools.simulations.get_okareo_client")
def test_round_trip_clone_succeeds_after_substitution(
    mock_client, mock_project, tools
):
    """get_target → substitute sentinels → create_or_update_target succeeds."""
    mock_okareo = MagicMock()
    mock_result = MagicMock()
    mock_result.id = "mut-staging-chatbot"
    mock_result.name = "staging-chatbot"
    mock_okareo.create_or_update_target.return_value = mock_result
    mock_client.return_value = mock_okareo
    mock_project.return_value = "proj-123"

    # Step 1: get_target
    with patch(
        "okareo_api_client.api.default.get_all_models_under_test_v0_models_under_test_get.sync",
        return_value=[SAMPLE_MUT],
    ):
        envelope = json.loads(tools["get_target"](name="prod-chatbot"))

    # Step 2: user/copilot substitution
    envelope["name"] = "staging-chatbot"
    envelope["next_message_params"]["url"] = "https://staging.example.com/chat"
    envelope["auth_params"]["url"] = "https://staging.example.com/oauth/token"
    envelope["auth_params"]["headers"]["Authorization"] = "Bearer real-token"
    envelope["auth_params"]["body"]["client_secret"] = "real-secret"

    # Drop read-only metadata before passing as kwargs
    envelope.pop("target_id")
    envelope.pop("tags", None)

    # Step 3: create_or_update_target with the substituted envelope
    result = json.loads(tools["create_or_update_target"](**envelope))

    assert "error" not in result
    assert result["name"] == "staging-chatbot"

    # SDK was called once; verify the kwargs reached it intact.
    mock_okareo.create_or_update_target.assert_called_once()
    _target_arg, kwargs = (
        mock_okareo.create_or_update_target.call_args.args,
        mock_okareo.create_or_update_target.call_args.kwargs,
    )
    # sensitive_fields was forwarded
    assert "sensitive_fields" in kwargs
    assert "auth_params.headers.Authorization" in kwargs["sensitive_fields"]
