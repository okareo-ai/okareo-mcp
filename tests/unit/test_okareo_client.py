"""Unit tests for src/okareo_client.py — get_okareo_client().

Since feature 030, tenant selection happens at sign-in and there is no
per-session override: the credential the request presents is already scoped to
the authorized organization, so ``get_okareo_client`` always uses
``credential.api_key`` as the Okareo SDK's ``api_key``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.auth.context import (
    SessionCredential,
    _reset_for_tests as _reset_credential,
    set_session_credential,
)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    _reset_credential()
    monkeypatch.delenv("OKAREO_API_KEY", raising=False)
    yield
    _reset_credential()


class TestCreateOkareoClient:
    """The constructor is a thin pass-through to ``Okareo(api_key=..., base_path=...)``."""

    def test_passes_api_key_and_base_path(self):
        with patch("src.okareo_client.Okareo") as okareo_cls:
            from src.okareo_client import create_okareo_client

            create_okareo_client("key-1", "https://api.okareo.com/")

        okareo_cls.assert_called_once_with(
            api_key="key-1", base_path="https://api.okareo.com/"
        )


class TestGetOkareoClient:
    def test_http_mode_uses_credential_jwt(self, monkeypatch):
        """The presented (already tenant-scoped) JWT is used as the api_key."""
        cred = SessionCredential(
            kind="oauth",
            api_key="jwt-scoped-tenant",
            org_id="t-1",
            subject="user-42",
        )
        set_session_credential(cred)

        with patch("src.okareo_client.Okareo") as okareo_cls, \
             patch("src.okareo_client._current_session_id", return_value="sess-A"):
            from src.okareo_client import get_okareo_client

            get_okareo_client()

        okareo_cls.assert_called_once()
        _, kwargs = okareo_cls.call_args
        assert kwargs["api_key"] == "jwt-scoped-tenant"

    def test_stdio_mode_uses_env_key(self, monkeypatch):
        monkeypatch.setenv("OKAREO_API_KEY", "env-key-xyz")

        with patch("src.okareo_client.Okareo") as okareo_cls:
            from src.okareo_client import get_okareo_client

            get_okareo_client()

        _, kwargs = okareo_cls.call_args
        assert kwargs["api_key"] == "env-key-xyz"
