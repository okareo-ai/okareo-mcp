"""Unit tests for credential redaction across error paths (US3, FR-007, SC-005).

Drives error formatting with sentinel credentials and asserts they never
appear in the formatted output. Sentinels chosen to be unmistakable.
"""

from __future__ import annotations

import json

from src.error_handling import _redact_credentials, format_tool_error


JWT_SENTINEL = (
    "eyJokareo_TESTSENTINEL_AAAAAAAAAAAAAAAAAAAAAAAAAAA.bbbbbbbbb.cccccccccc"
)
API_KEY_SENTINEL = "okareo-TESTSENTINEL-xxxxxxxxxxxxxxxxxxxxxxxxx"


class TestRedactCredentials:
    def test_redacts_bearer_token(self):
        text = f"401 Unauthorized: Authorization: Bearer {JWT_SENTINEL}"
        out = _redact_credentials(text)
        assert JWT_SENTINEL not in out
        assert "[REDACTED]" in out

    def test_redacts_authorization_header_value(self):
        text = f"Header dump — Authorization: {API_KEY_SENTINEL}"
        out = _redact_credentials(text)
        assert API_KEY_SENTINEL not in out

    def test_short_strings_untouched(self):
        # Bearer pattern requires 20+ chars; short prefixes are NOT a real
        # credential and pass through unchanged.
        assert _redact_credentials("Bearer abc") == "Bearer abc"

    def test_empty_passes_through(self):
        assert _redact_credentials("") == ""


class TestFormatToolError:
    def test_message_strips_bearer(self):
        exc = ValueError(f"Backend returned: Authorization: Bearer {JWT_SENTINEL}")
        rendered = format_tool_error(exc, {})
        payload = json.loads(rendered)
        assert JWT_SENTINEL not in json.dumps(payload), (
            "JWT sentinel leaked into formatted tool error"
        )

    def test_okareo_api_key_env_redacted(self, monkeypatch):
        monkeypatch.setenv("OKAREO_API_KEY", API_KEY_SENTINEL)
        exc = ValueError(f"the key was: {API_KEY_SENTINEL}")
        rendered = format_tool_error(exc, {})
        payload = json.loads(rendered)
        assert API_KEY_SENTINEL not in json.dumps(payload)

    def test_provider_key_registry_redacted(self):
        # key_registry is the existing path for redacting provider keys
        # (OpenAI, Anthropic, etc.); we ensure format_tool_error still
        # runs it after our new helper. Use a value the existing
        # sanitize_error helper can spot — passing it via key_registry.
        provider_sentinel = "sk-PROVIDER-SENTINEL-yyyyyyyyyyyyyyyyy"
        registry = {"OPENAI_API_KEY": provider_sentinel}
        exc = ValueError(f"call failed: token={provider_sentinel}")
        rendered = format_tool_error(exc, registry)
        payload = json.loads(rendered)
        assert provider_sentinel not in json.dumps(payload)
