"""Unit tests for src/okareo_client.py — get_okareo_client() and the
tenant-scoped JWT substitution (FR-024, 2026-05-18 pivot).

The override is no longer a header; it's a NEW Frontegg-issued JWT bound to
the target tenant. When ``switch_tenant`` runs successfully, that JWT is
written to ``tenant_state`` and ``get_okareo_client`` uses it as the
Okareo SDK's ``api_key`` instead of the credential's original JWT.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.auth import tenant_state
from src.auth.context import (
    SessionCredential,
    _reset_for_tests as _reset_credential,
    set_session_credential,
)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    tenant_state._reset_for_tests()
    _reset_credential()
    monkeypatch.delenv("OKAREO_API_KEY", raising=False)
    yield
    tenant_state._reset_for_tests()
    _reset_credential()


class TestCreateOkareoClient:
    """The constructor is now a thin pass-through to ``Okareo(api_key=..., base_path=...)``;
    there's no longer an override_tenant_id kwarg — the override JWT is passed
    in as the api_key by the caller."""

    def test_passes_api_key_and_base_path(self):
        with patch("src.okareo_client.Okareo") as okareo_cls:
            from src.okareo_client import create_okareo_client

            create_okareo_client("key-1", "https://api.okareo.com/")

        okareo_cls.assert_called_once_with(
            api_key="key-1", base_path="https://api.okareo.com/"
        )


class TestGetOkareoClient:
    def test_http_mode_with_override_uses_override_jwt(self, monkeypatch):
        """When switch_tenant has set an override, get_okareo_client uses
        the override's tenant-scoped JWT as the SDK api_key."""
        cred = SessionCredential(
            kind="oauth",
            api_key="jwt-DEFAULT-tenant",
            org_id="t-1",
            subject="user-42",
        )
        set_session_credential(cred)
        tenant_state.set_override("sess-A", "t-2", "jwt-T2-bound")

        with patch("src.okareo_client.Okareo") as okareo_cls, \
             patch("src.okareo_client._current_session_id", return_value="sess-A"):
            from src.okareo_client import get_okareo_client

            get_okareo_client()

        # The override JWT — not the credential JWT — was passed as api_key.
        okareo_cls.assert_called_once()
        _, kwargs = okareo_cls.call_args
        assert kwargs["api_key"] == "jwt-T2-bound"

    def test_http_mode_without_override_uses_credential_jwt(self, monkeypatch):
        cred = SessionCredential(
            kind="oauth",
            api_key="jwt-DEFAULT-tenant",
            org_id="t-1",
            subject="user-42",
        )
        set_session_credential(cred)
        # No override set.

        with patch("src.okareo_client.Okareo") as okareo_cls, \
             patch("src.okareo_client._current_session_id", return_value="sess-A"):
            from src.okareo_client import get_okareo_client

            get_okareo_client()

        _, kwargs = okareo_cls.call_args
        assert kwargs["api_key"] == "jwt-DEFAULT-tenant"

    def test_stdio_mode_uses_env_key(self, monkeypatch):
        monkeypatch.setenv("OKAREO_API_KEY", "env-key-xyz")

        with patch("src.okareo_client.Okareo") as okareo_cls:
            from src.okareo_client import get_okareo_client

            get_okareo_client()

        _, kwargs = okareo_cls.call_args
        assert kwargs["api_key"] == "env-key-xyz"
