"""Unit tests for src/tools/tenants.py — list_tenants + switch_tenant MCP tools.

Covers the tool surface defined in contracts/tenant-tools.md and the
behavioral invariants from spec.md FR-023..FR-029.

2026-05-18 pivot: switch_tenant now mints a new JWT via Frontegg's
tenant-switch refresh endpoint. Tests mock that call and the refresh-token
cache.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from src.auth import frontegg_user_info, refresh_token_cache, tenant_state
from src.auth.context import (
    SessionCredential,
    _reset_for_tests as _reset_credential,
    set_session_credential,
)
from src.auth.frontegg_user_info import Tenant


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    tenant_state._reset_for_tests()
    frontegg_user_info._reset_for_tests()
    refresh_token_cache._reset_for_tests()
    _reset_credential()
    monkeypatch.setenv("FRONTEGG_DOMAIN", "test.frontegg.example")
    yield
    tenant_state._reset_for_tests()
    frontegg_user_info._reset_for_tests()
    refresh_token_cache._reset_for_tests()
    _reset_credential()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_oauth_credential(
    *,
    org_id: str = "t-default",
    allowed: tuple[str, ...] = (),
    subject: str = "user-42",
) -> SessionCredential:
    return SessionCredential(
        kind="oauth",
        api_key="header.payload.signature",
        org_id=org_id,
        subject=subject,
        allowed_tenants=allowed,
    )


def _make_api_key_credential() -> SessionCredential:
    return SessionCredential(
        kind="api_key",
        api_key="okareo-fixture-key",
        org_id="t-api-org",
    )


def _capture_tools():
    """Register tools against a stub MCP and return a `{name: fn}` map."""
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


def _set_session_and_id(credential: SessionCredential, session_id: str | None):
    set_session_credential(credential)
    return patch(
        "src.tools.tenants._current_session_id", return_value=session_id,
    )


def _patch_user_info(tenants: list[Tenant]):
    return patch(
        "src.tools.tenants.get_user_tenants",
        AsyncMock(return_value=tenants),
    )


def _patch_refresh_with_tenant(*, new_access: str = "jwt-NEW-TENANT", new_refresh: str = "rt-NEW"):
    """Patch the Frontegg refresh call to return a successful new token pair."""
    return patch(
        "src.tools.tenants.refresh_with_tenant",
        AsyncMock(return_value={
            "access_token": new_access,
            "refresh_token": new_refresh,
            "expires_in": 1800,
        }),
    )


# ---------------------------------------------------------------------------
# list_tenants
# ---------------------------------------------------------------------------


class TestListTenantsHappyPath:
    def test_returns_all_tenants_marked_with_current(self):
        cred = _make_oauth_credential(org_id="t-1")
        tools = _capture_tools()
        fixture = [Tenant(id="t-1", name="Acme"), Tenant(id="t-2", name="Globex")]

        async def run():
            with _set_session_and_id(cred, "sess-A"), _patch_user_info(fixture):
                result = await tools["list_tenants"]()
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["active_tenant_id"] == "t-1"
        assert payload["active_tenant_source"] == "jwt_default"
        ids = [t["id"] for t in payload["tenants"]]
        assert ids == ["t-1", "t-2"]
        currents = [t["is_current"] for t in payload["tenants"]]
        assert currents == [True, False]

    def test_override_reflected_in_response(self):
        cred = _make_oauth_credential(org_id="t-1")
        tools = _capture_tools()
        fixture = [Tenant(id="t-1", name="Acme"), Tenant(id="t-2", name="Globex")]

        async def run():
            with _set_session_and_id(cred, "sess-A"), _patch_user_info(fixture):
                tenant_state.set_override("sess-A", "t-2", "jwt-bound-to-t2")
                result = await tools["list_tenants"]()
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["active_tenant_id"] == "t-2"
        assert payload["active_tenant_source"] == "override"
        currents = {t["id"]: t["is_current"] for t in payload["tenants"]}
        assert currents == {"t-1": False, "t-2": True}


class TestListTenantsErrors:
    def test_bearer_api_key_session_rejects(self):
        cred = _make_api_key_credential()
        tools = _capture_tools()

        async def run():
            with _set_session_and_id(cred, "sess-A"):
                result = await tools["list_tenants"]()
                return json.loads(result)

        payload = asyncio.run(run())
        assert "error" in payload
        assert payload["error"]["code"] == "tenant_selection_requires_oauth"
        assert "docs_url" in payload["error"].get("data", {})

    def test_frontegg_lookup_failure_returns_tool_error(self):
        cred = _make_oauth_credential()
        tools = _capture_tools()

        async def run():
            with _set_session_and_id(cred, "sess-A"), patch(
                "src.tools.tenants.get_user_tenants",
                AsyncMock(side_effect=frontegg_user_info.TenantLookupFailed("upstream 503")),
            ):
                result = await tools["list_tenants"]()
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["error"]["code"] == "tenant_lookup_failed"
        assert payload["error"]["data"].get("retriable") is True


# ---------------------------------------------------------------------------
# switch_tenant
# ---------------------------------------------------------------------------


class TestSwitchTenantHappyPath:
    def test_switch_via_jwt_claim_mints_new_jwt(self):
        cred = _make_oauth_credential(org_id="t-1", allowed=("t-1", "t-2"))
        tools = _capture_tools()
        refresh_token_cache.set_token("user-42", "rt-initial")

        async def run():
            with _set_session_and_id(cred, "sess-A"), \
                 _patch_user_info([Tenant(id="t-2", name="Globex")]), \
                 _patch_refresh_with_tenant(
                     new_access="jwt-T2-bound", new_refresh="rt-rotated",
                 ):
                result = await tools["switch_tenant"](tenant_id="t-2")
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["active_tenant_id"] == "t-2"
        assert payload["active_tenant_name"] == "Globex"
        assert payload["previous_tenant_id"] == "t-1"
        assert payload["resume_hint"]
        # The override map now holds the new tenant-scoped JWT.
        rec = tenant_state.get_override("sess-A")
        assert rec is not None
        assert rec.tenant_id == "t-2"
        assert rec.access_token == "jwt-T2-bound"
        # Refresh-token cache rotated to the newly-minted refresh token.
        assert refresh_token_cache.get_token("user-42") == "rt-rotated"

    def test_switch_falls_back_to_user_info_when_jwt_claim_absent(self):
        cred = _make_oauth_credential(org_id="t-1", allowed=())
        tools = _capture_tools()
        refresh_token_cache.set_token("user-42", "rt-initial")
        fixture = [Tenant(id="t-1", name="Acme"), Tenant(id="t-2", name="Globex")]

        async def run():
            with _set_session_and_id(cred, "sess-A"), \
                 _patch_user_info(fixture), \
                 _patch_refresh_with_tenant(new_access="jwt-T2-bound"):
                result = await tools["switch_tenant"](tenant_id="t-2")
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["active_tenant_id"] == "t-2"
        assert payload["active_tenant_name"] == "Globex"

    def test_single_tenant_switch_succeeds(self):
        """Switching to the same tenant the user is already on still succeeds
        (mints a new JWT for the same tenant — Frontegg-side is idempotent)."""
        cred = _make_oauth_credential(org_id="t-1", allowed=("t-1",))
        tools = _capture_tools()
        refresh_token_cache.set_token("user-42", "rt-initial")

        async def run():
            with _set_session_and_id(cred, "sess-A"), \
                 _patch_user_info([Tenant(id="t-1", name="Acme")]), \
                 _patch_refresh_with_tenant(new_access="jwt-T1-fresh"):
                result = await tools["switch_tenant"](tenant_id="t-1")
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["active_tenant_id"] == "t-1"
        assert payload["previous_tenant_id"] == "t-1"


def _fake_jwt(claims: dict) -> str:
    """Encode a JWT with the given claims (no signature verification needed)."""
    import jwt
    return jwt.encode(claims, "test-secret-32-bytes-or-more-please", algorithm="HS256")


class TestSwitchTenantVerifiesActualTenantInJwt:
    """Frontegg's /oauth/token may return 200 but silently ignore the
    `tenantId` extension parameter. If we trusted that response and cached
    a same-tenant JWT under the requested-tenant id, downstream calls would
    return the wrong data. The tool MUST verify the JWT's tenantId claim
    matches the requested tenant_id before storing the override."""

    def test_mismatch_returns_tenant_switch_not_supported(self):
        cred = _make_oauth_credential(org_id="t-1", allowed=("t-1", "t-2"))
        tools = _capture_tools()
        refresh_token_cache.set_token("user-42", "rt-initial")

        # Frontegg returns a "new" JWT that's actually still for t-1.
        same_tenant_jwt = _fake_jwt({"sub": "user-42", "tenantId": "t-1"})

        async def run():
            with _set_session_and_id(cred, "sess-A"), \
                 _patch_user_info([Tenant(id="t-2", name="Globex")]), \
                 patch(
                     "src.tools.tenants.refresh_with_tenant",
                     AsyncMock(return_value={
                         "access_token": same_tenant_jwt,
                         "refresh_token": "rotated",
                         "expires_in": 1800,
                     }),
                 ):
                result = await tools["switch_tenant"](tenant_id="t-2")
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["error"]["code"] == "tenant_switch_not_supported"
        assert payload["error"]["data"]["requested_tenant_id"] == "t-2"
        assert payload["error"]["data"]["actual_tenant_id"] == "t-1"
        # Override was NOT written.
        assert tenant_state.get_override("sess-A") is None

    def test_match_proceeds_to_store_override(self):
        cred = _make_oauth_credential(org_id="t-1", allowed=("t-1", "t-2"))
        tools = _capture_tools()
        refresh_token_cache.set_token("user-42", "rt-initial")

        # Frontegg returns a new JWT for the requested tenant — the happy path.
        new_jwt = _fake_jwt({"sub": "user-42", "tenantId": "t-2"})

        async def run():
            with _set_session_and_id(cred, "sess-A"), \
                 _patch_user_info([Tenant(id="t-2", name="Globex")]), \
                 patch(
                     "src.tools.tenants.refresh_with_tenant",
                     AsyncMock(return_value={
                         "access_token": new_jwt,
                         "refresh_token": "rotated",
                         "expires_in": 1800,
                     }),
                 ):
                result = await tools["switch_tenant"](tenant_id="t-2")
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["active_tenant_id"] == "t-2"
        rec = tenant_state.get_override("sess-A")
        assert rec is not None
        assert rec.tenant_id == "t-2"
        assert rec.access_token == new_jwt

    def test_undecodable_jwt_does_not_block_switch(self):
        """If we can't decode the JWT (corrupt, unusual encoding), skip the
        verification rather than blocking the switch. The downstream call
        will reveal the issue if it's a real problem."""
        cred = _make_oauth_credential(org_id="t-1", allowed=("t-1", "t-2"))
        tools = _capture_tools()
        refresh_token_cache.set_token("user-42", "rt-initial")

        async def run():
            with _set_session_and_id(cred, "sess-A"), \
                 _patch_user_info([Tenant(id="t-2", name="Globex")]), \
                 patch(
                     "src.tools.tenants.refresh_with_tenant",
                     AsyncMock(return_value={
                         "access_token": "not-a-real-jwt",
                         "refresh_token": "rotated",
                         "expires_in": 1800,
                     }),
                 ):
                result = await tools["switch_tenant"](tenant_id="t-2")
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["active_tenant_id"] == "t-2"


class TestSwitchTenantErrors:
    def test_unauthorized_tenant_id_does_not_echo_id(self):
        cred = _make_oauth_credential(org_id="t-1", allowed=("t-1",))
        tools = _capture_tools()
        refresh_token_cache.set_token("user-42", "rt-initial")
        bogus = "definitely-not-my-tenant-007"

        async def run():
            with _set_session_and_id(cred, "sess-A"):
                tenant_state.set_override("sess-A", "t-1", "jwt-original-1")
                result = await tools["switch_tenant"](tenant_id=bogus)
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["error"]["code"] == "tenant_not_authorized"
        # The rejected id is NOT in the serialized response.
        assert bogus not in json.dumps(payload)
        # Existing override is unchanged.
        rec = tenant_state.get_override("sess-A")
        assert rec.tenant_id == "t-1"
        assert rec.access_token == "jwt-original-1"

    def test_bearer_api_key_session_rejects(self):
        cred = _make_api_key_credential()
        tools = _capture_tools()

        async def run():
            with _set_session_and_id(cred, "sess-A"):
                result = await tools["switch_tenant"](tenant_id="t-99")
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["error"]["code"] == "tenant_selection_requires_oauth"
        # Override map is untouched.
        assert tenant_state.get_override("sess-A") is None

    def test_missing_refresh_token_cache_returns_tool_error(self):
        """If the refresh-token cache has no entry for this user (e.g. they
        signed in before the cache existed, or the entry was evicted), the
        switch tool returns a clear error telling them to re-sign-in."""
        cred = _make_oauth_credential(org_id="t-1", allowed=("t-1", "t-2"))
        tools = _capture_tools()
        # NO refresh_token_cache.set_token() call.

        async def run():
            with _set_session_and_id(cred, "sess-A"), \
                 _patch_user_info([Tenant(id="t-2", name="Globex")]):
                result = await tools["switch_tenant"](tenant_id="t-2")
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["error"]["code"] == "tenant_switch_unavailable"
        assert "docs_url" in payload["error"].get("data", {})

    def test_frontegg_auth_error_evicts_cache(self):
        """If Frontegg returns 401 on the tenant-switch refresh, drop the
        stale refresh token from the cache so the next attempt doesn't
        silently retry with a known-bad token."""
        cred = _make_oauth_credential(org_id="t-1", allowed=("t-1", "t-2"))
        tools = _capture_tools()
        refresh_token_cache.set_token("user-42", "rt-stale")

        async def run():
            with _set_session_and_id(cred, "sess-A"), \
                 _patch_user_info([Tenant(id="t-2", name="Globex")]), \
                 patch(
                     "src.tools.tenants.refresh_with_tenant",
                     AsyncMock(
                         side_effect=frontegg_user_info.TenantSwitchAuthError(
                             "session revoked"
                         )
                     ),
                 ):
                result = await tools["switch_tenant"](tenant_id="t-2")
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["error"]["code"] == "tenant_switch_unavailable"
        assert refresh_token_cache.get_token("user-42") is None

    def test_frontegg_generic_failure_preserves_state(self):
        cred = _make_oauth_credential(org_id="t-1", allowed=("t-1", "t-2"))
        tools = _capture_tools()
        refresh_token_cache.set_token("user-42", "rt-initial")
        tenant_state.set_override("sess-A", "t-1", "jwt-original-1")

        async def run():
            with _set_session_and_id(cred, "sess-A"), \
                 _patch_user_info([Tenant(id="t-2", name="Globex")]), \
                 patch(
                     "src.tools.tenants.refresh_with_tenant",
                     AsyncMock(side_effect=frontegg_user_info.TenantSwitchFailed("upstream 5xx")),
                 ):
                result = await tools["switch_tenant"](tenant_id="t-2")
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["error"]["code"] == "tenant_switch_failed"
        # State preserved.
        rec = tenant_state.get_override("sess-A")
        assert rec.tenant_id == "t-1"
        assert rec.access_token == "jwt-original-1"
        # Refresh token NOT evicted (this isn't an auth error, may be transient).
        assert refresh_token_cache.get_token("user-42") == "rt-initial"
