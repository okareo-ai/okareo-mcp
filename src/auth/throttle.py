"""Per-credential token-bucket rate limiter (FR-013, SC-007).

In-process, per-server-instance. One ``TokenBucket`` per ``org_id``; buckets
are created on first access and never deleted (memory usage scales with the
number of distinct orgs that ever hit a given instance — bounded in practice).

This is intentionally simple — no Redis, no cross-instance coordination. The
spec.md assumption block acknowledges that cross-instance throttling is a
follow-up if traffic warrants it. A single runaway client only burns its own
bucket's budget; other orgs' buckets are independent.

Tunables (env, default 200 req/min/org):
    MCP_RATE_LIMIT_RPM    — bucket capacity AND refill rate in requests/min.

If the env var is unset or invalid, defaults are used. ``MCP_RATE_LIMIT_RPM=0``
disables throttling entirely (used by tests that don't want to set up
deterministic timing).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field


_DEFAULT_RPM = 200
_DEFAULT_CAPACITY = float(_DEFAULT_RPM)


def _monotonic() -> float:
    """Wrapped for monkeypatching in tests."""
    return time.monotonic()


def _read_rpm_from_env(default: int = _DEFAULT_RPM) -> int:
    raw = os.environ.get("MCP_RATE_LIMIT_RPM", "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


@dataclass
class TokenBucket:
    """Standard token bucket: ``capacity`` tokens, refills at ``refill_per_sec``.

    Disabled when ``capacity <= 0`` — ``try_acquire`` always returns ``True``.
    """

    capacity: float
    refill_per_sec: float
    tokens: float = field(init=False)
    last_refill: float = field(init=False)

    def __post_init__(self):
        self.tokens = self.capacity
        self.last_refill = _monotonic()

    def try_acquire(self, cost: float = 1.0) -> tuple[bool, float]:
        """Acquire ``cost`` tokens. Returns ``(allowed, retry_after_seconds)``.

        On success: ``(True, 0.0)``. On rejection: ``(False, retry_after)``
        where ``retry_after`` is the number of seconds to wait before one
        more token becomes available (suitable for an HTTP ``Retry-After``
        header).
        """
        if self.capacity <= 0 or self.refill_per_sec <= 0:
            # Throttling disabled.
            return True, 0.0

        now = _monotonic()
        elapsed = max(0.0, now - self.last_refill)
        # Refill, capped at capacity.
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self.last_refill = now

        if self.tokens >= cost:
            self.tokens -= cost
            return True, 0.0

        deficit = cost - self.tokens
        retry_after = deficit / self.refill_per_sec
        return False, retry_after


class PerCredentialThrottle:
    """Manages one ``TokenBucket`` per ``org_id``.

    Bucket capacity and refill are read from ``MCP_RATE_LIMIT_RPM`` at
    construction (or overridable for tests). Independent buckets — one
    abusive org doesn't burn another org's budget (SC-007).
    """

    def __init__(self, rpm: int | None = None) -> None:
        if rpm is None:
            rpm = _read_rpm_from_env()
        self._capacity = float(max(0, rpm))
        # Refill is rpm / 60 → tokens per second.
        self._refill_per_sec = self._capacity / 60.0 if self._capacity > 0 else 0.0
        self._buckets: dict[str, TokenBucket] = {}

    @property
    def enabled(self) -> bool:
        return self._capacity > 0

    def try_acquire(self, org_id: str, cost: float = 1.0) -> tuple[bool, float]:
        """Returns ``(allowed, retry_after_seconds)``. Always allows when disabled."""
        if not self.enabled:
            return True, 0.0
        bucket = self._buckets.get(org_id)
        if bucket is None:
            bucket = TokenBucket(
                capacity=self._capacity, refill_per_sec=self._refill_per_sec,
            )
            self._buckets[org_id] = bucket
        return bucket.try_acquire(cost=cost)

    # ----- test helpers (production code MUST NOT call these) -----

    def _bucket_for_tests(self, org_id: str) -> TokenBucket | None:
        return self._buckets.get(org_id)
