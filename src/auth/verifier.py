"""``CombinedTokenVerifier`` — the single ``TokenVerifier`` for the remote MCP.

Accepts either a Frontegg-issued JWT (primary OAuth path) or an Okareo API
key (fallback bearer-header path) on the same bearer slot. The shape choice
is decided per request by a JWT-syntax heuristic, then routed accordingly:

- 3-segment dot-separated string → JWT path: verify signature against the
  cached Frontegg JWKS; check ``iss``, ``aud``, ``exp``, scope, and presence
  of the ``organization_id`` claim.
- Anything else → API-key path: hand the value to the supplied
  ``api_key_resolver`` (typically ``OkareoAPIKeyVerifier.verify``).

Either way, on success the verifier:
1. Binds the resulting ``SessionCredential`` to the per-request ContextVar.
2. Returns the SDK's ``AccessToken`` so the auth middleware lets the
   request through.

The verifier MUST NEVER raise: any failure (signature error, expired
token, unreachable Okareo backend, etc.) returns ``None`` so the SDK can
emit a clean 401 with the spec-mandated ``WWW-Authenticate`` header.
"""

from __future__ import annotations

import logging
import sys
from typing import Awaitable, Callable

import jwt as pyjwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

from src.auth.context import SessionCredential, set_session_credential
from src.auth.jwks_cache import JWKSCache


_logger = logging.getLogger(__name__)


def _diag(line: str) -> None:
    """Single-line diagnostic to stderr, visible in docker logs.

    Used for the JWT-rejection paths in ``_verify_jwt`` so an operator can
    tell **why** a token was rejected (iss mismatch, aud mismatch, missing
    organization_id claim, etc.) without enabling DEBUG logging.
    """
    print(line, file=sys.stderr, flush=True)


ApiKeyResolver = Callable[[str], Awaitable[SessionCredential | None]]


def _looks_like_jwt(token: str) -> bool:
    """Cheap shape check: three base64url segments separated by dots."""
    parts = token.split(".")
    if len(parts) != 3:
        return False
    return all(p and all(c.isalnum() or c in "-_" for c in p) for p in parts)


def _is_tenant_access_token(token: str) -> bool:
    """True if the JWT payload carries Frontegg's `type: tenantAccessToken`.

    Okareo issues "API keys" as Frontegg tenant access tokens — JWT-shaped,
    long-lived (no `exp` claim), and meant to be validated against the
    Okareo backend rather than via JWT cryptography. They share the JWT
    shape with short-lived OAuth user tokens but must take a different
    verification path: handing them to ``pyjwt.decode(require=["exp"])``
    fails immediately. The `type` claim is the canonical signal.
    """
    try:
        unverified = pyjwt.decode(token, options={"verify_signature": False})
    except pyjwt.InvalidTokenError:
        return False
    return isinstance(unverified, dict) and unverified.get("type") == "tenantAccessToken"


def _normalize_url(url: str) -> str:
    """Drop trailing slash for audience comparison."""
    return url.rstrip("/")


class CombinedTokenVerifier(TokenVerifier):
    """Dual-mode verifier: Frontegg JWT (primary) or Okareo API key (fallback)."""

    def __init__(
        self,
        *,
        issuer_url: str,
        resource_server_url: str,
        jwks_cache: JWKSCache,
        api_key_resolver: ApiKeyResolver,
        required_scope: str = "okareo:use",
        additional_audiences: list[str] | None = None,
    ) -> None:
        self._issuer = _normalize_url(issuer_url)
        self._resource = _normalize_url(resource_server_url)
        self._jwks = jwks_cache
        self._resolve_api_key = api_key_resolver
        self._required_scope = required_scope
        # Additional acceptable `aud` claim values beyond the resource URL.
        # The MCP spec (RFC 8707) wants aud = resource server URL, but
        # upstream IdPs may set aud differently — Frontegg, for example,
        # sets aud = <vendor_id>. We accept either form so our verifier
        # works against the Frontegg-issued JWTs the OAuth Proxy passes
        # through to MCP clients. Empty list = strict MCP-spec behavior.
        self._additional_audiences = list(additional_audiences or [])

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            if _looks_like_jwt(token):
                # Frontegg tenant access tokens are JWT-shaped Okareo API
                # keys (long-lived, no `exp`). Route them through the
                # API-key path so the Okareo backend is the source of
                # truth on validity — JWT validation would reject them
                # for the missing `exp` claim.
                if _is_tenant_access_token(token):
                    return await self._verify_api_key(token)
                return await self._verify_jwt(token)
            return await self._verify_api_key(token)
        except Exception as exc:
            # Defensive: any unexpected exception becomes a 401, not a 500.
            _logger.warning(
                "Token verification raised unexpectedly (%s); returning None",
                type(exc).__name__,
            )
            return None

    async def _verify_jwt(self, token: str) -> AccessToken | None:
        try:
            unverified_header = pyjwt.get_unverified_header(token)
        except pyjwt.InvalidTokenError as exc:
            _diag(f"[verifier] JWT header parse failed: {exc}")
            return None

        kid = unverified_header.get("kid")
        if not kid:
            _diag("[verifier] JWT header missing `kid`")
            return None

        try:
            jwk = await self._jwks.get_key(kid)
        except Exception as exc:
            _diag(f"[verifier] JWKS lookup raised: {type(exc).__name__}: {exc}")
            return None

        if not jwk:
            _diag(f"[verifier] No JWK found for kid={kid!r}")
            return None

        try:
            public_key = pyjwt.algorithms.RSAAlgorithm.from_jwk(jwk)
        except (ValueError, TypeError, pyjwt.InvalidKeyError) as exc:
            _diag(f"[verifier] Failed to load public key from JWK: {exc}")
            return None

        # Peek at unverified claims so we can produce useful diagnostics
        # without exposing the raw token. The signature is still validated
        # by the subsequent pyjwt.decode call.
        try:
            unverified_claims = pyjwt.decode(
                token, options={"verify_signature": False}
            )
        except pyjwt.InvalidTokenError as exc:
            _diag(f"[verifier] JWT body parse failed: {exc}")
            return None

        # Audience list: MCP-spec-canonical resource URL (with and without
        # trailing slash) + any additional aliases (e.g., Frontegg vendor_id).
        acceptable_audiences = [
            self._resource,
            self._resource + "/",
            *self._additional_audiences,
        ]
        try:
            claims = pyjwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=acceptable_audiences,
                options={"require": ["exp", "iss", "aud"]},
            )
        except pyjwt.InvalidTokenError as exc:
            # Show what we received vs what we expected so the operator can
            # diagnose iss/aud/exp mismatches in one log line. We log the
            # claim values (not credentials) — these are public-by-design.
            _diag(
                f"[verifier] JWT decode/validate failed ({type(exc).__name__}: {exc}). "
                f"Expected iss={self._issuer!r} | actual iss={unverified_claims.get('iss')!r}; "
                f"Acceptable aud={acceptable_audiences!r} | actual aud={unverified_claims.get('aud')!r}; "
                f"exp={unverified_claims.get('exp')!r}"
            )
            return None

        org_id = claims.get("organization_id") or claims.get("tenantId")
        if not org_id:
            _diag(
                "[verifier] JWT validated but no organization_id/tenantId claim. "
                f"Available claims: {sorted(claims.keys())}"
            )
            return None

        scope_str = claims.get("scope", "")
        scopes = tuple(s for s in scope_str.split() if s)
        # Scope enforcement is opt-in: if `required_scope` is unset/empty,
        # we accept any token that passes the other checks. This is the v1
        # default — Frontegg doesn't issue MCP-specific scopes by default,
        # and we don't yet do per-tool scope gating. The check stays here
        # so it can be turned on (via env var or constructor arg) once the
        # token template + per-tool scope policy are in place.
        if self._required_scope and self._required_scope not in scopes:
            _diag(
                f"[verifier] JWT validated but missing required scope {self._required_scope!r}. "
                f"Token scopes: {list(scopes)}"
            )
            return None

        subject = claims.get("sub")
        # API key for downstream Okareo SDK calls: for the OAuth path we
        # forward the JWT itself; the Okareo backend accepts JWTs (issued by
        # Frontegg under the same identity) as authentication.
        api_key_for_sdk = token

        # Allowed-tenant set for `switch_tenant`'s FR-025 validation, when
        # the Frontegg token template includes a `tenantIds[]` claim. If the
        # claim is absent or wrong-shape, leave as empty tuple — the tools
        # layer falls back to a Frontegg user-info call.
        raw_tenants = claims.get("tenantIds")
        allowed_tenants: tuple[str, ...] = ()
        if isinstance(raw_tenants, list):
            allowed_tenants = tuple(str(t) for t in raw_tenants if t)

        credential = SessionCredential(
            kind="oauth",
            api_key=api_key_for_sdk,
            org_id=str(org_id),
            subject=str(subject) if subject else None,
            scopes=scopes,
            allowed_tenants=allowed_tenants,
        )
        set_session_credential(credential)

        exp = claims.get("exp")
        return AccessToken(
            token=token,
            client_id=str(subject) if subject else str(org_id),
            scopes=list(scopes),
            expires_at=int(exp) if exp else None,
            resource=self._resource,
        )

    async def _verify_api_key(self, token: str) -> AccessToken | None:
        try:
            credential = await self._resolve_api_key(token)
        except Exception as exc:
            _diag(
                f"[verifier] API-key path: resolver raised "
                f"{type(exc).__name__}: {exc!r}"
            )
            return None

        if credential is None:
            _diag(
                "[verifier] API-key path: resolver returned None — Okareo "
                "rejected the key, the account has no projects, or "
                "OKAREO_BASE_URL points at the wrong environment."
            )
            return None

        set_session_credential(credential)
        return AccessToken(
            token=token,
            client_id=credential.org_id,
            scopes=list(credential.scopes),
            expires_at=None,
            resource=self._resource,
        )
