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

        from src.okareo_client import _project_cache, _project_cache_scope

        cred = SessionCredential(
            kind="oauth", api_key="jwt-A", org_id="org-A", subject="u",
        )
        set_session_credential(cred)
        okareo = MagicMock()
        okareo.api_key = "jwt-A"
        assert _project_cache_scope(okareo) == "org-A"
        # Raw API key must never appear in any cache key.
        assert "jwt-A" not in str(_project_cache.keys())
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
        assert mod.resolve_project(okareo).id == "proj-prod"
        assert okareo.get_projects.call_count == 1

        project_stg = MagicMock()
        project_stg.name = "Global"
        project_stg.id = "proj-stg"
        okareo.get_projects.return_value = [project_stg]
        monkeypatch.setenv("OKAREO_BASE_URL", "https://staging.okareo.com/")
        assert mod.resolve_project(okareo).id == "proj-stg"
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
            return mod.resolve_project(okareo).id

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
            assert mod.resolve_project(okareo).id == "proj-1"
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
            mod.resolve_project(okareo_i).id

        # 513th distinct org after the original — clearing happens on insert
        # once the bound is hit, so the cache size stays ≤ bound.
        assert len(mod._project_cache) <= mod._PROJECT_CACHE_BOUND


# ---------------------------------------------------------------------------
# 036-project-scoping — resolution precedence, cache, pin, error taxonomy
# ---------------------------------------------------------------------------

GLOBAL_ID = "11111111-1111-4111-8111-111111111111"
BILLING_ID = "22222222-2222-4222-8222-222222222222"
SUPPORT_ID = "33333333-3333-4333-8333-333333333333"


def _proj(pid: str, name: str):
    p = MagicMock()
    p.id = pid
    p.name = name
    return p


def _client(projects, api_key="key-1"):
    okareo = MagicMock()
    okareo.api_key = api_key
    okareo.get_projects.return_value = list(projects)
    return okareo


ONLY_GLOBAL = [_proj(GLOBAL_ID, "Global")]
MULTI = [
    _proj(GLOBAL_ID, "Global"),
    _proj(BILLING_ID, "Billing Agent"),
    _proj(SUPPORT_ID, "Support Bot"),
]


class TestResolveProjectPrecedence:
    """FR-002: explicit → pin → single-project default → raise."""

    def test_explicit_name_resolves(self):
        from src.okareo_client import resolve_project

        r = resolve_project(_client(MULTI), "Billing Agent")
        assert (r.id, r.name, r.basis) == (BILLING_ID, "Billing Agent", "explicit")

    def test_explicit_id_resolves(self):
        from src.okareo_client import resolve_project

        r = resolve_project(_client(MULTI), BILLING_ID)
        assert r.id == BILLING_ID and r.basis == "explicit"

    def test_name_match_is_case_and_whitespace_insensitive(self):
        from src.okareo_client import resolve_project

        r = resolve_project(_client(MULTI), "  bILLing aGENT  ")
        assert r.id == BILLING_ID

    def test_explicit_beats_pin(self, monkeypatch):
        from src.okareo_client import resolve_project

        monkeypatch.setenv("OKAREO_PROJECT", "Support Bot")
        r = resolve_project(_client(MULTI), "Billing Agent")
        assert r.id == BILLING_ID and r.basis == "explicit"

    def test_pin_used_when_no_argument(self, monkeypatch):
        from src.okareo_client import resolve_project

        monkeypatch.setenv("OKAREO_PROJECT", "Support Bot")
        r = resolve_project(_client(MULTI))
        assert (r.id, r.basis) == (SUPPORT_ID, "pin")

    def test_pin_beats_default_even_with_one_project(self, monkeypatch):
        from src.okareo_client import resolve_project

        monkeypatch.setenv("OKAREO_PROJECT", "Global")
        r = resolve_project(_client(ONLY_GLOBAL))
        assert r.basis == "pin"

    def test_single_project_org_resolves_without_prompt(self):
        """US2 / FR-032: the Global-only no-op."""
        from src.okareo_client import resolve_project

        okareo = _client(ONLY_GLOBAL)
        r = resolve_project(okareo)
        assert (r.id, r.name, r.basis) == (GLOBAL_ID, "Global", "default")
        assert okareo.get_projects.call_count == 1

    def test_assigns_project_id_on_client(self):
        """research R9: defense in depth for un-parameterized SDK calls."""
        from src.okareo_client import resolve_project

        okareo = _client(MULTI)
        resolve_project(okareo, "Billing Agent")
        assert okareo.project_id == BILLING_ID


class TestResolveProjectFailures:
    """FR-003/FR-006/FR-019: never fall back, never guess."""

    def test_multi_project_without_selection_raises_not_selected(self):
        from src.error_handling import ProjectNotSelected
        from src.okareo_client import resolve_project

        with pytest.raises(ProjectNotSelected) as exc:
            resolve_project(_client(MULTI))
        names = {p["name"] for p in exc.value.projects}
        assert names == {"Global", "Billing Agent", "Support Bot"}

    def test_multi_project_never_falls_back_to_global(self):
        """SC-005: the silent mis-scoping this feature exists to prevent."""
        from src.error_handling import ProjectNotSelected
        from src.okareo_client import resolve_project

        with pytest.raises(ProjectNotSelected):
            resolve_project(_client(MULTI))

    def test_unknown_name_raises_not_found_and_does_not_fall_back(self):
        from src.error_handling import ProjectNotFound
        from src.okareo_client import resolve_project

        with pytest.raises(ProjectNotFound) as exc:
            resolve_project(_client(MULTI), "Nonexistent")
        assert "unchanged" in str(exc.value)
        assert exc.value.projects

    def test_unknown_name_in_single_project_org_still_raises(self):
        """FR-006: a bad explicit name never degrades to the default."""
        from src.error_handling import ProjectNotFound
        from src.okareo_client import resolve_project

        with pytest.raises(ProjectNotFound):
            resolve_project(_client(ONLY_GLOBAL), "Nonexistent")

    def test_uuid_shaped_value_is_never_retried_as_a_name(self):
        """FR-004: a wrong id fails rather than quietly matching a name."""
        from src.error_handling import ProjectNotFound
        from src.okareo_client import resolve_project

        with pytest.raises(ProjectNotFound):
            resolve_project(_client(MULTI), "44444444-4444-4444-8444-444444444444")

    def test_bad_pin_raises_misconfigured_not_not_found(self, monkeypatch):
        """US4 sc. 3: the remedy differs, so the code must differ."""
        from src.error_handling import ProjectMisconfigured
        from src.okareo_client import resolve_project

        monkeypatch.setenv("OKAREO_PROJECT", "Ghost Project")
        with pytest.raises(ProjectMisconfigured) as exc:
            resolve_project(_client(MULTI))
        assert "connection configuration" in str(exc.value)
        assert exc.value.data["pin"] == "Ghost Project"

    def test_bad_pin_never_falls_back_to_global(self, monkeypatch):
        from src.error_handling import ProjectMisconfigured
        from src.okareo_client import resolve_project

        monkeypatch.setenv("OKAREO_PROJECT", "Ghost Project")
        with pytest.raises(ProjectMisconfigured):
            resolve_project(_client(ONLY_GLOBAL))


class TestProjectListCache:
    """research R4: TTL is the only freshness mechanism."""

    def test_repeated_resolution_uses_one_fetch(self):
        from src.okareo_client import resolve_project

        okareo = _client(ONLY_GLOBAL)
        for _ in range(5):
            resolve_project(okareo)
        assert okareo.get_projects.call_count == 1

    def test_ttl_expiry_refetches(self, monkeypatch):
        import src.okareo_client as mod

        okareo = _client(ONLY_GLOBAL)
        mod.resolve_project(okareo)
        assert okareo.get_projects.call_count == 1

        # A project created in the web app becomes visible after the TTL.
        okareo.get_projects.return_value = MULTI
        real = mod.time.monotonic
        monkeypatch.setattr(
            mod.time, "monotonic", lambda: real() + mod._PROJECT_CACHE_TTL_SECONDS + 1
        )
        with pytest.raises(Exception):
            mod.resolve_project(okareo)  # now ambiguous — 3 projects
        assert okareo.get_projects.call_count == 2

    def test_cache_key_has_no_project_derived_component(self):
        """FR-036: attribution must stay stable for multi-project accounts."""
        import src.okareo_client as mod

        okareo = _client(MULTI)
        mod.resolve_project(okareo, "Billing Agent")
        key = next(iter(mod._project_cache))
        assert BILLING_ID not in str(key)
        assert "Billing" not in str(key)
        assert okareo.api_key not in str(key)

    def test_bound_clears_wholesale(self):
        import src.okareo_client as mod

        for i in range(mod._PROJECT_CACHE_BOUND + 2):
            cred = SessionCredential(
                kind="oauth", api_key=f"k{i}", org_id=f"org-{i}", subject="u",
            )
            set_session_credential(cred)
            mod.resolve_project(_client(ONLY_GLOBAL, api_key=f"k{i}"))
        assert len(mod._project_cache) <= mod._PROJECT_CACHE_BOUND


class TestConnectionPin:
    """research R3: the pin must travel per connection, per transport."""

    def test_stdio_reads_env_var(self, monkeypatch):
        from src.okareo_client import _read_connection_pin

        monkeypatch.setenv("OKAREO_PROJECT", "Billing Agent")
        monkeypatch.delenv("TRANSPORT", raising=False)
        assert _read_connection_pin() == "Billing Agent"

    def test_env_var_ignored_in_http_mode(self, monkeypatch):
        """The hazard: one hosted process serves every tenant."""
        from src.okareo_client import _read_connection_pin

        monkeypatch.setenv("OKAREO_PROJECT", "Billing Agent")
        monkeypatch.setenv("TRANSPORT", "streamable-http")
        assert _read_connection_pin() is None

    def test_http_query_param_wins_over_header(self, monkeypatch):
        import src.okareo_client as mod

        monkeypatch.setenv("TRANSPORT", "streamable-http")
        request = MagicMock()
        request.query_params = {"project": "From Query"}
        request.headers = {"X-Okareo-Project": "From Header"}
        ctx = MagicMock()
        ctx.request_context.request = request
        with patch("mcp.server.lowlevel.server.request_ctx") as rc:
            rc.get.return_value = ctx
            assert mod._read_connection_pin() == "From Query"

    def test_http_falls_back_to_header(self, monkeypatch):
        import src.okareo_client as mod

        monkeypatch.setenv("TRANSPORT", "streamable-http")
        request = MagicMock()
        request.query_params = {}
        request.headers = {"X-Okareo-Project": "From Header"}
        ctx = MagicMock()
        ctx.request_context.request = request
        with patch("mcp.server.lowlevel.server.request_ctx") as rc:
            rc.get.return_value = ctx
            assert mod._read_connection_pin() == "From Header"

    def test_missing_request_context_degrades_to_none(self, monkeypatch):
        import src.okareo_client as mod

        monkeypatch.setenv("TRANSPORT", "streamable-http")
        with patch("mcp.server.lowlevel.server.request_ctx") as rc:
            rc.get.side_effect = LookupError()
            assert mod._read_connection_pin() is None

    def test_blank_pin_is_treated_as_absent(self, monkeypatch):
        from src.okareo_client import _read_connection_pin

        monkeypatch.setenv("OKAREO_PROJECT", "   ")
        monkeypatch.delenv("TRANSPORT", raising=False)
        assert _read_connection_pin() is None


class TestResolveArtifactByName:
    """FR-001a — names resolve inside the acting project, and nowhere else."""

    TARGET_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    def _mut(self, name, pid=BILLING_ID):
        m = MagicMock()
        m.id = self.TARGET_ID
        m.name = name
        m.project_id = pid
        m.models = {"custom_endpoint": {"type": "custom_endpoint"}}
        return m

    def _call(self, name, listed, project_id=BILLING_ID):
        from okareo_api_client.api import default as _default_pkg

        import src.okareo_client as mod

        okareo = _client(MULTI)
        mut_mod = MagicMock()
        mut_mod.sync.return_value = listed
        with patch.object(
            _default_pkg, "get_all_models_under_test_v0_models_under_test_get",
            mut_mod, create=True,
        ):
            result = mod.resolve_artifact_by_name(
                okareo, name, project_id, kind="target"
            )
        return result, mut_mod

    def test_exact_match(self):
        result, _ = self._call("checkout-agent", [self._mut("checkout-agent")])
        assert result.id == self.TARGET_ID

    def test_match_is_case_insensitive(self):
        result, _ = self._call("CHECKOUT-Agent", [self._mut("checkout-agent")])
        assert result.id == self.TARGET_ID

    def test_match_tolerates_surrounding_whitespace(self):
        result, _ = self._call("  checkout-agent  ", [self._mut("checkout-agent")])
        assert result.id == self.TARGET_ID

    def test_lookup_carries_the_project_filter(self):
        """The load-bearing assertion — this is the whole defect."""
        _, mut_mod = self._call("checkout-agent", [self._mut("checkout-agent")])
        assert str(mut_mod.sync.call_args.kwargs["project_id"]) == BILLING_ID

    def test_miss_raises_with_the_project_and_what_is_available(self):
        from src.error_handling import ArtifactNotInProject

        with pytest.raises(ArtifactNotInProject) as exc:
            self._call("nope", [self._mut("checkout-agent"), self._mut("support-bot")])
        assert exc.value.data["available"] == ["checkout-agent", "support-bot"]
        assert "target" in str(exc.value)

    def test_miss_makes_no_further_lookups(self):
        """FR-030: never look outside the acting project, even to explain."""
        from src.error_handling import ArtifactNotInProject

        with pytest.raises(ArtifactNotInProject):
            _, mut_mod = self._call("nope", [self._mut("checkout-agent")])
        # One project-filtered listing, and nothing that enumerates projects.
        from okareo_api_client.api import default as _default_pkg

        import src.okareo_client as mod

        okareo = _client(MULTI)
        mut_mod = MagicMock()
        mut_mod.sync.return_value = [self._mut("checkout-agent")]
        with patch.object(
            _default_pkg, "get_all_models_under_test_v0_models_under_test_get",
            mut_mod, create=True,
        ):
            with pytest.raises(ArtifactNotInProject):
                mod.resolve_artifact_by_name(okareo, "nope", BILLING_ID, kind="target")
        assert mut_mod.sync.call_count == 1
        assert okareo.get_projects.call_count <= 1

    def test_miss_names_no_other_project(self):
        from src.error_handling import ArtifactNotInProject

        with pytest.raises(ArtifactNotInProject) as exc:
            self._call("nope", [self._mut("checkout-agent")])
        blob = str(exc.value) + str(exc.value.data)
        assert "Support Bot" not in blob
        assert "Global" not in blob

    def test_empty_project_is_a_clean_miss(self):
        from src.error_handling import ArtifactNotInProject

        with pytest.raises(ArtifactNotInProject) as exc:
            self._call("anything", [])
        assert exc.value.data["available"] == []
