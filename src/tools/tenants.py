"""Tenant-management MCP tools.

Two tools, both OAuth-only:

- ``list_tenants()`` — return every Frontegg tenant the authenticated user
  belongs to. Each entry: ``{id, name, is_current}``. Top-level fields
  ``active_tenant_id`` and ``active_tenant_source`` (always ``"jwt_default"``
  now that selection happens at sign-in) describe the active organization.

- ``switch_tenant(tenant_id)`` — **no longer changes the active organization**
  (feature 030). Organization selection now happens during sign-in: the token
  the co-pilot receives is already scoped to the organization the user
  authorized. To change organizations, the user reconnects/re-authenticates
  the MCP and picks a different organization. This tool returns that guidance
  rather than attempting a fragile server-side token re-mint. See
  ``specs/030-tenant-selection/contracts/switch-tenant-tool.md``.

On Bearer-API-key sessions both tools return
``tenant_selection_requires_oauth`` with a docs pointer.
"""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from src.auth.context import get_session_credential_optional
from src.auth.frontegg_user_info import (
    TenantLookupAuthError,
    TenantLookupFailed,
    get_user_tenants,
)

_DOCS_URL = "https://docs.okareo.com/docs/mcp/remote#oauth-sign-in"


def _error(code: str, message: str, data: dict[str, Any] | None = None) -> str:
    """Format an MCP tool error consistent with the no-PII contract (no
    rejected inputs in the data payload)."""
    return json.dumps(
        {"error": {"code": code, "message": message, "data": data or {}}}
    )


def _current_session_id() -> str | None:
    """Bridge to ``src.okareo_client._current_session_id`` so tests can patch
    this single symbol without traversing the SDK request-context plumbing."""
    from src.okareo_client import _current_session_id as _impl

    return _impl()


def register_tools(mcp: FastMCP) -> None:
    """Register list_tenants and switch_tenant with the FastMCP server."""

    @mcp.tool(
        title="List Tenants",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_tenants() -> str:
        """List every Okareo organization you have access to in this MCP session.

        The currently-active organization is marked ``is_current: true``. The
        active organization is determined at sign-in (the token this session
        presents is already scoped to it). Only available on OAuth-authenticated
        sessions; on Bearer-API-key sessions returns
        ``tenant_selection_requires_oauth``.
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
        # The active organization is whatever the presented JWT is scoped to
        # (feature 030 — no per-session override exists anymore).
        active = credential.org_id

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
                "active_tenant_source": "jwt_default",
            }
        )

    @mcp.tool(
        title="Switch Tenant",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def switch_tenant(tenant_id: str) -> str:
        """Change which Okareo organization your session operates against.

        Organization selection now happens **during sign-in** (feature 030):
        when you connect the Okareo MCP you choose which organization to
        authorize, and the credential this session uses is already scoped to
        it. This tool therefore no longer changes the active organization — to
        switch, reconnect/re-authenticate the Okareo MCP from your copilot and
        select a different organization when prompted. Use ``list_tenants`` to
        see which organization is currently active.
        """
        credential = get_session_credential_optional()
        if credential is None or credential.kind == "api_key":
            return _error(
                "tenant_selection_requires_oauth",
                "Tenant selection requires OAuth sign-in; the API-key bearer "
                "path is single-organization.",
                data={"docs_url": _DOCS_URL},
            )

        payload: dict[str, Any] = {
            "action": "reauthenticate_to_change_tenant",
            "message": (
                "Changing your active Okareo organization now happens during "
                "sign-in. To switch organizations, reconnect/re-authenticate "
                "the Okareo MCP from your copilot; you'll be asked which "
                "organization to authorize. This tool no longer changes the "
                "active organization."
            ),
            "docs_url": _DOCS_URL,
        }
        if credential.org_id:
            payload["current_tenant_id"] = credential.org_id
        return json.dumps(payload)
