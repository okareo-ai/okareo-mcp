"""Embedded-login handoff endpoint (feature 021-embedded-login).

Single new route: ``POST /oauth/handoff``. Accepts a Frontegg-issued JWT +
refresh_token from the embedded login page (after the user signed in or
signed up against Frontegg's identity REST API directly), validates the JWT
against the existing JWKS cache, populates the matching
``PendingAuthorization`` record from the OAuth Proxy, and returns the URL
the page should navigate the user's browser to (the MCP-client's registered
``redirect_uri`` with our minted code).

Wire-format contract: see
``specs/021-embedded-login/contracts/handoff-endpoint.openapi.yaml``.

CSRF defense: the route enforces ``Origin`` and (when present)
``Sec-Fetch-Site`` headers. Production allows only the configured
``MCP_RESOURCE_SERVER_URL`` origin. Dev mode (``MCP_EMBEDDED_LOGIN_DEV_MODE=true``)
also accepts ``http://localhost:3000`` for the Next.js dev server.

The pending code is one-time-use (``OAuthStateStore.consume_pending`` deletes
on success). The Frontegg JWT shape is **not** trusted blindly: signature,
issuer, audience, and exp are all verified before any state mutation.

In stdio mode this module is NEVER imported (see ``src/server.py`` HTTP-mode
gating) so the airgapped guarantee from specs/020-remote-mcp/spec.md
Assumptions is preserved.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from dataclasses import dataclass

import jwt as pyjwt
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.auth.jwks_cache import JWKSCache
from src.auth.oauth_proxy import ProxyConfig
from src.auth.oauth_state import OAuthStateStore


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddedHandoffRequest:
    """Validated form of the inbound JSON body. Parsed once per request."""

    pending_code: str
    frontegg_access_token: str
    frontegg_refresh_token: str
    frontegg_expires_in: int


@dataclass(frozen=True)
class EmbeddedHandoffResponse:
    """Response shape — single field, the URL the page navigates to next."""

    redirect_url: str


# ---------------------------------------------------------------------------
# Error envelope (RFC 6749 §5.2 style)
# ---------------------------------------------------------------------------


def _error(status: int, code: str, description: str) -> JSONResponse:
    return JSONResponse(
        {"error": code, "error_description": description}, status_code=status
    )


# ---------------------------------------------------------------------------
# Body validation
# ---------------------------------------------------------------------------


_PENDING_CODE_PREFIX = "okm_"


def _parse_body(payload: dict) -> EmbeddedHandoffRequest | JSONResponse:
    """Strict JSON-body validation. Returns the DTO or an error response."""
    if not isinstance(payload, dict):
        return _error(400, "invalid_request", "body must be a JSON object")

    pending_code = payload.get("pending_code")
    if not isinstance(pending_code, str) or not pending_code.startswith(
        _PENDING_CODE_PREFIX
    ):
        return _error(400, "invalid_request", "pending_code is missing or malformed")

    access_token = payload.get("frontegg_access_token")
    if not isinstance(access_token, str) or len(access_token) < 32:
        return _error(
            400, "invalid_request", "frontegg_access_token is required"
        )

    refresh_token = payload.get("frontegg_refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        return _error(
            400, "invalid_request", "frontegg_refresh_token is required"
        )

    expires_in = payload.get("frontegg_expires_in")
    if not isinstance(expires_in, int) or expires_in <= 0:
        return _error(
            400, "invalid_request", "frontegg_expires_in must be a positive integer"
        )

    return EmbeddedHandoffRequest(
        pending_code=pending_code,
        frontegg_access_token=access_token,
        frontegg_refresh_token=refresh_token,
        frontegg_expires_in=expires_in,
    )


# ---------------------------------------------------------------------------
# Origin / Sec-Fetch-Site enforcement (CSRF defense, FR-010)
# ---------------------------------------------------------------------------


def _allowed_origins(config: ProxyConfig) -> list[str]:
    """Return the set of acceptable ``Origin`` header values.

    Production: just the canonical MCP resource server URL.
    Dev mode (``MCP_EMBEDDED_LOGIN_DEV_MODE=true``): also accept
    ``http://localhost:3000`` (Next.js dev server) and ``http://localhost:8080``
    (Python server reflexive calls).
    """
    canonical = config.resource_server_url.rstrip("/")
    allowed = [canonical]
    if os.environ.get("MCP_EMBEDDED_LOGIN_DEV_MODE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        allowed.extend(
            [
                "http://localhost:3000",
                "http://127.0.0.1:3000",
                "http://localhost:8080",
                "http://127.0.0.1:8080",
            ]
        )
    return allowed


def _check_origin(request: Request, config: ProxyConfig) -> JSONResponse | None:
    """CSRF guard. Returns an error response if the request must be rejected;
    None if the origin checks pass.
    """
    origin = request.headers.get("origin")
    if not origin:
        return _error(403, "forbidden", "cross-origin not permitted")
    if origin.rstrip("/") not in [a.rstrip("/") for a in _allowed_origins(config)]:
        _logger.warning(
            "Handoff rejected: Origin=%s not in allow-list", origin
        )
        return _error(403, "forbidden", "cross-origin not permitted")

    # Sec-Fetch-Site: when present (modern browsers), MUST be "same-origin"
    # in production (the canonical origin) or absent/cross-site in dev (when
    # the page lives on a different origin than the server).
    sfs = request.headers.get("sec-fetch-site")
    if sfs is not None:
        canonical = config.resource_server_url.rstrip("/")
        is_canonical = origin.rstrip("/") == canonical
        if is_canonical and sfs.lower() not in ("same-origin", "none"):
            _logger.warning(
                "Handoff rejected: Sec-Fetch-Site=%s on same-origin request (Origin=%s)",
                sfs,
                origin,
            )
            return _error(403, "forbidden", "cross-origin not permitted")
        # If origin is a dev-mode allowlisted localhost, Sec-Fetch-Site WILL
        # be "cross-site" — that's expected; the Origin check already gated it.

    return None


def _maybe_add_cors_headers(response: Response, request: Request, config: ProxyConfig) -> Response:
    """In dev mode only: add Access-Control-Allow-Origin so the browser accepts
    the cross-origin response. No-op in production (where Origin == resource URL).
    """
    origin = request.headers.get("origin", "")
    if not origin:
        return response
    canonical = config.resource_server_url.rstrip("/")
    if origin.rstrip("/") == canonical:
        return response  # same-origin; no CORS needed
    allowed = [a.rstrip("/") for a in _allowed_origins(config)]
    if origin.rstrip("/") in allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response


# ---------------------------------------------------------------------------
# JWT validation (defense-in-depth)
# ---------------------------------------------------------------------------


async def _validate_frontegg_jwt(
    token: str,
    issuer: str,
    audience_aliases: list[str],
    jwks_cache: JWKSCache,
) -> bool:
    """Verify signature + iss + aud + exp. Returns True on success.

    We do NOT inspect ``organization_id``/``tenantId`` here — that's a
    runtime concern of ``CombinedTokenVerifier`` on subsequent /mcp calls.
    The handoff just refuses to populate the pending record with a token
    that fails basic Frontegg-signed-and-valid checks.
    """
    if not issuer or not audience_aliases:
        _logger.error(
            "Handoff JWT validation requested with empty issuer/audience config"
        )
        return False

    try:
        header = pyjwt.get_unverified_header(token)
    except pyjwt.InvalidTokenError as exc:
        _logger.warning("Handoff JWT header parse failed: %s", exc)
        return False

    kid = header.get("kid")
    if not kid:
        _logger.warning("Handoff JWT missing `kid`")
        return False

    try:
        jwk = await jwks_cache.get_key(kid)
    except Exception as exc:
        _logger.warning("Handoff JWKS lookup raised: %s", type(exc).__name__)
        return False
    if not jwk:
        _logger.warning("Handoff JWT: no JWK for kid=%s", kid)
        return False

    try:
        public_key = pyjwt.algorithms.RSAAlgorithm.from_jwk(jwk)
    except (ValueError, TypeError, pyjwt.InvalidKeyError) as exc:
        _logger.warning("Handoff JWT: failed to load JWK as public key: %s", exc)
        return False

    try:
        pyjwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            issuer=issuer.rstrip("/"),
            audience=audience_aliases,
            options={"require": ["exp", "iss", "aud"]},
        )
    except pyjwt.InvalidTokenError as exc:
        _logger.warning(
            "Handoff JWT decode/validate failed: %s: %s", type(exc).__name__, exc
        )
        return False

    return True


# ---------------------------------------------------------------------------
# Route factory
# ---------------------------------------------------------------------------


def make_handoff_route(
    store: OAuthStateStore,
    config: ProxyConfig,
    jwks_cache: JWKSCache,
    *,
    audience_aliases: list[str],
):
    """Build the Starlette handler for ``POST /oauth/handoff``.

    Args:
        store: shared ``OAuthStateStore`` (the SAME instance used by the
            OAuth Proxy routes — must be a single per-process instance).
        config: ``ProxyConfig`` with the resource_server_url used for
            Origin checks and to construct the absolute redirect URLs.
        jwks_cache: same ``JWKSCache`` used by ``CombinedTokenVerifier``;
            shared so we don't double-fetch on every handoff.
        audience_aliases: list of acceptable ``aud`` claim values (mirrors
            the verifier's ``additional_audiences``, plus the resource URL
            with and without trailing slash).
    """

    async def _route(request: Request) -> Response:
        # 1a. OPTIONS preflight — dev mode only (production is same-origin so
        # browsers never preflight). Emit minimal CORS headers for the
        # localhost:3000 Next.js dev server. The actual Origin check on the
        # subsequent POST is still enforced by `_check_origin`.
        if request.method == "OPTIONS":
            origin = request.headers.get("origin", "")
            allowed = [a.rstrip("/") for a in _allowed_origins(config)]
            if origin.rstrip("/") in allowed and origin.rstrip(
                "/"
            ) != config.resource_server_url.rstrip("/"):
                return Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Origin": origin,
                        "Access-Control-Allow-Methods": "POST, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type",
                        "Access-Control-Max-Age": "600",
                        "Vary": "Origin",
                    },
                )
            return Response(status_code=204)

        # 1b. Reject non-POST methods.
        if request.method != "POST":
            return _maybe_add_cors_headers(
                _error(405, "method_not_allowed", "POST only"), request, config
            )

        content_type = request.headers.get("content-type", "").split(";")[0].strip()
        if content_type != "application/json":
            return _maybe_add_cors_headers(
                _error(
                    415,
                    "unsupported_media_type",
                    "Content-Type must be application/json",
                ),
                request,
                config,
            )

        # 2. CSRF / Origin enforcement.
        origin_err = _check_origin(request, config)
        if origin_err is not None:
            return origin_err  # do NOT emit CORS — origin was rejected

        # 3. Parse body.
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            _logger.warning("Handoff body JSON parse failed: %s", exc)
            return _maybe_add_cors_headers(
                _error(400, "invalid_request", "body is not valid JSON"),
                request,
                config,
            )

        parsed = _parse_body(payload)
        if isinstance(parsed, JSONResponse):
            return _maybe_add_cors_headers(parsed, request, config)
        body: EmbeddedHandoffRequest = parsed

        # 4. Look up pending — peek before validating JWT so an unknown code
        # is the cheaper rejection.
        pending = await store.get_pending(body.pending_code)
        if pending is None:
            return _maybe_add_cors_headers(
                _error(400, "invalid_grant", "unknown or expired authorization"),
                request,
                config,
            )

        # 5. Validate the Frontegg JWT.
        ok = await _validate_frontegg_jwt(
            body.frontegg_access_token,
            issuer=_frontegg_issuer(config),
            audience_aliases=audience_aliases,
            jwks_cache=jwks_cache,
        )
        if not ok:
            return _maybe_add_cors_headers(
                _error(400, "invalid_token", "access token failed validation"),
                request,
                config,
            )

        # 6. Populate the pending record. The OAuthStateStore method is
        # idempotent-by-failure (returns False on unknown/expired) — we
        # already checked above so a False here means a race with another
        # handoff for the same code.
        populated = await store.populate_pending(
            body.pending_code,
            frontegg_jwt=body.frontegg_access_token,
            frontegg_refresh_token=body.frontegg_refresh_token,
            frontegg_expires_in=body.frontegg_expires_in,
        )
        if not populated:
            return _maybe_add_cors_headers(
                _error(400, "invalid_grant", "unknown or expired authorization"),
                request,
                config,
            )

        # 7. Synthesize the URL the page should navigate to: the MCP-client's
        # registered redirect_uri with `code` and (when present) `state`.
        # `code` is the pending-auth code we minted at /oauth/authorize time;
        # the MCP client redeems it at /oauth/token (returning the JWT we
        # just stored, passthrough).
        params = {"code": body.pending_code}
        if pending.state_to_client:
            params["state"] = pending.state_to_client

        sep = "&" if "?" in pending.redirect_uri else "?"
        redirect_url = pending.redirect_uri + sep + urllib.parse.urlencode(params)

        _logger.info(
            "Handoff complete: pending_code_prefix=%s, redirecting to MCP client",
            body.pending_code[:8],
        )

        return _maybe_add_cors_headers(
            JSONResponse(
                EmbeddedHandoffResponse(redirect_url=redirect_url).__dict__,
                status_code=200,
            ),
            request,
            config,
        )

    return _route


def _frontegg_issuer(config: ProxyConfig) -> str:
    """Resolve the expected JWT ``iss`` claim.

    Mirrors ``src/server.py::_frontegg_issuer``. When ``FRONTEGG_ISSUER`` env
    var is set, use it verbatim (handles the Frontegg custom-domain case
    where iss is a different host than the one JWKS is fetched from).
    Otherwise fall back to ``https://${config.frontegg_domain}``.
    """
    override = os.environ.get("FRONTEGG_ISSUER", "").strip()
    if override:
        return override.rstrip("/")
    domain = (config.frontegg_domain or "").strip()
    return f"https://{domain}" if domain else ""
