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


class TestAPIKeyPathsLogNoKeyMaterial:
    """048 SC-005: key validation and the shared-connection token path write
    no key, envelope or fragment of either to logs or stderr."""

    SIGNING_KEY = "a-stable-dcr-signing-key-of-at-least-32-bytes"

    def _key(self) -> str:
        import jwt as pyjwt

        return pyjwt.encode(
            {"type": "apiKey", "tenantId": "tenant-H", "sub": "u"},
            "hygiene-signing-key-that-is-32-bytes!",
            algorithm="HS256",
        )

    def _assert_clean(self, logged: str, *secrets: str) -> None:
        for secret in secrets:
            assert secret not in logged
            # Not even a recognisable piece of the signature segment.
            tail = secret.rsplit(".", 1)[-1]
            for i in range(0, max(len(tail) - 20, 0) + 1, 5):
                assert tail[i : i + 20] not in logged

    def test_validation_outcomes(self, caplog, capsys):
        import asyncio
        import logging

        import httpx

        from src.auth.api_key_verifier import OkareoAPIKeyVerifier

        key = self._key()
        responses = iter(
            [httpx.Response(200, json=[]), httpx.Response(401)]
        )

        def handler(_request):
            try:
                return next(responses)
            except StopIteration:
                raise httpx.ConnectError("down") from None

        verifier = OkareoAPIKeyVerifier(
            "https://api.okareo.example/", transport=httpx.MockTransport(handler)
        )
        with caplog.at_level(logging.DEBUG):
            for _ in range(3):
                asyncio.run(verifier.validate(key, use_cache=False))
        out = capsys.readouterr()
        self._assert_clean(caplog.text + out.err + out.out, key)

    def test_shared_token_on_the_bearer_path(self, caplog, capsys):
        import asyncio
        import logging

        import pytest

        from src.auth.api_key_verifier import KeyValidation
        from src.auth.jwks_cache import JWKSCache
        from src.auth.shared_connection import SharedConnectionKeys, issue_tokens
        from src.auth.verifier import CombinedTokenVerifier, InvalidAPIKeyError

        key = self._key()
        keys = SharedConnectionKeys.from_signing_key(self.SIGNING_KEY)
        tokens = issue_tokens(keys, key, "tenant-H")
        outcomes = iter(["valid", "invalid"])

        async def validator(_k):
            outcome = next(outcomes)
            if outcome == "valid":
                return KeyValidation(outcome="valid", tenant_id="tenant-H")
            return KeyValidation(outcome=outcome)

        verifier = CombinedTokenVerifier(
            issuer_url="https://auth.example",
            resource_server_url="http://localhost:8080",
            jwks_cache=JWKSCache("https://auth.example"),
            api_key_validator=validator,
            shared_keys=keys,
        )
        with caplog.at_level(logging.DEBUG):
            asyncio.run(verifier.verify_token(tokens["access_token"]))
            with pytest.raises(InvalidAPIKeyError) as exc:
                asyncio.run(verifier.verify_token(tokens["access_token"]))
        out = capsys.readouterr()
        logged = caplog.text + out.err + out.out + exc.value.description
        self._assert_clean(logged, key, tokens["access_token"], tokens["refresh_token"])
