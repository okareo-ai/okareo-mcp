"""Unit tests covering US2 (auth inspection) and US3 (streaming clarity) for
get_target output (spec 023-tool-fixes).

These are guarantees built on top of US1's get_target reshape — verifying
that the new envelope makes a Target's auth endpoint diagnosable from a
copilot conversation, and that streaming stays nested inside the
message-endpoint params (no Target-level alias).
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.target_redaction import REDACTION_SENTINEL


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


# ---------------------------------------------------------------------------
# US2 — Inspect a Target's auth endpoint
# ---------------------------------------------------------------------------

@patch("src.tools.simulations.resolve_project_id")
@patch("src.tools.simulations.get_okareo_client")
def test_auth_endpoint_url_method_and_response_path_are_visible(
    mock_client, mock_project, tools
):
    """With auth_params configured, response surfaces every shape field a
    debugger needs (url, method, response_access_token_path, header keys,
    body keys) without consulting any external source.
    """
    mock_client.return_value = MagicMock()
    mock_project.return_value = "proj-123"

    mut = {
        "id": "mut-1",
        "name": "with-auth",
        "models": {
            "custom_endpoint": {
                "next_message_params": {"url": "https://x.example.com", "method": "POST"},
                "auth_params": {
                    "url": "https://x.example.com/oauth/token",
                    "method": "POST",
                    "headers": {
                        "Authorization": "Bearer real-token",
                        "Content-Type": "application/json",
                    },
                    "body": {"grant_type": "client_credentials", "client_id": "abc"},
                    "status_code": 200,
                    "response_access_token_path": "response.access_token",
                },
            }
        },
    }

    with patch(
        "okareo_api_client.api.default.get_all_models_under_test_v0_models_under_test_get.sync",
        return_value=[mut],
    ):
        resp = json.loads(tools["get_target"](name="with-auth"))

    auth = resp["auth_params"]
    assert auth["url"] == "https://x.example.com/oauth/token"
    assert auth["method"] == "POST"
    assert auth["response_access_token_path"] == "response.access_token"
    assert auth["status_code"] == 200
    # Header KEYS visible (FR-001) — values may be redacted in production but
    # the structural shape is intact.
    assert set(auth["headers"].keys()) == {"Authorization", "Content-Type"}
    # Body KEYS visible
    assert set(auth["body"].keys()) == {"grant_type", "client_id"}


@patch("src.tools.simulations.resolve_project_id")
@patch("src.tools.simulations.get_okareo_client")
def test_sensitive_fields_paths_show_sentinel(mock_client, mock_project, tools):
    """Every path in the Target's sensitive_fields list comes back with
    its value replaced by the redaction sentinel (FR-003, FR-004).
    """
    mock_client.return_value = MagicMock()
    mock_project.return_value = "proj-123"

    mut = {
        "id": "mut-2",
        "name": "redacted",
        "sensitive_fields": [
            "auth_params.headers.Authorization",
            "auth_params.body.client_secret",
        ],
        "models": {
            "custom_endpoint": {
                "next_message_params": {"url": "https://x.example.com", "method": "POST"},
                "auth_params": {
                    "url": "https://x.example.com/oauth/token",
                    "method": "POST",
                    "headers": {"Authorization": "real-secret"},
                    "body": {"client_id": "abc", "client_secret": "real-secret"},
                },
            }
        },
    }

    with patch(
        "okareo_api_client.api.default.get_all_models_under_test_v0_models_under_test_get.sync",
        return_value=[mut],
    ):
        resp = json.loads(tools["get_target"](name="redacted"))

    assert resp["auth_params"]["headers"]["Authorization"] == REDACTION_SENTINEL
    assert resp["auth_params"]["body"]["client_secret"] == REDACTION_SENTINEL
    # Non-sensitive sibling stays intact
    assert resp["auth_params"]["body"]["client_id"] == "abc"
    # sensitive_fields list returned verbatim (FR-005)
    assert resp["sensitive_fields"] == [
        "auth_params.headers.Authorization",
        "auth_params.body.client_secret",
    ]


# ---------------------------------------------------------------------------
# US3 — Streaming is per-endpoint, not Target-wide
# ---------------------------------------------------------------------------

@patch("src.tools.simulations.resolve_project_id")
@patch("src.tools.simulations.get_okareo_client")
def test_streaming_lives_inside_next_message_params_only(
    mock_client, mock_project, tools
):
    """For a Target with streaming configured, the get_target response
    places `streaming` only inside `next_message_params` — never as a
    top-level Target field (FR-007).
    """
    mock_client.return_value = MagicMock()
    mock_project.return_value = "proj-123"

    mut = {
        "id": "mut-3",
        "name": "streaming-target",
        "models": {
            "custom_endpoint": {
                "next_message_params": {
                    "url": "https://x.example.com",
                    "method": "POST",
                    "streaming": {
                        "stop": [{"value": "[DONE]"}],
                        "select": [
                            {"path": "response.choices[0].delta.content", "value": True}
                        ],
                    },
                },
            }
        },
    }

    with patch(
        "okareo_api_client.api.default.get_all_models_under_test_v0_models_under_test_get.sync",
        return_value=[mut],
    ):
        resp = json.loads(tools["get_target"](name="streaming-target"))

    # No top-level streaming key (FR-007)
    assert "streaming" not in resp
    # Streaming nested inside next_message_params
    assert "streaming" in resp["next_message_params"]
    assert resp["next_message_params"]["streaming"]["stop"][0]["value"] == "[DONE]"


def test_no_target_level_streaming_alias_documented(tools):
    """Sanity-check: the tool docstrings do not advertise a Target-level
    streaming param. Streaming guidance must live under message-endpoint
    params (FR-007).
    """
    create_doc = tools["create_or_update_target"].__doc__ or ""
    get_doc = tools["get_target"].__doc__ or ""

    # No top-level streaming arg/section
    assert "streaming: " not in create_doc.replace("'streaming'", "")
    # Streaming guidance, where mentioned, sits inside next_message_params
    # / start_session_params docs (substring co-occurrence check).
    assert "streaming" in create_doc.lower()
    assert "next_message_params" in create_doc
    # get_target docstring does not introduce a streaming concept of its own
    assert "Target-level streaming" not in get_doc
