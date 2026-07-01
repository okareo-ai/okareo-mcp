"""Unit tests for src/tools/tenants.py — list_tenants + switch_tenant MCP tools.

Feature 030: organization selection happens at sign-in. ``switch_tenant`` no
longer mints a tenant-scoped token — it returns guidance to reconnect/
re-authenticate. ``list_tenants`` remains read-only and reports the active
organization from the presented JWT.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from src.auth import frontegg_user_info
from src.auth.context import (
    SessionCredential,
    _reset_for_tests as _reset_credential,
    set_session_credential,
)
from src.auth.frontegg_user_info import Tenant


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    frontegg_user_info._reset_for_tests()
    _reset_credential()
    monkeypatch.setenv("FRONTEGG_DOMAIN", "test.frontegg.example")
    yield
    frontegg_user_info._reset_for_tests()
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

    def test_active_tenant_follows_the_presented_credential(self):
        """The active org is whatever the JWT is scoped to (org_id) — there is
        no per-session override anymore (feature 030)."""
        cred = _make_oauth_credential(org_id="t-2")
        tools = _capture_tools()
        fixture = [Tenant(id="t-1", name="Acme"), Tenant(id="t-2", name="Globex")]

        async def run():
            with _set_session_and_id(cred, "sess-A"), _patch_user_info(fixture):
                result = await tools["list_tenants"]()
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["active_tenant_id"] == "t-2"
        assert payload["active_tenant_source"] == "jwt_default"
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
# switch_tenant (feature 030 — guidance only, no mint)
# ---------------------------------------------------------------------------


class TestSwitchTenantGuidance:
    def test_returns_reauthenticate_guidance_without_changing_org(self):
        cred = _make_oauth_credential(org_id="t-1", allowed=("t-1", "t-2"))
        tools = _capture_tools()

        async def run():
            with _set_session_and_id(cred, "sess-A"):
                result = await tools["switch_tenant"](tenant_id="t-2")
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["action"] == "reauthenticate_to_change_tenant"
        assert payload["current_tenant_id"] == "t-1"
        assert "docs_url" in payload
        # No error, no active-tenant change reported.
        assert "error" not in payload
        assert "active_tenant_id" not in payload

    def test_does_not_call_frontegg_or_require_a_cache(self):
        """The guidance path must not depend on any server-side token
        machinery — it should not reach out to Frontegg at all."""
        cred = _make_oauth_credential(org_id="t-1")
        tools = _capture_tools()

        async def run():
            with _set_session_and_id(cred, "sess-A"), patch(
                "src.tools.tenants.get_user_tenants",
                AsyncMock(side_effect=AssertionError("must not be called")),
            ):
                result = await tools["switch_tenant"](tenant_id="t-9")
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["action"] == "reauthenticate_to_change_tenant"

    def test_bearer_api_key_session_rejects(self):
        cred = _make_api_key_credential()
        tools = _capture_tools()

        async def run():
            with _set_session_and_id(cred, "sess-A"):
                result = await tools["switch_tenant"](tenant_id="t-99")
                return json.loads(result)

        payload = asyncio.run(run())
        assert payload["error"]["code"] == "tenant_selection_requires_oauth"
        assert "docs_url" in payload["error"].get("data", {})
