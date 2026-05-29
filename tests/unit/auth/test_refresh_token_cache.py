"""Unit tests for src/auth/refresh_token_cache.py — the per-user refresh-
token cache that backs server-side tenant switching (2026-05-18 pivot)."""

from __future__ import annotations

import pytest

from src.auth import refresh_token_cache


@pytest.fixture(autouse=True)
def _isolate():
    refresh_token_cache._reset_for_tests()
    yield
    refresh_token_cache._reset_for_tests()


class TestRoundtrip:
    def test_set_then_get(self):
        refresh_token_cache.set_token("user-A", "rt-alpha")
        assert refresh_token_cache.get_token("user-A") == "rt-alpha"

    def test_absent_user_returns_none(self):
        assert refresh_token_cache.get_token("never-set") is None

    def test_overwrite_replaces(self):
        refresh_token_cache.set_token("user-A", "rt-1")
        refresh_token_cache.set_token("user-A", "rt-2")
        assert refresh_token_cache.get_token("user-A") == "rt-2"


class TestUserIsolation:
    def test_two_users_independent(self):
        refresh_token_cache.set_token("user-A", "rt-A")
        refresh_token_cache.set_token("user-B", "rt-B")
        assert refresh_token_cache.get_token("user-A") == "rt-A"
        assert refresh_token_cache.get_token("user-B") == "rt-B"

    def test_forget_one_leaves_others(self):
        refresh_token_cache.set_token("user-A", "rt-A")
        refresh_token_cache.set_token("user-B", "rt-B")
        refresh_token_cache.forget_user("user-A")
        assert refresh_token_cache.get_token("user-A") is None
        assert refresh_token_cache.get_token("user-B") == "rt-B"


class TestInputValidation:
    def test_empty_user_sub_is_noop_on_set(self):
        refresh_token_cache.set_token("", "rt-X")
        assert refresh_token_cache._size_for_tests() == 0

    def test_empty_token_is_noop_on_set(self):
        refresh_token_cache.set_token("user-A", "")
        assert refresh_token_cache._size_for_tests() == 0

    def test_empty_user_sub_returns_none_on_get(self):
        assert refresh_token_cache.get_token("") is None

    def test_forget_unknown_user_is_noop(self):
        refresh_token_cache.forget_user("never-set")  # should not raise


class TestPersistence:
    """Disk persistence so the cache survives container restarts during
    local development (2026-05-19 addition)."""

    def test_writes_to_disk_on_set(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "refresh_tokens.json"
        monkeypatch.setenv("MCP_REFRESH_TOKEN_CACHE_PATH", str(cache_file))
        refresh_token_cache._reset_for_tests()

        refresh_token_cache.set_token("user-A", "rt-alpha")

        assert cache_file.exists()
        import json
        with open(cache_file) as f:
            data = json.load(f)
        assert data == {"user-A": "rt-alpha"}

    def test_loads_from_disk_on_first_access(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "refresh_tokens.json"
        cache_file.write_text('{"user-B": "rt-beta", "user-C": "rt-gamma"}')
        monkeypatch.setenv("MCP_REFRESH_TOKEN_CACHE_PATH", str(cache_file))
        refresh_token_cache._reset_for_tests()

        assert refresh_token_cache.get_token("user-B") == "rt-beta"
        assert refresh_token_cache.get_token("user-C") == "rt-gamma"

    def test_survives_simulated_restart(self, tmp_path, monkeypatch):
        """Write once, simulate a restart by resetting in-process state and
        clearing the loaded flag — values come back from disk."""
        cache_file = tmp_path / "refresh_tokens.json"
        monkeypatch.setenv("MCP_REFRESH_TOKEN_CACHE_PATH", str(cache_file))
        refresh_token_cache._reset_for_tests()

        refresh_token_cache.set_token("user-A", "rt-alpha")
        # Simulate process restart: clear in-process state.
        refresh_token_cache._reset_for_tests()
        # Reading without writing — should populate from disk.
        assert refresh_token_cache.get_token("user-A") == "rt-alpha"

    def test_forget_user_persists_removal(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "refresh_tokens.json"
        monkeypatch.setenv("MCP_REFRESH_TOKEN_CACHE_PATH", str(cache_file))
        refresh_token_cache._reset_for_tests()

        refresh_token_cache.set_token("user-A", "rt-alpha")
        refresh_token_cache.set_token("user-B", "rt-beta")
        refresh_token_cache.forget_user("user-A")

        # Simulate restart; user-A is gone, user-B remains.
        refresh_token_cache._reset_for_tests()
        assert refresh_token_cache.get_token("user-A") is None
        assert refresh_token_cache.get_token("user-B") == "rt-beta"

    def test_empty_env_disables_persistence(self, tmp_path, monkeypatch):
        """Setting MCP_REFRESH_TOKEN_CACHE_PATH to empty string disables
        persistence (purely in-memory). Used in tests / by operators who
        want zero-disk-footprint operation."""
        monkeypatch.setenv("MCP_REFRESH_TOKEN_CACHE_PATH", "")
        refresh_token_cache._reset_for_tests()

        refresh_token_cache.set_token("user-A", "rt-alpha")
        # Nothing was written to disk (path is None).
        # Reset state; reading after reset gets nothing back.
        refresh_token_cache._reset_for_tests()
        assert refresh_token_cache.get_token("user-A") is None

    def test_corrupt_disk_file_starts_empty(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "refresh_tokens.json"
        cache_file.write_text("not valid json {")
        monkeypatch.setenv("MCP_REFRESH_TOKEN_CACHE_PATH", str(cache_file))
        refresh_token_cache._reset_for_tests()

        # Corrupt file → empty cache, but no crash.
        assert refresh_token_cache.get_token("any") is None
        # Subsequent writes still work.
        refresh_token_cache.set_token("user-X", "rt-x")
        assert refresh_token_cache.get_token("user-X") == "rt-x"

    def test_unwritable_path_falls_back_to_memory(self, tmp_path, monkeypatch):
        """If the configured path can't be written (e.g., permission denied
        on a parent that exists as a file), the cache still works in-memory."""
        # Create a file where we'd expect a directory — makes mkdir+write fail.
        blocking_file = tmp_path / "blocked"
        blocking_file.write_text("not-a-directory")
        bad_path = blocking_file / "refresh_tokens.json"
        monkeypatch.setenv("MCP_REFRESH_TOKEN_CACHE_PATH", str(bad_path))
        refresh_token_cache._reset_for_tests()

        # In-memory write succeeds even though persist fails.
        refresh_token_cache.set_token("user-A", "rt-alpha")
        assert refresh_token_cache.get_token("user-A") == "rt-alpha"
