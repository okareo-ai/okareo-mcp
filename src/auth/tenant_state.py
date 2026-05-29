"""Per-MCP-session active-tenant override (FR-024).

When the user calls ``switch_tenant``, the MCP server mints a new Frontegg
access token bound to the chosen tenant (via Frontegg's
``/identity/resources/auth/v1/user/token/refresh`` endpoint with `tenantId`).
That new JWT is stored here, keyed by `Mcp-Session-Id`. Subsequent tool calls
in the same session use the override JWT as the Okareo SDK's api_key — so
the Okareo backend reads the right ``tenantId`` claim and scopes the call
accordingly.

This replaces the original ``X-Okareo-Org-Override`` header design
(2026-05-18 pivot, after discovering the Frontegg-native pattern is token
refresh, not a header override).

Storing JWTs in process memory: the override JWTs are short-lived (Frontegg
default ~30 min access-token TTL) and stored only for the lifetime of the
MCP session. Same security posture as the original SessionCredential ContextVar
— credentials in process memory, never on disk, never in logs.

Eviction is dual-mode (analysis finding C1):

1. **Lazy TTL (load-bearing).** ``get_override()`` checks ``last_touched`` on
   every read; entries older than ``MCP_TENANT_OVERRIDE_TTL`` seconds
   (default 1800 / 30 min) are dropped. Active reads refresh the timestamp.

2. **Explicit session-end hook (opportunistic, fast-path).** If FastMCP
   exposes a session-end callback we register one; the TTL is the contract,
   the hook is an optimization.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

_DEFAULT_IDLE_TTL_SECONDS = 1800.0  # 30 minutes
_DEFAULT_GC_EVERY_N_WRITES = 100


def _monotonic() -> float:
    """Wrapped for monkeypatching in tests."""
    return time.monotonic()


def _read_ttl_from_env(default: float) -> float:
    """Resolve ``MCP_TENANT_OVERRIDE_TTL`` env var, falling back on invalid input."""
    raw = os.environ.get("MCP_TENANT_OVERRIDE_TTL", "").strip()
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


_IDLE_TTL_SECONDS: float = _read_ttl_from_env(_DEFAULT_IDLE_TTL_SECONDS)
_GC_EVERY_N_WRITES: int = _DEFAULT_GC_EVERY_N_WRITES


@dataclass(frozen=True)
class TenantOverride:
    """The per-session override record.

    Stores both the tenant_id (for display / diagnostics) and the access_token
    that's actually bound to that tenant. Downstream Okareo calls use
    ``access_token`` as the api_key; ``tenant_id`` is what ``list_tenants``
    reports as ``active_tenant_id`` when the override is in effect.
    """

    tenant_id: str
    access_token: str


# {session_id: (last_touched_monotonic, TenantOverride)}
_overrides: dict[str, tuple[float, TenantOverride]] = {}
_writes_since_last_gc: int = 0


def set_override(session_id: str, tenant_id: str, access_token: str) -> None:
    """Store the active-tenant choice for this MCP session.

    Caller MUST have validated that ``tenant_id`` is in the user's allowed-tenant
    set (FR-025) AND that ``access_token`` was just minted by Frontegg for
    ``tenant_id``. This function does not re-validate.
    """
    global _writes_since_last_gc
    record = TenantOverride(tenant_id=tenant_id, access_token=access_token)
    _overrides[session_id] = (_monotonic(), record)
    _writes_since_last_gc += 1
    if _writes_since_last_gc >= _GC_EVERY_N_WRITES:
        _gc_expired()
        _writes_since_last_gc = 0


def get_override(session_id: str) -> TenantOverride | None:
    """Return the active override for ``session_id``, or ``None`` if unset / expired.

    Reads refresh ``last_touched`` so an actively-used session never expires.
    """
    entry = _overrides.get(session_id)
    if entry is None:
        return None
    last_touched, record = entry
    now = _monotonic()
    if (now - last_touched) > _IDLE_TTL_SECONDS:
        # Lazy TTL eviction. Drop and report absent.
        _overrides.pop(session_id, None)
        return None
    # Refresh on read so active conversations don't time out.
    _overrides[session_id] = (now, record)
    return record


def get_override_tenant_id(session_id: str) -> str | None:
    """Convenience accessor: returns the tenant_id (not the JWT) for the
    session's current override, or ``None``. Used by display paths that
    don't need the access token."""
    record = get_override(session_id)
    return record.tenant_id if record is not None else None


def clear_session(session_id: str) -> None:
    """Explicit eviction. Called from the FastMCP session-end hook if present."""
    _overrides.pop(session_id, None)


def _gc_expired() -> None:
    """Full sweep — drop every expired entry. Opportunistic; bounded cost."""
    now = _monotonic()
    expired = [
        sid for sid, (touched, _) in _overrides.items()
        if (now - touched) > _IDLE_TTL_SECONDS
    ]
    for sid in expired:
        _overrides.pop(sid, None)


# ---------------------------------------------------------------------------
# Test helpers (production code MUST NOT call these)
# ---------------------------------------------------------------------------


def _reset_for_tests() -> None:
    global _writes_since_last_gc, _IDLE_TTL_SECONDS, _GC_EVERY_N_WRITES
    _overrides.clear()
    _writes_since_last_gc = 0
    _IDLE_TTL_SECONDS = _read_ttl_from_env(_DEFAULT_IDLE_TTL_SECONDS)
    _GC_EVERY_N_WRITES = _DEFAULT_GC_EVERY_N_WRITES


def _dict_size_for_tests() -> int:
    return len(_overrides)
