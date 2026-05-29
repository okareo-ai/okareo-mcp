"""Unit tests for the per-credential token-bucket throttle (T027 / FR-013 / SC-007)."""

from __future__ import annotations

import pytest

from src.auth import throttle
from src.auth.throttle import PerCredentialThrottle, TokenBucket


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 1000.0}
    monkeypatch.setattr(throttle, "_monotonic", lambda: state["now"])
    return state


class TestTokenBucket:
    def test_within_capacity_allows(self, clock):
        b = TokenBucket(capacity=10.0, refill_per_sec=1.0)
        for _ in range(10):
            ok, _ = b.try_acquire()
            assert ok

    def test_over_capacity_rejects_with_retry_after(self, clock):
        b = TokenBucket(capacity=5.0, refill_per_sec=1.0)
        for _ in range(5):
            assert b.try_acquire()[0]
        ok, retry = b.try_acquire()
        assert not ok
        assert retry > 0.0

    def test_bucket_refills_over_time(self, clock):
        b = TokenBucket(capacity=5.0, refill_per_sec=1.0)
        for _ in range(5):
            b.try_acquire()
        ok, _ = b.try_acquire()
        assert not ok
        # 1 second passes — one token added.
        clock["now"] += 1.0
        ok, _ = b.try_acquire()
        assert ok

    def test_refill_capped_at_capacity(self, clock):
        b = TokenBucket(capacity=5.0, refill_per_sec=1.0)
        # Wait a long time — capacity does not grow past 5.
        clock["now"] += 60.0
        # Should be able to acquire 5 in a row.
        for _ in range(5):
            assert b.try_acquire()[0]
        # 6th must be rejected.
        assert not b.try_acquire()[0]

    def test_disabled_when_capacity_zero(self, clock):
        b = TokenBucket(capacity=0.0, refill_per_sec=0.0)
        for _ in range(100):
            ok, retry = b.try_acquire()
            assert ok
            assert retry == 0.0


class TestPerCredentialThrottle:
    def test_independent_buckets(self, clock, monkeypatch):
        monkeypatch.setenv("MCP_RATE_LIMIT_RPM", "5")
        t = PerCredentialThrottle()

        # Drain org-A's bucket entirely.
        for _ in range(5):
            assert t.try_acquire("org-A")[0]
        ok_a, _ = t.try_acquire("org-A")
        assert not ok_a

        # org-B still has full capacity — its bucket was never touched.
        for _ in range(5):
            ok_b, _ = t.try_acquire("org-B")
            assert ok_b, "org-A's exhaustion must not affect org-B"

    def test_disabled_when_rpm_zero(self, clock, monkeypatch):
        monkeypatch.setenv("MCP_RATE_LIMIT_RPM", "0")
        t = PerCredentialThrottle()
        assert not t.enabled
        for _ in range(200):
            ok, retry = t.try_acquire("anyone")
            assert ok and retry == 0.0

    def test_env_invalid_falls_back_to_default(self, clock, monkeypatch):
        monkeypatch.setenv("MCP_RATE_LIMIT_RPM", "not-a-number")
        t = PerCredentialThrottle()
        assert t.enabled
        # Default 200 RPM → drain takes 200 calls.
        granted = 0
        for _ in range(210):
            if t.try_acquire("org-X")[0]:
                granted += 1
        assert granted == 200

    def test_retry_after_grows_with_deficit(self, clock, monkeypatch):
        monkeypatch.setenv("MCP_RATE_LIMIT_RPM", "60")
        t = PerCredentialThrottle()

        # Drain.
        for _ in range(60):
            t.try_acquire("org-X")
        _, retry1 = t.try_acquire("org-X")
        _, retry2 = t.try_acquire("org-X")
        # Second rejection should suggest a slightly longer wait (deficit grew).
        assert retry2 >= retry1
