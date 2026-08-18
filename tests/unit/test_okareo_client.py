"""Unit tests for src/okareo_client.py — get_okareo_client().

Since feature 030, tenant selection happens at sign-in and there is no
per-session override: the credential the request presents is already scoped to
the authorized organization, so ``get_okareo_client`` always uses
``credential.api_key`` as the Okareo SDK's ``api_key``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.auth.context import (
    SessionCredential,
    _reset_for_tests as _reset_credential,
    set_session_credential,
)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    from src.okareo_client import _reset_for_tests as _reset_project_cache

    _reset_credential()
    _reset_project_cache()
    monkeypatch.delenv("OKAREO_API_KEY", raising=False)
    yield
    _reset_credential()
    _reset_project_cache()


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


class TestProjectIdCacheKeying:
    def test_http_mode_keys_on_org_id(self):
        import hashlib

        from src.okareo_client import _project_cache_scope, _project_id_cache

        cred = SessionCredential(
            kind="oauth", api_key="jwt-A", org_id="org-A", subject="u",
        )
        set_session_credential(cred)
        okareo = MagicMock()
        okareo.api_key = "jwt-A"
        assert _project_cache_scope(okareo) == "org-A"
        # Raw API key must never appear in any cache key.
        assert "jwt-A" not in str(_project_id_cache.keys())
        assert hashlib.sha256(b"jwt-A").hexdigest()[:16] != "org-A"

    def test_stdio_keys_on_api_key_hash(self):
        import hashlib

        from src.okareo_client import _project_cache_scope

        monkeypatch_key = "stdio-secret-key"
        okareo = MagicMock()
        okareo.api_key = monkeypatch_key
        expected = hashlib.sha256(monkeypatch_key.encode()).hexdigest()[:16]
        assert _project_cache_scope(okareo) == expected
        assert monkeypatch_key not in expected

    def test_differing_base_url_never_share_entry(self, monkeypatch):
        from src import okareo_client as mod

        project = MagicMock()
        project.name = "Global"
        project.id = "proj-prod"

        okareo = MagicMock()
        okareo.api_key = "k"
        okareo.get_projects.return_value = [project]

        monkeypatch.setenv("OKAREO_BASE_URL", "https://api.okareo.com/")
        assert mod.resolve_project_id(okareo) == "proj-prod"
        assert okareo.get_projects.call_count == 1

        project_stg = MagicMock()
        project_stg.name = "Global"
        project_stg.id = "proj-stg"
        okareo.get_projects.return_value = [project_stg]
        monkeypatch.setenv("OKAREO_BASE_URL", "https://staging.okareo.com/")
        assert mod.resolve_project_id(okareo) == "proj-stg"
        assert okareo.get_projects.call_count == 2


class TestProjectIdCacheCorrectness:
    def test_cross_org_independent_after_gc(self):
        """Force the id()-reuse condition the original defect depended on."""
        import gc

        from src import okareo_client as mod

        def _resolve(org_id: str, project_id: str) -> str:
            cred = SessionCredential(
                kind="oauth", api_key=f"jwt-{org_id}", org_id=org_id, subject="u",
            )
            set_session_credential(cred)
            okareo = MagicMock()
            okareo.api_key = f"jwt-{org_id}"
            project = MagicMock()
            project.name = "Global"
            project.id = project_id
            okareo.get_projects.return_value = [project]
            return mod.resolve_project_id(okareo)

        assert _resolve("org-A", "proj-A") == "proj-A"
        # Drop references and collect so CPython may reuse object ids.
        _reset_credential()
        gc.collect()
        assert _resolve("org-B", "proj-B") == "proj-B"

    def test_cache_reuse_and_overflow_clear(self, monkeypatch):
        from src import okareo_client as mod

        cred = SessionCredential(
            kind="oauth", api_key="jwt", org_id="org-1", subject="u",
        )
        set_session_credential(cred)
        okareo = MagicMock()
        okareo.api_key = "jwt"
        project = MagicMock()
        project.name = "Global"
        project.id = "proj-1"
        okareo.get_projects.return_value = [project]

        for _ in range(5):
            assert mod.resolve_project_id(okareo) == "proj-1"
        assert okareo.get_projects.call_count == 1

        # Fill to the bound, then one more clears wholesale.
        for i in range(512):
            other = SessionCredential(
                kind="oauth",
                api_key=f"jwt-{i}",
                org_id=f"org-fill-{i}",
                subject="u",
            )
            set_session_credential(other)
            okareo_i = MagicMock()
            okareo_i.api_key = f"jwt-{i}"
            p = MagicMock()
            p.name = "Global"
            p.id = f"proj-{i}"
            okareo_i.get_projects.return_value = [p]
            mod.resolve_project_id(okareo_i)

        # 513th distinct org after the original — clearing happens on insert
        # once the bound is hit, so the cache size stays ≤ bound.
        assert len(mod._project_id_cache) <= mod._PROJECT_ID_CACHE_BOUND
