"""Integration test for per-credential throttle isolation (T028, FR-013, SC-007).

Drives the ``PerCredentialThrottle`` under a sustained-load profile from one
credential and asserts that a second credential continues to be served with
zero 429s. Drives the bucket directly rather than over HTTP because the
isolation property lives in the bucket boundary, not the transport layer.
"""

from __future__ import annotations

import pytest

from src.auth.throttle import PerCredentialThrottle


@pytest.fixture
def throttle_60_rpm(monkeypatch):
    monkeypatch.setenv("MCP_RATE_LIMIT_RPM", "60")
    return PerCredentialThrottle()


class TestThrottleIsolation:
    def test_one_abusive_org_does_not_burn_another(self, throttle_60_rpm):
        """Org A burns through its bucket; org B's bucket is independent."""
        t = throttle_60_rpm

        # 200 requests from org-A. After the first 60, the rest are rejected.
        a_allowed = 0
        a_rejected = 0
        for _ in range(200):
            ok, _ = t.try_acquire("org-A")
            if ok:
                a_allowed += 1
            else:
                a_rejected += 1

        assert a_allowed == 60
        assert a_rejected == 140

        # Meanwhile, org-B has had nothing happen to it. 20 requests in a
        # row: all 200s (every one of them allowed).
        for _ in range(20):
            ok, retry = t.try_acquire("org-B")
            assert ok, "org-A's abuse must not affect org-B"
            assert retry == 0.0

    def test_rejected_request_carries_retry_after(self, throttle_60_rpm):
        t = throttle_60_rpm
        for _ in range(60):
            t.try_acquire("org-X")
        ok, retry = t.try_acquire("org-X")
        assert not ok
        assert retry > 0.0


class TestThrottleDisabledByEnv:
    def test_env_zero_disables_throttle_for_all_orgs(self, monkeypatch):
        monkeypatch.setenv("MCP_RATE_LIMIT_RPM", "0")
        t = PerCredentialThrottle()
        for org in ("a", "b", "c"):
            for _ in range(500):
                ok, retry = t.try_acquire(org)
                assert ok and retry == 0.0
