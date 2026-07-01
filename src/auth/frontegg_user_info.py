"""Frontegg user-info wrapper: source the user's allowed-tenant set.

Used by `list_tenants` (display) and `switch_tenant` (validation). Calls
``GET https://<FRONTEGG_DOMAIN>/identity/resources/users/v3/me`` carrying the
caller's own JWT — NEVER an admin / M2M credential (FR-029). The response's
``tenants[]`` array (each entry has ``tenantId`` + ``name``) is what the MCP
returns to the LLM.

Per-session TTL cache (default 60s) avoids redundant calls when the LLM
invokes `list_tenants` multiple times in the same conversation turn. The
cache is keyed by ``Mcp-Session-Id`` exactly (no cross-user bleed possible
because session IDs are server-minted with sufficient entropy).

Security:

- The JWT MUST NEVER appear in log lines (test asserts this with a sentinel).
- Error messages MUST NOT echo the JWT either; we surface only category
  ("auth", "lookup_failed"), not contents.
- The endpoint URL is the public Frontegg ``/identity/resources/users/v3/me``
  documented at https://developers.frontegg.com/api/identity/users —
  the caller's JWT is its only authentication.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import httpx

_logger = logging.getLogger(__name__)

_DEFAULT_CACHE_TTL_SECONDS = 60.0
_CACHE_TTL_SECONDS: float = _DEFAULT_CACHE_TTL_SECONDS
_DEFAULT_TIMEOUT_SECONDS = 5.0


def _monotonic() -> float:
    """Wrapped for monkeypatching in tests."""
    return time.monotonic()


class TenantLookupAuthError(RuntimeError):
    """Frontegg returned 401 / 403 — the JWT is no longer valid for user-info.

    The tool surface MAY translate this to ``tenant_lookup_failed`` or let the
    outer auth layer surface a re-auth error; either is acceptable since the
    underlying issue is the same. Error messages MUST NOT include the JWT.
    """


class TenantLookupFailed(RuntimeError):
    """Frontegg returned 5xx, timed out, or the response body was malformed.

    Retriable. Error messages MUST NOT include the JWT.
    """


@dataclass(frozen=True)
class Tenant:
    """A Frontegg tenant the user belongs to."""

    id: str
    name: str


# Cache: {session_id: (timestamp, tenants)}
_cache: dict[str, tuple[float, list[Tenant]]] = {}


async def _get(url: str, headers: dict[str, str]) -> httpx.Response:
    """Issue the GET to Frontegg. Wrapped so tests can patch a single seam.

    Kept module-level (not inside `get_user_tenants`) so AsyncMock.patch works
    cleanly without monkey-patching the function under test.
    """
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS) as client:
        return await client.get(url, headers=headers)


async def get_user_tenants(
    *, jwt: str, session_id: str, frontegg_domain: str,
) -> list[Tenant]:
    """Return the tenants the authenticated user belongs to.

    Args:
        jwt: The user's access token. Forwarded verbatim as the bearer
            credential to Frontegg. MUST NOT be logged.
        session_id: The MCP session this lookup is being made for. Used as
            the cache key — two sessions for the same user MAY each make
            their own first call.
        frontegg_domain: ``<tenant>.frontegg.com`` (no scheme, no path).

    Raises:
        TenantLookupAuthError: Frontegg rejected the JWT (401/403).
        TenantLookupFailed: 5xx, timeout, or response body malformed.
    """
    cached = _cache.get(session_id)
    if cached is not None:
        ts, tenants = cached
        if (_monotonic() - ts) <= _CACHE_TTL_SECONDS:
            return tenants
        # Stale — drop and refetch.
        _cache.pop(session_id, None)

    if not frontegg_domain:
        raise TenantLookupFailed("Frontegg domain not configured")

    headers = {
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/json",
    }

    # Primary endpoint: /identity/resources/users/v3/me/tenants returns the
    # full tenant objects (id + name + metadata) the user belongs to. This is
    # what we want for `list_tenants` UX.
    #
    # Fallback: /identity/resources/users/v3/me also includes a `tenants[]`
    # array on some Frontegg account configurations, but in many cases each
    # entry only carries `tenantId` (no name). The /me endpoint is the
    # original v0 implementation kept here as a fallback for older Frontegg
    # accounts where /me/tenants returns 404.
    tenants_url = f"https://{frontegg_domain}/identity/resources/users/v3/me/tenants"
    me_url = f"https://{frontegg_domain}/identity/resources/users/v3/me"

    raw_tenants = await _fetch_tenants_payload(
        tenants_url, me_url, headers,
    )

    tenants: list[Tenant] = []
    missing_name_count = 0
    sample_keys: list[str] = []
    for entry in raw_tenants:
        if not isinstance(entry, dict):
            continue
        if not sample_keys:
            sample_keys = sorted(entry.keys())
        tid = (
            entry.get("tenantId")
            or entry.get("id")
            or entry.get("tenant_id")
        )
        name = (
            entry.get("name")
            or entry.get("tenantName")
            or entry.get("displayName")
            or entry.get("tenant_name")
            or ""
        )
        if not tid:
            continue
        if not name:
            missing_name_count += 1
        tenants.append(Tenant(id=str(tid), name=str(name)))

    if missing_name_count > 0:
        # Diagnostic so an operator can iterate on the Frontegg config without
        # leaking the JWT or any secret. Logs only the response field names,
        # not values.
        _logger.warning(
            "Frontegg user-info returned %d tenant(s) with no name field. "
            "Available keys per entry: %s. The tool will fall back to "
            "id-only display.",
            missing_name_count,
            sample_keys,
        )

    _cache[session_id] = (_monotonic(), tenants)
    return tenants


async def _fetch_tenants_payload(
    tenants_url: str, me_url: str, headers: dict[str, str],
) -> list[dict]:
    """Return the raw tenants list. Tries /me/tenants first, falls back to /me.

    Returns a list of dicts (each representing one tenant); empty list if
    neither endpoint returns anything useful. Raises ``TenantLookupAuthError``
    or ``TenantLookupFailed`` on hard failures (4xx/5xx/network).
    """
    # Primary: /me/tenants — preferred because it returns full tenant objects
    # (with names). Response is a top-level JSON array.
    try:
        response = await _get(tenants_url, headers)
    except httpx.HTTPError as exc:
        _logger.warning(
            "Frontegg /me/tenants network error: %s", type(exc).__name__,
        )
        raise TenantLookupFailed("Frontegg /me/tenants unreachable") from None

    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError as exc:
            raise TenantLookupFailed(
                "Frontegg /me/tenants response not JSON"
            ) from exc
        if isinstance(body, list):
            return [e for e in body if isinstance(e, dict)]
        # Some Frontegg versions wrap the list in {"items": [...]} or similar.
        if isinstance(body, dict):
            for key in ("items", "tenants", "data"):
                inner = body.get(key)
                if isinstance(inner, list):
                    return [e for e in inner if isinstance(e, dict)]
        # Unrecognized shape — fall through to the /me fallback.

    if response.status_code in (401, 403):
        raise TenantLookupAuthError("Frontegg rejected the access token")

    # Anything else (404 from older Frontegg, unexpected 2xx shape, etc.):
    # try the /me fallback.
    try:
        response = await _get(me_url, headers)
    except httpx.HTTPError as exc:
        _logger.warning(
            "Frontegg /me network error: %s", type(exc).__name__,
        )
        raise TenantLookupFailed("Frontegg /me unreachable") from None

    if response.status_code in (401, 403):
        raise TenantLookupAuthError("Frontegg rejected the access token")
    if not (200 <= response.status_code < 300):
        raise TenantLookupFailed(
            f"Frontegg user-info returned status {response.status_code}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise TenantLookupFailed("Frontegg user-info response not JSON") from exc

    raw = body.get("tenants") if isinstance(body, dict) else None
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    raise TenantLookupFailed("Frontegg user-info response missing tenants array")


def invalidate_cache(session_id: str) -> None:
    """Drop the cached tenant list for one session. Safe if absent."""
    _cache.pop(session_id, None)


# ---------------------------------------------------------------------------
# Test helpers (production code MUST NOT call these)
# ---------------------------------------------------------------------------


def _reset_for_tests() -> None:
    global _CACHE_TTL_SECONDS
    _cache.clear()
    _CACHE_TTL_SECONDS = float(
        os.environ.get("MCP_TENANT_CACHE_TTL", "").strip() or _DEFAULT_CACHE_TTL_SECONDS
    )
