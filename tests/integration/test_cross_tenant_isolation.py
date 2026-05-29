"""Integration-style tests for cross-tenant isolation (T024 / FR-005 / FR-008).

Per-request scoping uses a ContextVar; two concurrent asyncio tasks each set
their own ``SessionCredential`` and then call ``get_okareo_client()`` —
neither should see the other's credential.

These tests do NOT stand up a full ASGI server; they exercise the in-process
boundary (verifier → ContextVar → get_okareo_client → Okareo) which is the
piece per-request isolation actually depends on.

2026-05-18 pivot: the tenant override is a tenant-scoped JWT, not a header.
``get_okareo_client`` substitutes that JWT for the credential's JWT when
constructing the Okareo SDK client; no custom-headers path is exercised.
"""

from __future__ import annotations

import asyncio
import contextvars
from unittest.mock import MagicMock, patch

import pytest

from src.auth import tenant_state
from src.auth.context import (
    SessionCredential,
    _reset_for_tests as _reset_credential,
    set_session_credential,
)


@pytest.fixture(autouse=True)
def _isolate():
    _reset_credential()
    tenant_state._reset_for_tests()
    yield
    _reset_credential()
    tenant_state._reset_for_tests()


class TestConcurrentSessionsDifferentOrgs:
    """Two concurrent asyncio tasks, two different credentials. Each task's
    `get_okareo_client()` MUST be constructed with that task's API key."""

    def test_two_orgs_no_cross_session_leakage(self, monkeypatch):
        monkeypatch.delenv("OKAREO_API_KEY", raising=False)
        captured_keys: list[str] = []

        def _fake_okareo(api_key, base_path):
            captured_keys.append(api_key)
            return MagicMock(name=f"okareo:{api_key}")

        async def task(api_key: str, org_id: str, barrier: asyncio.Event):
            ctx = contextvars.copy_context()

            def _inner():
                cred = SessionCredential(
                    kind="oauth", api_key=api_key, org_id=org_id, subject=org_id,
                )
                set_session_credential(cred)

            ctx.run(_inner)
            await barrier.wait()
            from src.okareo_client import get_okareo_client

            def _read():
                return get_okareo_client()

            return ctx.run(_read)

        async def run():
            barrier = asyncio.Event()
            with patch("src.okareo_client.Okareo", side_effect=_fake_okareo):
                t_a = asyncio.create_task(task("key-ALPHA", "org-A", barrier))
                t_b = asyncio.create_task(task("key-BRAVO", "org-B", barrier))
                await asyncio.sleep(0)
                barrier.set()
                await asyncio.gather(t_a, t_b)

        asyncio.run(run())

        assert sorted(captured_keys) == ["key-ALPHA", "key-BRAVO"]


class TestConcurrentSessionsSameOrgIndependentTenantOverride:
    """Same user, two concurrent MCP sessions, two different `switch_tenant`
    overrides — each session's Okareo client uses its OWN tenant-scoped JWT
    (US4 acceptance scenario 4 / SC-011)."""

    def test_two_sessions_different_override_jwts(self, monkeypatch):
        monkeypatch.delenv("OKAREO_API_KEY", raising=False)
        captured_api_keys: list[str] = []

        def _fake_okareo(api_key, base_path):
            captured_api_keys.append(api_key)
            return MagicMock(name=f"okareo:{api_key}")

        async def task(session_id: str, override_jwt: str, barrier: asyncio.Event):
            ctx = contextvars.copy_context()

            def _setup():
                set_session_credential(
                    SessionCredential(
                        kind="oauth",
                        api_key="shared-default-jwt",
                        org_id="org-shared",
                        subject="user-1",
                    )
                )

            ctx.run(_setup)
            # Each session has its OWN override JWT (minted by Frontegg for
            # its chosen tenant).
            tenant_state.set_override(session_id, f"tenant-for-{session_id}", override_jwt)
            await barrier.wait()
            from src.okareo_client import get_okareo_client

            def _read():
                with patch(
                    "src.okareo_client._current_session_id",
                    return_value=session_id,
                ):
                    return get_okareo_client()

            return ctx.run(_read)

        async def run():
            barrier = asyncio.Event()
            with patch("src.okareo_client.Okareo", side_effect=_fake_okareo):
                t_a = asyncio.create_task(task("sess-A", "jwt-bound-to-X", barrier))
                t_b = asyncio.create_task(task("sess-B", "jwt-bound-to-Y", barrier))
                await asyncio.sleep(0)
                barrier.set()
                await asyncio.gather(t_a, t_b)

        asyncio.run(run())

        # Each task constructed an Okareo client with its OWN override JWT
        # — never the shared default JWT, never the other session's JWT.
        assert sorted(captured_api_keys) == ["jwt-bound-to-X", "jwt-bound-to-Y"]
