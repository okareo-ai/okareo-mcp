"""Tenant-management MCP tools (FR-023..FR-029).

Two tools, both OAuth-only:

- ``list_tenants()`` — return every Frontegg tenant the authenticated user
  belongs to. Each entry: ``{id, name, is_current}``. Top-level fields
  ``active_tenant_id`` and ``active_tenant_source`` (``"override"`` vs
  ``"jwt_default"``) help the LLM detect a missing override after restart.

- ``switch_tenant(tenant_id)`` — mints a NEW Frontegg access token bound to
  the target tenant (via Frontegg's tenant-switch refresh endpoint), stores
  it as the per-MCP-session override, and returns the active-tenant info to
  the LLM. The new JWT is what downstream Okareo calls use; the user's
  original JWT (the one their MCP client presents) is left untouched.

This is the Frontegg-native pattern — Frontegg's `tenantId` claim on the
JWT is the source of truth for tenant scoping, so the only way to "switch"
is to mint a new JWT (2026-05-18 architecture pivot). The MCP server caches
the user's refresh_token at /oauth/callback so it can perform this mint on
the user's behalf without prompting them to re-sign-in.

On Bearer-API-key sessions both tools return
``tenant_selection_requires_oauth`` with a docs pointer (FR-026).
"""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from src.auth import refresh_token_cache, tenant_state
from src.auth.context import get_session_credential_optional
from src.auth.frontegg_user_info import (
    TenantLookupAuthError,
    TenantLookupFailed,
    TenantSwitchAuthError,
    TenantSwitchFailed,
    get_user_tenants,
    refresh_with_tenant,
)

_DOCS_URL = "https://docs.okareo.com/docs/mcp/remote#oauth-sign-in"


def _error(code: str, message: str, data: dict[str, Any] | None = None) -> str:
    """Format an MCP tool error consistent with FR-025 (no PII / no rejected
    inputs in the data payload)."""
    return json.dumps(
        {"error": {"code": code, "message": message, "data": data or {}}}
    )


def _current_session_id() -> str | None:
    """Bridge to ``src.okareo_client._current_session_id`` so tests can patch
    this single symbol without traversing the SDK request-context plumbing."""
    from src.okareo_client import _current_session_id as _impl

    return _impl()


def _read_tenant_id_from_jwt(jwt_string: str) -> str | None:
    """Decode a Frontegg JWT (without verifying signature — we just minted it,
    its authority comes from Frontegg returning it) and return the tenantId
    claim. Returns None if the JWT can't be decoded.

    Frontegg's tenant claim varies by token-template config: ``tenantId``,
    ``organization_id``, or ``tid``. We try all three.
    """
    try:
        import jwt as pyjwt
        claims = pyjwt.decode(jwt_string, options={"verify_signature": False})
    except Exception:
        return None
    if not isinstance(claims, dict):
        return None
    for key in ("tenantId", "organization_id", "tid", "tenant_id"):
        value = claims.get(key)
        if value:
            return str(value)
    return None


def register_tools(mcp: FastMCP) -> None:
    """Register list_tenants and switch_tenant with the FastMCP server."""

    @mcp.tool()
    async def list_tenants() -> str:
        """List every Okareo organization you have access to in this MCP session.

        The currently-active organization is marked ``is_current: true``. Use
        this before ``switch_tenant`` to discover available tenants by name.
        Only available on OAuth-authenticated sessions; on Bearer-API-key
        sessions returns ``tenant_selection_requires_oauth``.
        """
        credential = get_session_credential_optional()
        if credential is None or credential.kind == "api_key":
            return _error(
                "tenant_selection_requires_oauth",
                "Tenant selection requires OAuth sign-in; the API-key bearer "
                "path is single-organization.",
                data={"docs_url": _DOCS_URL},
            )

        session_id = _current_session_id()
        override = (
            tenant_state.get_override(session_id) if session_id else None
        )
        active = override.tenant_id if override is not None else credential.org_id
        source = "override" if override is not None else "jwt_default"

        try:
            tenants = await get_user_tenants(
                jwt=credential.api_key,
                session_id=session_id or "",
                frontegg_domain=os.environ.get("FRONTEGG_DOMAIN", "").strip(),
            )
        except TenantLookupAuthError:
            return _error(
                "tenant_lookup_failed",
                "Frontegg rejected the access token while listing tenants.",
                data={"retriable": True},
            )
        except TenantLookupFailed:
            return _error(
                "tenant_lookup_failed",
                "Could not fetch the tenant list from the identity provider.",
                data={"retriable": True},
            )

        return json.dumps(
            {
                "tenants": [
                    {"id": t.id, "name": t.name, "is_current": (t.id == active)}
                    for t in tenants
                ],
                "active_tenant_id": active,
                "active_tenant_source": source,
            }
        )

    @mcp.tool()
    async def switch_tenant(tenant_id: str) -> str:
        """Change which Okareo organization subsequent tool calls operate against.

        Pass ``tenant_id`` from ``list_tenants``. The MCP server mints a new
        Frontegg access token bound to the target tenant on your behalf, so
        subsequent tool calls scope to the right organization. The change is
        session-scoped and lasts only while this MCP transport stays connected.
        If the conversation is resumed after a restart (e.g., closing and
        reopening the copilot), re-call ``switch_tenant`` with the same
        ``tenant_id`` from the conversation transcript before the next
        tenant-scoped tool call — the server does not persist the selection.
        This tool is only available on OAuth-authenticated sessions.
        """
        credential = get_session_credential_optional()
        if credential is None or credential.kind == "api_key":
            return _error(
                "tenant_selection_requires_oauth",
                "Tenant selection requires OAuth sign-in; the API-key bearer "
                "path is single-organization.",
                data={"docs_url": _DOCS_URL},
            )

        session_id = _current_session_id()
        previous = credential.org_id
        if session_id:
            prior = tenant_state.get_override(session_id)
            if prior is not None:
                previous = prior.tenant_id

        # Validate against the allowed-tenant set BEFORE attempting the
        # Frontegg refresh (FR-025). JWT claim is the fast path; if absent,
        # fall back to Frontegg user-info.
        allowed_ids: set[str] = set(credential.allowed_tenants)
        target_name = ""
        if allowed_ids:
            if tenant_id not in allowed_ids:
                return _error(
                    "tenant_not_authorized",
                    "You do not have access to the requested tenant.",
                    data={},
                )
            # Optional name lookup for the response payload.
            try:
                tenants = await get_user_tenants(
                    jwt=credential.api_key,
                    session_id=session_id or "",
                    frontegg_domain=os.environ.get("FRONTEGG_DOMAIN", "").strip(),
                )
                for t in tenants:
                    if t.id == tenant_id:
                        target_name = t.name
                        break
            except (TenantLookupAuthError, TenantLookupFailed):
                target_name = ""
        else:
            # No claim — fall back to user-info for both validation and name.
            try:
                tenants = await get_user_tenants(
                    jwt=credential.api_key,
                    session_id=session_id or "",
                    frontegg_domain=os.environ.get("FRONTEGG_DOMAIN", "").strip(),
                )
            except TenantLookupAuthError:
                return _error(
                    "tenant_lookup_failed",
                    "Frontegg rejected the access token while validating the tenant.",
                    data={"retriable": True},
                )
            except TenantLookupFailed:
                return _error(
                    "tenant_lookup_failed",
                    "Could not validate the requested tenant against the identity provider.",
                    data={"retriable": True},
                )
            found = next((t for t in tenants if t.id == tenant_id), None)
            if found is None:
                return _error(
                    "tenant_not_authorized",
                    "You do not have access to the requested tenant.",
                    data={},
                )
            target_name = found.name

        # Mint a new access token bound to `tenant_id` via Frontegg's
        # tenant-switch refresh endpoint (2026-05-18 pivot). This requires
        # the user's refresh_token, captured at /oauth/callback and keyed
        # by JWT subject. If the cache misses, the user must re-sign-in to
        # populate it.
        user_sub = credential.subject or ""
        refresh_token = refresh_token_cache.get_token(user_sub)
        # Diagnostic so an operator can tell whether the failure is:
        #  (a) credential.subject is empty (JWT has no `sub` claim — odd), or
        #  (b) cache miss for a known sub (sign-in happened before the
        #      capture code was deployed, OR Frontegg didn't return a
        #      refresh_token at /oauth/callback — see the warning logged
        #      there).
        import sys as _sys
        _sys.stderr.write(
            f"[switch_tenant] sub={user_sub!r} cache_hit={bool(refresh_token)} "
            f"cache_size={refresh_token_cache._size_for_tests()}\n"
        )
        _sys.stderr.flush()
        if not refresh_token:
            return _error(
                "tenant_switch_unavailable",
                "The MCP server has no cached refresh token for your account. "
                "This usually happens after the server is restarted/redeployed: "
                "your existing MCP connection still works because its access "
                "token is cached client-side, but tenant switching needs a "
                "fresh server-side handshake. To fix: in your copilot, reload "
                "the Okareo MCP server (e.g., disable + re-enable it, or update "
                "the URL in your .mcp.json to bump a version query param like "
                "?v=N+1, then reload). The browser will open for sign-in; "
                "complete it and try `switch_tenant` again.",
                data={"docs_url": _DOCS_URL, "action": "reload_mcp_connection"},
            )

        frontegg_domain = os.environ.get("FRONTEGG_DOMAIN", "").strip()
        frontegg_client_id = os.environ.get("FRONTEGG_CLIENT_ID", "").strip()
        try:
            new_tokens = await refresh_with_tenant(
                refresh_token=refresh_token,
                tenant_id=tenant_id,
                frontegg_domain=frontegg_domain,
                frontegg_client_id=frontegg_client_id,
                # The current JWT is required as Bearer auth for Frontegg's
                # /me/tenant endpoint, which is the actual mechanism used
                # under the hood.
                current_access_token=credential.api_key,
            )
        except TenantSwitchAuthError:
            # Stale refresh token. Drop the cache entry so the next attempt
            # surfaces the same error rather than silently using a known-bad
            # token. User must re-sign-in.
            refresh_token_cache.forget_user(user_sub)
            return _error(
                "tenant_switch_unavailable",
                "Your session has expired or been revoked; please re-sign-in.",
                data={"docs_url": _DOCS_URL},
            )
        except TenantSwitchFailed:
            return _error(
                "tenant_switch_failed",
                "Could not switch tenants; the identity provider rejected the "
                "request. The existing tenant selection is unchanged.",
                data={"retriable": True},
            )

        new_access = str(new_tokens.get("access_token") or "")
        new_refresh = str(new_tokens.get("refresh_token") or "")
        if not new_access:
            return _error(
                "tenant_switch_failed",
                "Identity provider returned no access token.",
                data={"retriable": True},
            )

        # **Verify the new JWT actually carries the requested tenant.** Some
        # Frontegg accounts return 200 from /oauth/token but silently ignore
        # the `tenantId` extension parameter on refresh-token grants. In that
        # case the "new" access token is a fresh one for the OLD tenant. If
        # we cached it and downstream Okareo calls used it, the user would
        # see the original tenant's data despite the switch returning success.
        actual_tenant_id = _read_tenant_id_from_jwt(new_access)
        import sys as _sys
        _sys.stderr.write(
            f"[switch_tenant] frontegg returned jwt with tenantId="
            f"{actual_tenant_id!r}, requested={tenant_id!r}\n"
        )
        _sys.stderr.flush()
        if actual_tenant_id and actual_tenant_id != tenant_id:
            # Frontegg didn't honor the tenantId param. Don't write the
            # override (it would be a same-tenant token labelled as the
            # other tenant, producing misleading downstream behavior).
            return _error(
                "tenant_switch_not_supported",
                "The identity provider returned an access token for the "
                "original tenant rather than the requested one. This indicates "
                "Frontegg's /oauth/token endpoint does not honor the tenantId "
                "extension on this account/Application — server-side tenant "
                "switching requires a Frontegg config change (or a different "
                "switching endpoint).",
                data={
                    "requested_tenant_id": tenant_id,
                    "actual_tenant_id": actual_tenant_id,
                },
            )

        # Rotate the cached refresh token to the newly-issued one. Frontegg
        # invalidates the old one after every refresh.
        if new_refresh:
            refresh_token_cache.set_token(user_sub, new_refresh)

        # Atomic write of the override. Both fields land together so a
        # concurrent read can't see a half-applied state.
        if session_id:
            tenant_state.set_override(session_id, tenant_id, new_access)

        return json.dumps(
            {
                "active_tenant_id": tenant_id,
                "active_tenant_name": target_name,
                "previous_tenant_id": previous,
                "resume_hint": (
                    f"Session-scoped only — re-call switch_tenant('{tenant_id}') "
                    "at the start of any resumed conversation."
                ),
            }
        )
