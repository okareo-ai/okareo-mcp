"""``CombinedTokenVerifier`` — the single ``TokenVerifier`` for the remote MCP.

Accepts either a Frontegg-issued JWT (primary OAuth path) or an Okareo API
key (bearer-header path) on the same bearer slot. Each bearer takes exactly
one path (specs/048-api-key-shared-connections data-model.md):

- JWT whose unverified ``type`` is ``apiKey`` (minted by okareo-server) or
  ``tenantAccessToken`` (legacy, issued by Frontegg) → API-key path:
  okareo-server decides validity through ``api_key_validator``.
- Any other JWT → sign-in path: verify signature against the cached Frontegg
  JWKS; check ``iss``, ``aud``, ``exp``, scope, and the organization claim.
- ``okmcp_at_…`` → shared connection: decrypt, then the API-key path on the
  key inside (``shared_connection.py``).
- Anything else → refused as an invalid key. okareo-server accepts only
  JWTs, so asking it would be pointless.

On success the verifier binds the ``SessionCredential`` to the per-request
ContextVar and returns the SDK's ``AccessToken``.

Failures differ by path. The sign-in path returns ``None`` so the SDK emits
its own 401, unchanged since before 048. The API-key path raises
``InvalidAPIKeyError`` (401 that says what to fix) or
``CredentialUnavailableError`` (503), which ``OkareoFastMCP`` renders; an
okareo-server outage must never read as "your key is wrong".
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Awaitable, Callable

import jwt as pyjwt
from mcp.server.auth.provider import AccessToken, TokenVerifier
from starlette.authentication import AuthenticationError

from src.auth.api_key_verifier import KeyValidation, looks_like_jwt
from src.auth.context import CredentialKind, SessionCredential, set_session_credential
from src.auth.errors import CredentialUnavailableError, InvalidAPIKeyError
from src.auth.shared_connection import (
    ACCESS_PREFIX,
    InvalidSharedTokenError,
    SharedConnectionKeys,
    open_token,
)
from src.auth.jwks_cache import JWKSCache


_logger = logging.getLogger(__name__)


def _diag(line: str) -> None:
    """Single-line diagnostic to stderr, visible in docker logs.

    Used for the JWT-rejection paths in ``_verify_jwt`` so an operator can
    tell **why** a token was rejected (iss mismatch, aud mismatch, missing
    organization_id claim, etc.) without enabling DEBUG logging.
    """
    print(line, file=sys.stderr, flush=True)


ApiKeyValidator = Callable[[str], Awaitable[KeyValidation]]

# `apiKey`: minted by okareo-server since 2026-06-09. `tenantAccessToken`:
# issued by Frontegg before that, still accepted by okareo-server.
_API_KEY_TOKEN_TYPES = frozenset({"apiKey", "tenantAccessToken"})

_RECONNECT = "This connection has expired or is not recognised; reconnect."


def _unverified_type(token: str) -> str | None:
    """The JWT's ``type`` claim, read without verifying anything.

    Only used to pick a path; each path then verifies on its own terms.
    """
    try:
        unverified = pyjwt.decode(token, options={"verify_signature": False})
    except pyjwt.InvalidTokenError:
        return None
    if not isinstance(unverified, dict):
        return None
    token_type = unverified.get("type")
    return token_type if isinstance(token_type, str) else None


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
        api_key_validator: ApiKeyValidator,
        required_scope: str = "okareo:use",
        additional_audiences: list[str] | None = None,
        shared_keys: SharedConnectionKeys | None = None,
    ) -> None:
        self._issuer = _normalize_url(issuer_url)
        self._resource = _normalize_url(resource_server_url)
        self._jwks = jwks_cache
        self._validate_api_key = api_key_validator
        # None when MCP_DCR_SIGNING_KEY is not configured: a token sealed
        # with an ephemeral per-instance key could not be opened elsewhere.
        self._shared_keys = shared_keys
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
            if token.startswith(ACCESS_PREFIX):
                return await self._verify_shared(token)
            if not looks_like_jwt(token):
                raise InvalidAPIKeyError()
            if _unverified_type(token) in _API_KEY_TOKEN_TYPES:
                # API keys carry no `exp` and are not signed by Frontegg, so
                # the JWKS path would reject every one of them (048 R1).
                return await self._verify_api_key(token)
            return await self._verify_jwt(token)
        except AuthenticationError:
            raise
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
        email = claims.get("email")
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
            email=str(email) if email else None,
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

    async def _verify_shared(self, token: str) -> AccessToken:
        if self._shared_keys is None:
            raise InvalidAPIKeyError(_RECONNECT)
        try:
            payload = open_token(token, self._shared_keys, "at")
        except InvalidSharedTokenError:
            raise InvalidAPIKeyError(_RECONNECT) from None
        # The key inside is checked on every request, so revoking it cuts the
        # connection off however long the envelope still has to run.
        return await self._verify_api_key(
            payload["k"], kind="shared_api_key", presented=token
        )

    async def _verify_api_key(
        self,
        token: str,
        kind: CredentialKind = "api_key",
        presented: str | None = None,
    ) -> AccessToken:
        try:
            result = await self._validate_api_key(token)
        except Exception as exc:
            # Fail closed, and as "could not check" rather than "invalid":
            # the key may well be fine.
            _diag(
                f"[verifier] API-key path: validator raised {type(exc).__name__}"
            )
            raise CredentialUnavailableError() from None

        if result.outcome == "unavailable":
            raise CredentialUnavailableError()
        if result.outcome != "valid" or not result.tenant_id:
            raise InvalidAPIKeyError()

        expires_at = (
            datetime.fromtimestamp(result.expires_at, tz=timezone.utc)
            if result.expires_at is not None
            else None
        )
        credential = SessionCredential(
            kind=kind,
            api_key=token,
            org_id=result.tenant_id,
            subject=result.subject,
            expires_at=expires_at,
        )
        set_session_credential(credential)
        return AccessToken(
            token=presented or token,
            client_id=result.tenant_id,
            scopes=list(credential.scopes),
            expires_at=result.expires_at,
            resource=self._resource,
        )
