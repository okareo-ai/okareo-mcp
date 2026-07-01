"""Integration-style tests for cross-tenant isolation (FR-005 / FR-008).

Per-request scoping uses a ContextVar; two concurrent asyncio tasks each set
their own ``SessionCredential`` and then call ``get_okareo_client()`` —
neither should see the other's credential.

These tests do NOT stand up a full ASGI server; they exercise the in-process
boundary (verifier → ContextVar → get_okareo_client → Okareo) which is the
piece per-request isolation actually depends on.

Feature 030: tenant selection happens at sign-in, so each session simply
presents its own (already tenant-scoped) JWT as ``credential.api_key``. There
is no per-session override; isolation follows directly from the ContextVar.
"""

from __future__ import annotations

import asyncio
import contextvars
from unittest.mock import MagicMock, patch

import pytest

from src.auth.context import (
    SessionCredential,
    _reset_for_tests as _reset_credential,
    set_session_credential,
)


@pytest.fixture(autouse=True)
def _isolate():
    _reset_credential()
    yield
    _reset_credential()


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


class TestConcurrentSessionsSameUserDifferentTenantTokens:
    """Same user, two concurrent MCP sessions authorized to different
    organizations at sign-in — each session's Okareo client uses its OWN
    tenant-scoped JWT (the credential it presents), never the other's."""

    def test_two_sessions_different_tenant_scoped_jwts(self, monkeypatch):
        monkeypatch.delenv("OKAREO_API_KEY", raising=False)
        captured_api_keys: list[str] = []

        def _fake_okareo(api_key, base_path):
            captured_api_keys.append(api_key)
            return MagicMock(name=f"okareo:{api_key}")

        async def task(scoped_jwt: str, tenant_id: str, barrier: asyncio.Event):
            ctx = contextvars.copy_context()

            def _setup():
                # Each session presents its own tenant-scoped JWT (chosen at
                # sign-in) as the credential api_key.
                set_session_credential(
                    SessionCredential(
                        kind="oauth",
                        api_key=scoped_jwt,
                        org_id=tenant_id,
                        subject="user-1",
                    )
                )

            ctx.run(_setup)
            await barrier.wait()
            from src.okareo_client import get_okareo_client

            def _read():
                return get_okareo_client()

            return ctx.run(_read)

        async def run():
            barrier = asyncio.Event()
            with patch("src.okareo_client.Okareo", side_effect=_fake_okareo):
                t_a = asyncio.create_task(task("jwt-bound-to-X", "tenant-X", barrier))
                t_b = asyncio.create_task(task("jwt-bound-to-Y", "tenant-Y", barrier))
                await asyncio.sleep(0)
                barrier.set()
                await asyncio.gather(t_a, t_b)

        asyncio.run(run())

        assert sorted(captured_api_keys) == ["jwt-bound-to-X", "jwt-bound-to-Y"]
