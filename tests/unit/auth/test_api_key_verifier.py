"""Tests for src/auth/api_key_verifier.py."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def verifier_cls():
    from src.auth.api_key_verifier import OkareoAPIKeyVerifier

    return OkareoAPIKeyVerifier


class TestOkareoAPIKeyVerifier:
    def test_valid_key_returns_session_credential(self, verifier_cls):
        mock_project = MagicMock()
        mock_project.id = "proj-uuid-123"
        mock_okareo = MagicMock()
        mock_okareo.get_projects.return_value = [mock_project]

        async def run():
            with patch("src.auth.api_key_verifier.Okareo", return_value=mock_okareo):
                v = verifier_cls(base_url="https://api.okareo.example")
                return await v.verify("okareo-VALID-KEY")

        cred = asyncio.run(run())
        assert cred is not None
        assert cred.kind == "api_key"
        assert cred.api_key == "okareo-VALID-KEY"
        # org_id surrogate is first project's id (see plan: no /me endpoint)
        assert cred.org_id == "proj-uuid-123"

    def test_invalid_key_returns_none(self, verifier_cls):
        # The Okareo SDK raises TypeError on invalid key (per existing
        # src/okareo_client.py docstring). The verifier must translate this
        # to None so the auth middleware returns 401.
        def _raise_on_init(*_a, **_kw):
            raise TypeError("Invalid API key format")

        async def run():
            with patch("src.auth.api_key_verifier.Okareo", side_effect=_raise_on_init):
                v = verifier_cls(base_url="https://api.okareo.example")
                return await v.verify("okareo-BAD-KEY")

        assert asyncio.run(run()) is None

    def test_no_projects_returns_none(self, verifier_cls):
        # Edge case: key is valid but the account has no projects. Without a
        # project id we can't derive an org_id; reject the session.
        mock_okareo = MagicMock()
        mock_okareo.get_projects.return_value = []

        async def run():
            with patch("src.auth.api_key_verifier.Okareo", return_value=mock_okareo):
                v = verifier_cls(base_url="https://api.okareo.example")
                return await v.verify("okareo-VALID-KEY")

        assert asyncio.run(run()) is None

    def test_network_error_raises_for_caller(self, verifier_cls):
        # Connectivity issues are not "invalid key" — they should propagate
        # so the combined verifier can decide between 401 and 502.
        import httpx

        async def run():
            with patch(
                "src.auth.api_key_verifier.Okareo",
                side_effect=httpx.ConnectError("upstream down"),
            ):
                v = verifier_cls(base_url="https://api.okareo.example")
                with pytest.raises(httpx.ConnectError):
                    await v.verify("okareo-VALID-KEY")

        asyncio.run(run())
