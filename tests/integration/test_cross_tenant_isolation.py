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


class TestProjectIdIsolationAcrossOrgs:
    """End-to-end: two differently-scoped sessions resolve their own project IDs."""

    def test_two_orgs_resolve_independent_project_ids(self, monkeypatch):
        from src.okareo_client import (
            _reset_for_tests as _reset_project_cache,
            resolve_project,
        )

        _reset_project_cache()
        monkeypatch.delenv("OKAREO_API_KEY", raising=False)

        resolved: dict[str, str] = {}

        def _resolve(org_id: str, project_id: str) -> str:
            set_session_credential(
                SessionCredential(
                    kind="oauth",
                    api_key=f"jwt-{org_id}",
                    org_id=org_id,
                    subject="user-1",
                )
            )
            okareo = MagicMock()
            okareo.api_key = f"jwt-{org_id}"
            project = MagicMock()
            project.name = "Global"
            project.id = project_id
            okareo.get_projects.return_value = [project]
            return resolve_project(okareo).id

        resolved["A"] = _resolve("org-A", "proj-A")
        resolved["B"] = _resolve("org-B", "proj-B")
        assert resolved["A"] == "proj-A"
        assert resolved["B"] == "proj-B"
        assert resolved["A"] != resolved["B"]


class TestProjectPinIsolation:
    """036-project-scoping / research R3.

    In HTTP mode one process serves every tenant, so a pin must travel per
    connection. The failure this guards against is a pin set by one caller
    leaking into another caller's request.
    """

    def test_env_var_pin_is_ignored_in_http_mode(self, monkeypatch):
        """The whole hazard in one assertion: OKAREO_PROJECT is process-wide."""
        from src.okareo_client import _read_connection_pin

        monkeypatch.setenv("TRANSPORT", "streamable-http")
        monkeypatch.setenv("OKAREO_PROJECT", "Tenant-A-Project")
        assert _read_connection_pin() is None

    def test_pin_is_read_per_request_not_cached(self, monkeypatch):
        from unittest.mock import MagicMock, patch

        import src.okareo_client as mod

        monkeypatch.setenv("TRANSPORT", "streamable-http")

        def _pin_for(value):
            request = MagicMock()
            request.query_params = {"project": value} if value else {}
            request.headers = {}
            ctx = MagicMock()
            ctx.request_context.request = request
            with patch("mcp.server.lowlevel.server.request_ctx") as rc:
                rc.get.return_value = ctx
                return mod._read_connection_pin()

        assert _pin_for("Tenant-A-Project") == "Tenant-A-Project"
        assert _pin_for("Tenant-B-Project") == "Tenant-B-Project"
        assert _pin_for(None) is None

    def test_project_cache_is_keyed_per_organization(self, monkeypatch):
        """A pinned project resolved for org A must not be served to org B."""
        from unittest.mock import MagicMock

        from src.okareo_client import _reset_for_tests, resolve_project

        monkeypatch.delenv("TRANSPORT", raising=False)
        monkeypatch.setenv("OKAREO_PROJECT", "Shared Name")
        _reset_for_tests()

        def _resolve(org_id, project_id):
            set_session_credential(
                SessionCredential(
                    kind="oauth",
                    api_key=f"jwt-{org_id}",
                    org_id=org_id,
                    subject="user-1",
                )
            )
            okareo = MagicMock()
            okareo.api_key = f"jwt-{org_id}"
            project = MagicMock()
            project.id = project_id
            project.name = "Shared Name"
            okareo.get_projects.return_value = [project]
            return resolve_project(okareo).id

        a = _resolve("org-A", "11111111-1111-4111-8111-111111111111")
        b = _resolve("org-B", "22222222-2222-4222-8222-222222222222")
        assert a != b, "same project name in two orgs must resolve separately"
        _reset_for_tests()
