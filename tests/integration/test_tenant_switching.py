"""Integration test for end-to-end tenant switching (T052 / US4 / FR-024).

Verifies the full chain:

    list_tenants → switch_tenant → tool call uses tenant-scoped JWT

without standing up a full HTTP transport. The boundaries tested here:

  - tools/tenants.py validates against allowed_tenants then mints a new JWT
    via Frontegg's tenant-switch refresh endpoint (mocked).
  - okareo_client.py reads the per-session override and uses the override
    JWT as the Okareo SDK's api_key.
  - The Bearer-API-key fallback rejects both tenant tools.
  - Two concurrent sessions hold independent overrides (each with its own
    tenant-scoped JWT).

2026-05-18 pivot: there is no `X-Okareo-Org-Override` header anymore. The
override is a different JWT entirely.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.auth import frontegg_user_info, refresh_token_cache, tenant_state
from src.auth.context import (
    SessionCredential,
    _reset_for_tests as _reset_credential,
    set_session_credential,
)
from src.auth.frontegg_user_info import Tenant


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    tenant_state._reset_for_tests()
    frontegg_user_info._reset_for_tests()
    refresh_token_cache._reset_for_tests()
    _reset_credential()
    monkeypatch.setenv("FRONTEGG_DOMAIN", "test.frontegg.example")
    monkeypatch.delenv("OKAREO_API_KEY", raising=False)
    yield
    tenant_state._reset_for_tests()
    frontegg_user_info._reset_for_tests()
    refresh_token_cache._reset_for_tests()
    _reset_credential()


def _capture_tools():
    from src.tools import tenants as tenants_module

    registry: dict[str, callable] = {}

    class _StubMCP:
        def tool(self, *args, **kwargs):
            def _decorator(fn):
                registry[fn.__name__] = fn
                return fn

            return _decorator

    tenants_module.register_tools(_StubMCP())
    return registry


class TestEndToEndHappyPath:
    """One OAuth session — list_tenants → switch_tenant → outbound Okareo
    call uses the tenant-scoped JWT minted by Frontegg."""

    def test_switch_propagates_via_jwt_substitution(self):
        cred = SessionCredential(
            kind="oauth",
            api_key="jwt-DEFAULT",
            org_id="t-default",
            subject="user-42",
            allowed_tenants=("t-default", "t-other"),
        )
        set_session_credential(cred)
        refresh_token_cache.set_token("user-42", "rt-initial")

        tools = _capture_tools()
        captured_api_keys: list[str] = []

        def _fake_okareo(api_key, base_path):
            captured_api_keys.append(api_key)
            return MagicMock(name=f"okareo:{api_key}")

        async def run():
            with patch(
                "src.tools.tenants._current_session_id", return_value="sess-A"
            ):
                # 1) list_tenants returns the JWT default as current.
                with patch(
                    "src.tools.tenants.get_user_tenants",
                    AsyncMock(return_value=[
                        Tenant(id="t-default", name="Default Co"),
                        Tenant(id="t-other", name="Other Co"),
                    ]),
                ):
                    list_payload = json.loads(await tools["list_tenants"]())
                    assert list_payload["active_tenant_id"] == "t-default"
                    assert list_payload["active_tenant_source"] == "jwt_default"

                # 2) switch_tenant mints a new tenant-scoped JWT via Frontegg.
                with patch(
                    "src.tools.tenants.get_user_tenants",
                    AsyncMock(return_value=[Tenant(id="t-other", name="Other Co")]),
                ), patch(
                    "src.tools.tenants.refresh_with_tenant",
                    AsyncMock(return_value={
                        "access_token": "jwt-BOUND-TO-OTHER",
                        "refresh_token": "rt-rotated",
                        "expires_in": 1800,
                    }),
                ):
                    switch_payload = json.loads(
                        await tools["switch_tenant"](tenant_id="t-other")
                    )
                    assert switch_payload["active_tenant_id"] == "t-other"

            # 3) An ordinary tool call constructs the Okareo client using
            # the override JWT, NOT the credential's default JWT.
            with patch("src.okareo_client.Okareo", side_effect=_fake_okareo), \
                 patch(
                     "src.okareo_client._current_session_id",
                     return_value="sess-A",
                 ):
                from src.okareo_client import get_okareo_client

                get_okareo_client()

            assert captured_api_keys == ["jwt-BOUND-TO-OTHER"]

        asyncio.run(run())


class TestBearerFallbackRejection:
    def test_both_tools_reject_with_oauth_required(self):
        cred = SessionCredential(
            kind="api_key",
            api_key="okareo-key",
            org_id="t-from-key",
        )
        set_session_credential(cred)

        tools = _capture_tools()

        async def run():
            with patch(
                "src.tools.tenants._current_session_id", return_value="sess-A"
            ):
                list_payload = json.loads(await tools["list_tenants"]())
                switch_payload = json.loads(
                    await tools["switch_tenant"](tenant_id="t-other")
                )

            assert list_payload["error"]["code"] == "tenant_selection_requires_oauth"
            assert switch_payload["error"]["code"] == "tenant_selection_requires_oauth"
            assert tenant_state.get_override("sess-A") is None

        asyncio.run(run())


class TestConcurrentSessionsDontCollide:
    def test_two_sessions_two_tenants(self):
        """Two MCP sessions for the same user with different switch_tenant
        targets — each session's override holds its own tenant-scoped JWT."""
        cred = SessionCredential(
            kind="oauth",
            api_key="jwt-DEFAULT",
            org_id="t-default",
            subject="user-42",
            allowed_tenants=("t-1", "t-2", "t-3"),
        )
        set_session_credential(cred)
        refresh_token_cache.set_token("user-42", "rt-initial")

        tools = _capture_tools()

        # Each switch returns a different tenant-bound JWT.
        switch_responses = iter([
            {"access_token": "jwt-bound-to-t1", "refresh_token": "rt-a", "expires_in": 1800},
            {"access_token": "jwt-bound-to-t2", "refresh_token": "rt-b", "expires_in": 1800},
        ])

        async def _fake_refresh(*, refresh_token, tenant_id, frontegg_domain, frontegg_client_id="", current_access_token="", frontegg_vendor_id=""):
            return next(switch_responses)

        async def run():
            with patch(
                "src.tools.tenants.get_user_tenants",
                AsyncMock(return_value=[
                    Tenant(id="t-1", name="One"),
                    Tenant(id="t-2", name="Two"),
                ]),
            ), patch(
                "src.tools.tenants.refresh_with_tenant",
                _fake_refresh,
            ):
                with patch(
                    "src.tools.tenants._current_session_id", return_value="sess-A"
                ):
                    await tools["switch_tenant"](tenant_id="t-1")
                with patch(
                    "src.tools.tenants._current_session_id", return_value="sess-B"
                ):
                    await tools["switch_tenant"](tenant_id="t-2")

            rec_a = tenant_state.get_override("sess-A")
            rec_b = tenant_state.get_override("sess-B")
            assert rec_a.tenant_id == "t-1"
            assert rec_a.access_token == "jwt-bound-to-t1"
            assert rec_b.tenant_id == "t-2"
            assert rec_b.access_token == "jwt-bound-to-t2"

        asyncio.run(run())


class TestNoOverrideNoSubstitution:
    """Regression guard — a session that never calls switch_tenant must
    have its outbound Okareo calls use the original credential JWT."""

    def test_jwt_default_session_uses_credential_jwt(self):
        cred = SessionCredential(
            kind="oauth",
            api_key="jwt-DEFAULT",
            org_id="t-default",
            subject="user-42",
        )
        set_session_credential(cred)

        captured_api_keys: list[str] = []

        def _fake_okareo(api_key, base_path):
            captured_api_keys.append(api_key)
            return MagicMock()

        with patch("src.okareo_client.Okareo", side_effect=_fake_okareo), \
             patch("src.okareo_client._current_session_id", return_value="sess-A"):
            from src.okareo_client import get_okareo_client

            get_okareo_client()

        assert captured_api_keys == ["jwt-DEFAULT"]
