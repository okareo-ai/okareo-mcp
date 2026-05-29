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
# Tenant switching via Frontegg's token-refresh endpoint
# ---------------------------------------------------------------------------


class TenantSwitchFailed(RuntimeError):
    """Frontegg returned 4xx/5xx for the tenant-switch refresh, or the
    response body was malformed. Treated by `switch_tenant` as a recoverable
    error — the caller can retry, or in the 401 case re-authenticate."""


class TenantSwitchAuthError(TenantSwitchFailed):
    """Frontegg rejected the refresh token (401). The user must re-sign-in
    so a fresh refresh token can be captured."""


async def refresh_with_tenant(
    *,
    refresh_token: str,
    tenant_id: str,
    frontegg_domain: str,
    frontegg_client_id: str = "",
    current_access_token: str = "",
    frontegg_vendor_id: str = "",  # unused, kept for back-compat
) -> dict[str, str | int]:
    """Switch the user's active tenant via Frontegg's purpose-built endpoint
    and return a new access token bound to the requested tenant.

    Flow (third iteration, 2026-05-18):

    1. **`PUT https://<frontegg_domain>/identity/resources/users/v3/me/tenant`**
       Body: `{tenantId}`. Bearer auth with the user's CURRENT access token.
       Side effect: Frontegg sets the user's "active tenant" context to the
       requested tenant. Response often includes new tokens directly.

    2. **If step 1 returned new tokens in the body, return them.** Some
       Frontegg configurations do this; others return 200 with an empty body
       and require a follow-up refresh.

    3. **Otherwise, refresh via `POST /oauth/token` with `grant_type=refresh_token`.**
       Now that the active tenant is set to the new one, Frontegg returns a
       tenant-scoped access token without us having to pass `tenantId`.

    Earlier iterations of this code path (cookie-auth at
    `/identity/resources/auth/v1/user/token/refresh`, and `tenantId` extension
    on `/oauth/token`) didn't work — the first because OAuth refresh tokens
    aren't accepted at the session-cookie endpoint, the second because
    Frontegg's standard `/oauth/token` silently ignores the `tenantId`
    extension on this account.

    Returns a dict with ``access_token``, ``refresh_token``, ``expires_in``.

    Raises:
        TenantSwitchAuthError: Frontegg returned 401 (access token or refresh
            token invalid). User must re-sign-in.
        TenantSwitchFailed: any other 4xx/5xx or malformed response.
    """
    _ = frontegg_vendor_id  # kept for caller compatibility; unused
    if not frontegg_domain:
        raise TenantSwitchFailed("Frontegg domain not configured")
    if not refresh_token:
        raise TenantSwitchFailed("No refresh token available for this user")
    if not tenant_id:
        raise TenantSwitchFailed("tenant_id is required")
    if not current_access_token:
        raise TenantSwitchFailed(
            "Current access token required to call Frontegg's /me/tenant endpoint"
        )

    # --- Step 1: set the user's active tenant via /users/v1/tenant ----------
    # Path confirmed against the Okareo UI's network requests (PUT
    # /frontegg/identity/resources/users/v1/tenant via the UI's custom-domain
    # routing). The server-to-server call skips the `/frontegg/` prefix.
    me_tenant_url = (
        f"https://{frontegg_domain}/identity/resources/users/v1/tenant"
    )
    me_tenant_headers = {
        "Authorization": f"Bearer {current_access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    me_tenant_body = {"tenantId": tenant_id}

    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS) as client:
            me_resp = await client.put(
                me_tenant_url,
                json=me_tenant_body,
                headers=me_tenant_headers,
            )
    except httpx.HTTPError as exc:
        _logger.warning(
            "Frontegg /me/tenant network error: %s", type(exc).__name__,
        )
        raise TenantSwitchFailed("Frontegg unreachable") from None

    if me_resp.status_code in (401, 403):
        body_preview = (me_resp.text or "")[:300]
        _logger.warning(
            "Frontegg /me/tenant 401/403. Response body: %s", body_preview,
        )
        raise TenantSwitchAuthError(
            "Frontegg rejected the access token at /me/tenant; user must re-sign-in"
        )
    if not (200 <= me_resp.status_code < 300):
        body_preview = (me_resp.text or "")[:300]
        _logger.warning(
            "Frontegg /me/tenant failed status=%d body=%s",
            me_resp.status_code, body_preview,
        )
        raise TenantSwitchFailed(
            f"Frontegg /me/tenant returned status {me_resp.status_code}"
        )

    # Some Frontegg configurations return new tokens directly here.
    try:
        me_body = me_resp.json() if me_resp.text.strip() else {}
    except ValueError:
        me_body = {}

    direct_access = (
        me_body.get("access_token") or me_body.get("accessToken")
        if isinstance(me_body, dict) else None
    )
    direct_refresh = (
        me_body.get("refresh_token") or me_body.get("refreshToken")
        if isinstance(me_body, dict) else None
    )
    direct_expires = (
        me_body.get("expires_in") or me_body.get("expiresIn")
        if isinstance(me_body, dict) else None
    )

    if direct_access and direct_refresh:
        return {
            "access_token": str(direct_access),
            "refresh_token": str(direct_refresh),
            "expires_in": int(direct_expires) if direct_expires is not None else 0,
        }

    # --- Step 2: refresh via /oauth/token to pick up the new active tenant --
    oauth_url = f"https://{frontegg_domain}/oauth/token"
    oauth_data: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if frontegg_client_id:
        oauth_data["client_id"] = frontegg_client_id

    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS) as client:
            r = await client.post(
                oauth_url,
                data=oauth_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        _logger.warning(
            "Frontegg /oauth/token (post-switch) network error: %s",
            type(exc).__name__,
        )
        raise TenantSwitchFailed("Frontegg unreachable on post-switch refresh") from None

    if r.status_code in (401, 403):
        body_preview = (r.text or "")[:300]
        _logger.warning(
            "Frontegg /oauth/token post-switch 401/403. Body: %s", body_preview,
        )
        raise TenantSwitchAuthError(
            "Frontegg rejected the refresh token on post-switch refresh"
        )
    if not (200 <= r.status_code < 300):
        body_preview = (r.text or "")[:300]
        _logger.warning(
            "Frontegg /oauth/token post-switch failed status=%d body=%s",
            r.status_code, body_preview,
        )
        raise TenantSwitchFailed(
            f"Frontegg post-switch refresh returned status {r.status_code}"
        )

    try:
        oauth_body = r.json()
    except ValueError as exc:
        raise TenantSwitchFailed("Frontegg post-switch refresh response not JSON") from exc

    if not isinstance(oauth_body, dict):
        raise TenantSwitchFailed("Frontegg post-switch refresh response not an object")

    new_access = oauth_body.get("access_token") or oauth_body.get("accessToken")
    new_refresh = oauth_body.get("refresh_token") or oauth_body.get("refreshToken")
    expires_in = oauth_body.get("expires_in") or oauth_body.get("expiresIn")

    if not new_access:
        raise TenantSwitchFailed("Frontegg response missing access_token")
    if not new_refresh:
        raise TenantSwitchFailed("Frontegg response missing rotated refresh_token")

    return {
        "access_token": str(new_access),
        "refresh_token": str(new_refresh),
        "expires_in": int(expires_in) if expires_in is not None else 0,
    }


# ---------------------------------------------------------------------------
# Test helpers (production code MUST NOT call these)
# ---------------------------------------------------------------------------


def _reset_for_tests() -> None:
    global _CACHE_TTL_SECONDS
    _cache.clear()
    _CACHE_TTL_SECONDS = float(
        os.environ.get("MCP_TENANT_CACHE_TTL", "").strip() or _DEFAULT_CACHE_TTL_SECONDS
    )
