"""Unit tests for src/auth/tenant_state.py — the per-MCP-session active-tenant override.

Covers FR-024 (in-process override keyed by Mcp-Session-Id, never re-mints the
client's JWT) and the dual-eviction design from analysis finding C1 (TTL
primary, hook secondary).

2026-05-18 pivot: the override now stores a TenantOverride record with both
the tenant_id and the new tenant-scoped JWT minted by Frontegg.
"""

from __future__ import annotations

import asyncio

import pytest

from src.auth import tenant_state
from src.auth.tenant_state import TenantOverride


@pytest.fixture(autouse=True)
def _isolate_state():
    """Each test runs against a clean module-level dict."""
    tenant_state._reset_for_tests()
    yield
    tenant_state._reset_for_tests()


def _jwt(tag: str = "X") -> str:
    """A stand-in JWT string for tests — not validated, just stored."""
    return f"jwt.bound-to.{tag}"


class TestRoundtrip:
    def test_set_then_get_returns_record(self):
        tenant_state.set_override("sess-A", "tenant-1", _jwt("1"))
        rec = tenant_state.get_override("sess-A")
        assert isinstance(rec, TenantOverride)
        assert rec.tenant_id == "tenant-1"
        assert rec.access_token == _jwt("1")

    def test_absent_session_returns_none(self):
        assert tenant_state.get_override("never-set") is None

    def test_get_override_tenant_id_convenience(self):
        tenant_state.set_override("sess-A", "tenant-1", _jwt("1"))
        assert tenant_state.get_override_tenant_id("sess-A") == "tenant-1"
        assert tenant_state.get_override_tenant_id("never-set") is None

    def test_overwrite_is_idempotent(self):
        tenant_state.set_override("sess-A", "tenant-1", _jwt("1"))
        tenant_state.set_override("sess-A", "tenant-1", _jwt("1"))
        rec = tenant_state.get_override("sess-A")
        assert rec.tenant_id == "tenant-1"
        assert rec.access_token == _jwt("1")

    def test_overwrite_changes_value(self):
        tenant_state.set_override("sess-A", "tenant-1", _jwt("1"))
        tenant_state.set_override("sess-A", "tenant-2", _jwt("2"))
        rec = tenant_state.get_override("sess-A")
        assert rec.tenant_id == "tenant-2"
        assert rec.access_token == _jwt("2")


class TestSessionIsolation:
    def test_two_sessions_independent(self):
        tenant_state.set_override("sess-A", "tenant-1", _jwt("A"))
        tenant_state.set_override("sess-B", "tenant-2", _jwt("B"))
        assert tenant_state.get_override("sess-A").tenant_id == "tenant-1"
        assert tenant_state.get_override("sess-A").access_token == _jwt("A")
        assert tenant_state.get_override("sess-B").tenant_id == "tenant-2"
        assert tenant_state.get_override("sess-B").access_token == _jwt("B")

    def test_clear_one_session_leaves_others(self):
        tenant_state.set_override("sess-A", "tenant-1", _jwt("A"))
        tenant_state.set_override("sess-B", "tenant-2", _jwt("B"))
        tenant_state.clear_session("sess-A")
        assert tenant_state.get_override("sess-A") is None
        assert tenant_state.get_override("sess-B").tenant_id == "tenant-2"

    def test_clear_unknown_session_is_noop(self):
        tenant_state.set_override("sess-A", "tenant-1", _jwt("A"))
        tenant_state.clear_session("never-set")  # should not raise
        assert tenant_state.get_override("sess-A").tenant_id == "tenant-1"


class TestTTLEviction:
    """Lazy TTL eviction — analysis finding C1. Reads beyond TTL return None
    and remove the entry; reads within TTL refresh the timestamp."""

    def test_entry_expires_after_ttl(self, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr(tenant_state, "_monotonic", lambda: clock["now"])
        monkeypatch.setattr(tenant_state, "_IDLE_TTL_SECONDS", 30.0)

        tenant_state.set_override("sess-A", "tenant-1", _jwt("1"))
        clock["now"] = 1015.0
        assert tenant_state.get_override("sess-A") is not None  # within TTL

        clock["now"] = 1100.0  # >30s since the last touch at 1015
        assert tenant_state.get_override("sess-A") is None  # evicted

        # Re-read after eviction stays None.
        assert tenant_state.get_override("sess-A") is None

    def test_read_refreshes_timestamp(self, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr(tenant_state, "_monotonic", lambda: clock["now"])
        monkeypatch.setattr(tenant_state, "_IDLE_TTL_SECONDS", 30.0)

        tenant_state.set_override("sess-A", "tenant-1", _jwt("1"))

        for _ in range(10):
            clock["now"] += 20.0
            assert tenant_state.get_override("sess-A") is not None

    def test_gc_bounds_dict_size_under_churn(self, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr(tenant_state, "_monotonic", lambda: clock["now"])
        monkeypatch.setattr(tenant_state, "_IDLE_TTL_SECONDS", 30.0)
        monkeypatch.setattr(tenant_state, "_GC_EVERY_N_WRITES", 1)

        for i in range(20):
            tenant_state.set_override(f"sess-{i}", f"tenant-{i}", _jwt(str(i)))

        assert tenant_state._dict_size_for_tests() == 20

        clock["now"] = 2000.0
        tenant_state.set_override("sess-fresh", "tenant-fresh", _jwt("fresh"))

        size = tenant_state._dict_size_for_tests()
        assert size == 1, f"expected only the fresh entry, got {size}"


class TestEnvOverrideTTL:
    def test_env_var_overrides_default_ttl(self, monkeypatch):
        monkeypatch.setenv("MCP_TENANT_OVERRIDE_TTL", "60")
        ttl = tenant_state._read_ttl_from_env(default=1800.0)
        assert ttl == 60.0

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("MCP_TENANT_OVERRIDE_TTL", "not-a-number")
        ttl = tenant_state._read_ttl_from_env(default=1800.0)
        assert ttl == 1800.0


class TestConcurrency:
    def test_concurrent_writes_dont_interleave(self):
        async def writer(label: str, n: int):
            for i in range(n):
                tenant_state.set_override(
                    f"sess-{label}", f"tenant-{label}-{i}", _jwt(f"{label}-{i}"),
                )
                await asyncio.sleep(0)

        async def run():
            await asyncio.gather(
                writer("A", 50),
                writer("B", 50),
                writer("C", 50),
            )

        asyncio.run(run())

        assert tenant_state.get_override("sess-A").tenant_id == "tenant-A-49"
        assert tenant_state.get_override("sess-A").access_token == _jwt("A-49")
        assert tenant_state.get_override("sess-B").tenant_id == "tenant-B-49"
        assert tenant_state.get_override("sess-C").tenant_id == "tenant-C-49"
