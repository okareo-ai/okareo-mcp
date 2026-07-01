"""Per-request credential context for the remote (HTTP-mode) MCP server.

A `SessionCredential` is the unified shape that every tool, the analytics
layer, and the throttle middleware consume — regardless of whether the caller
authenticated via OAuth (Frontegg JWT) or via the bearer-API-key fallback.

The credential is set once per request, inside `CombinedTokenVerifier`, and
read back by tools via `get_session_credential()`. Lifetime is bounded by the
HTTP request via a ContextVar; nothing persists across requests.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


CredentialKind = Literal["oauth", "api_key"]


class CredentialMissingError(RuntimeError):
    """Raised when a tool requests the session credential outside a request scope.

    This indicates a wiring bug: either HTTP-mode tools are being called
    without a valid auth context, or stdio tools are reaching into HTTP-only
    helpers. The error message intentionally carries no credential data.
    """


@dataclass(frozen=True)
class SessionCredential:
    """The authenticated principal for one MCP HTTP request.

    Attributes are documented in ``specs/020-remote-mcp/data-model.md``.
    """

    kind: CredentialKind
    api_key: str
    org_id: str
    subject: str | None = None
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = field(default_factory=lambda: ("okareo:use",))
    # Optional set of tenant IDs the user is allowed to switch into, as
    # carried by the JWT's `tenantIds[]` claim when the Frontegg token
    # template includes it. Empty tuple = claim absent (the tools layer
    # falls back to a Frontegg user-info call for validation). Used by
    # `switch_tenant` for FR-025 fast-path authorization.
    allowed_tenants: tuple[str, ...] = field(default_factory=tuple)


_credential_var: contextvars.ContextVar[SessionCredential | None] = (
    contextvars.ContextVar("okareo_session_credential", default=None)
)


def set_session_credential(credential: SessionCredential) -> contextvars.Token:
    """Bind a credential to the current request context.

    Returns the ContextVar token so callers can reset it explicitly if needed
    (typically not required — the var dies with the request context).
    """
    return _credential_var.set(credential)


def get_session_credential() -> SessionCredential:
    """Return the credential bound to the current request context.

    Raises:
        CredentialMissingError: If no credential is set (caller is outside
            an authenticated HTTP request).
    """
    credential = _credential_var.get()
    if credential is None:
        raise CredentialMissingError(
            "No session credential is bound to the current request context. "
            "This helper must be called from inside an authenticated "
            "HTTP-mode MCP request."
        )
    return credential


def get_session_credential_optional() -> SessionCredential | None:
    """Return the credential if one is bound, else None.

    Use this from code that must work in both stdio and HTTP modes (e.g.,
    `get_okareo_client()` chooses between request-scoped and env-scoped paths
    based on whether a credential is present).
    """
    return _credential_var.get()


def _reset_for_tests() -> None:
    """Test helper: clear the ContextVar in the current context.

    Intended for use by ``tests/unit/auth/test_context.py`` only. Production
    code MUST NOT call this — request contexts are isolated by design.
    """
    _credential_var.set(None)
