"""OAuth Proxy: the MCP server acts as a full OAuth 2.1 AS to MCP clients.

Routes mounted by ``register_oauth_proxy_routes()``:

- ``GET /.well-known/oauth-authorization-server`` — RFC 8414 AS metadata
  pointing at our own /oauth/authorize, /oauth/token, /register.
- ``GET /oauth/authorize`` — entry point for the OAuth code flow. Validates
  the MCP-client request, creates a ``PendingAuthorization``, and 302s the
  browser to the **embedded login page** at ``/login?pending=<code>`` (feature
  021-embedded-login, FR-002). The page authenticates the user against
  Frontegg directly and posts the resulting JWT back to /oauth/handoff.
- ``GET /oauth/callback`` — retained as defense-in-depth (specs/021-embedded-login
  research.md R4). Not exercised by the embedded flow; remains functional in
  case an operator-misconfigured Frontegg Application ever redirects here
  (e.g., during rollback). Exchanges a Frontegg code for a JWT, populates
  the pending record, and 302s the browser to the MCP client's redirect_uri.
- ``POST /oauth/token`` — MCP client redeems our code (with PKCE) and
  receives Frontegg's JWT verbatim (token passthrough).

The companion ``/oauth/handoff`` route used by the new embedded path is
defined in ``src/auth/embedded_handoff.py``; it shares the same
``OAuthStateStore`` instance.

State lives in ``oauth_state.OAuthStateStore`` — per-instance, no persistence.

See: specs/020-remote-mcp/spec.md FR-016/019/020,
specs/020-remote-mcp/contracts/mcp-auth-contract.md,
specs/021-embedded-login/spec.md FR-002 / FR-004 / FR-011.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import sys
import urllib.parse
from dataclasses import dataclass

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from src.auth.oauth_state import OAuthStateStore


_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProxyConfig:
    """Static configuration for the OAuth Proxy.

    Note: there is no ``frontegg_api_key`` field. Since the 2026-05-18
    PKCE-upstream clarification, the proxy authenticates to Frontegg as a
    public OAuth client — `client_id` + PKCE `code_verifier`, no secret.
    """

    resource_server_url: str  # canonical URL of this MCP, e.g. https://tools.okareo.com
    frontegg_domain: str  # e.g. okareo.frontegg.com (no scheme)
    frontegg_client_id: str  # our OAuth client at Frontegg

    @property
    def self_callback_url(self) -> str:
        return f"{self.resource_server_url.rstrip('/')}/oauth/callback"

    @property
    def frontegg_authorize_url(self) -> str:
        return f"https://{self.frontegg_domain}/oauth/authorize"

    @property
    def frontegg_token_url(self) -> str:
        return f"https://{self.frontegg_domain}/oauth/token"

    @property
    def as_metadata_url(self) -> str:
        return f"{self.resource_server_url.rstrip('/')}/.well-known/oauth-authorization-server"


async def _exchange_frontegg_code(
    config: ProxyConfig, frontegg_code: str, code_verifier: str
) -> httpx.Response:
    """Server-to-server: trade Frontegg's auth code for a JWT.

    Authenticates as a **public OAuth client** (2026-05-18 PKCE-upstream
    clarification) — no client_secret, no Basic auth. Sends `code_verifier`
    instead, which Frontegg validates against the `code_challenge` sent at
    `/oauth/authorize` time. This is the auth method Frontegg's Web-app
    Application types accept; it also satisfies any IdP that supports OAuth
    2.1 PKCE for public clients.

    Factored out so tests can mock without intercepting the test's own httpx.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.post(
            config.frontegg_token_url,
            data={
                "grant_type": "authorization_code",
                "code": frontegg_code,
                "redirect_uri": config.self_callback_url,
                "client_id": config.frontegg_client_id,
                "code_verifier": code_verifier,
            },
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "accept": "application/json",
            },
        )


async def _forward_refresh_to_frontegg(
    config: ProxyConfig, refresh_token: str
) -> httpx.Response:
    """Forward a refresh-token grant to Frontegg (public-client style).

    Per RFC 6749 §6, public clients don't authenticate on refresh-token
    requests; we send only `grant_type`, `refresh_token`, and `client_id`.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.post(
            config.frontegg_token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": config.frontegg_client_id,
            },
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "accept": "application/json",
            },
        )


def _generate_pkce_pair() -> tuple[str, str]:
    """Return (verifier, S256-challenge) for an upstream OAuth flow.

    Verifier: 43–128 chars from the unreserved set (RFC 7636 §4.1). We use
    ~256 bits of entropy via `secrets.token_urlsafe(43)` which yields a
    ~58-char URL-safe string — comfortably above the 43-char minimum.

    Challenge: ``base64url(sha256(verifier))`` with no padding.
    """
    verifier = secrets.token_urlsafe(43)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _pkce_verify(verifier: str, challenge: str) -> bool:
    """RFC 7636 S256: challenge == base64url(sha256(verifier))."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return computed == challenge


def _error_response(status: int, error: str, description: str) -> JSONResponse:
    """RFC 6749 §5.2 error response shape.

    For ``invalid_client`` on a 401 response we additionally emit a
    ``WWW-Authenticate`` header. MCP authorization-spec-compliant clients
    that present a stale/cached ``client_id`` (typically after a
    ``MCP_DCR_SIGNING_KEY`` rotation or a switch from an ephemeral key to
    a configured one) treat this as a signal to re-register at ``/register``
    and retry — turning a hard failure into a transparent recovery.
    """
    headers: dict[str, str] = {}
    if status == 401 and error == "invalid_client":
        # Bearer scheme advertised here because that's what the runtime
        # /mcp auth layer also uses; the realm pin tells the client this
        # is the same protected resource it should re-register against.
        headers["WWW-Authenticate"] = (
            f'Bearer error="invalid_client", error_description="{description}"'
        )
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
        headers=headers or None,
    )


# ---------------------------------------------------------------------------
# Route handlers (closures over store + config so they're testable
# independently of the global mcp instance).
# ---------------------------------------------------------------------------


def make_as_metadata_route(config: ProxyConfig):
    async def _route(request: Request) -> JSONResponse:  # noqa: ARG001
        base = config.resource_server_url.rstrip("/")
        return JSONResponse(
            {
                "issuer": base,
                "authorization_endpoint": f"{base}/oauth/authorize",
                "token_endpoint": f"{base}/oauth/token",
                "registration_endpoint": f"{base}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": [
                    "authorization_code",
                    "refresh_token",
                ],
                "code_challenge_methods_supported": ["S256"],
                "scopes_supported": ["okareo:use"],
                "token_endpoint_auth_methods_supported": ["none"],
            }
        )

    return _route


def make_authorize_route(store: OAuthStateStore, config: ProxyConfig):
    async def _route(request: Request):
        q = request.query_params
        client_id = q.get("client_id")
        redirect_uri = q.get("redirect_uri")
        response_type = q.get("response_type")
        code_challenge = q.get("code_challenge")
        code_challenge_method = q.get("code_challenge_method")
        # `scope` was used to construct the upstream Frontegg redirect URL in
        # the original 020-remote-mcp hosted-login flow. Feature 021 replaces
        # that with a redirect to the same-origin embedded /login page, which
        # negotiates scope with Frontegg client-side. The MCP-client-supplied
        # scope query param is accepted (for spec compliance) but no longer
        # threaded through here. Re-read it via `q.get("scope")` if a future
        # need to enforce it server-side arises.
        state_to_client = q.get("state")

        if not client_id:
            return _error_response(400, "invalid_request", "client_id is required")
        if not redirect_uri:
            return _error_response(400, "invalid_request", "redirect_uri is required")
        if response_type != "code":
            return _error_response(
                400, "unsupported_response_type", "Only response_type=code is supported"
            )
        if not code_challenge:
            return _error_response(
                400, "invalid_request", "code_challenge is required (PKCE)"
            )
        if code_challenge_method != "S256":
            return _error_response(
                400,
                "invalid_request",
                "Only code_challenge_method=S256 is supported",
            )

        client = await store.get_client(client_id)
        if client is None:
            return _error_response(401, "invalid_client", "Unknown client_id")
        if redirect_uri not in client.redirect_uris:
            return _error_response(
                400,
                "invalid_request",
                "redirect_uri does not match a registered value for this client_id",
            )

        # Generate the upstream-side PKCE pair (independent of the MCP
        # client's PKCE). We store the verifier in the pending record and
        # send only the challenge to Frontegg.
        upstream_verifier, upstream_challenge = _generate_pkce_pair()

        pending = await store.create_pending(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method="S256",
            state_to_client=state_to_client,
            upstream_code_verifier=upstream_verifier,
        )

        # 302 → the same-origin embedded login page (feature 021-embedded-login,
        # FR-002). Frontegg cannot host a hosted-login Application alongside
        # the embedded-login Application appfrontend uses; the embedded page
        # authenticates the user against Frontegg directly and posts the
        # resulting tokens to /oauth/handoff.
        #
        # `upstream_code_verifier` and `upstream_challenge` are still
        # generated and persisted above (dead-letter for the new path) so
        # that the retained /oauth/callback GET route — kept as
        # defense-in-depth per specs/021-embedded-login/research.md R4 —
        # remains functional if a misconfigured Frontegg Application ever
        # redirects to it.
        login_url = (
            config.resource_server_url.rstrip("/")
            + "/login?"
            + urllib.parse.urlencode({"pending": pending.code})
        )
        return RedirectResponse(url=login_url, status_code=302)

    return _route


def make_callback_route(store: OAuthStateStore, config: ProxyConfig):
    async def _route(request: Request):
        q = request.query_params
        frontegg_code = q.get("code")
        our_code_as_state = q.get("state")
        frontegg_error = q.get("error")

        if frontegg_error:
            _logger.warning(
                "Frontegg returned error at /oauth/callback: %s", frontegg_error
            )
            return _error_response(
                400,
                "invalid_grant",
                f"upstream rejected: {frontegg_error}",
            )

        if not frontegg_code or not our_code_as_state:
            return _error_response(
                400, "invalid_request", "code and state are required"
            )

        pending = await store.get_pending(our_code_as_state)
        if pending is None:
            return _error_response(
                400,
                "invalid_grant",
                "Unknown or expired authorization state",
            )

        # Server-to-server exchange of the Frontegg code for a JWT. Uses
        # the upstream PKCE verifier we stored at /oauth/authorize time
        # (public-client auth — no client_secret).
        try:
            resp = await _exchange_frontegg_code(
                config,
                frontegg_code,
                pending.upstream_code_verifier,
            )
        except httpx.HTTPError as exc:
            _logger.error(
                "Frontegg /oauth/token unreachable: %s", type(exc).__name__
            )
            return _error_response(
                502, "server_error", "upstream identity provider unreachable"
            )

        if resp.status_code >= 500:
            body_preview = (resp.text or "")[:300]
            _logger.error(
                "Frontegg /oauth/token returned %d. Body preview: %s",
                resp.status_code,
                body_preview,
            )
            return _error_response(502, "server_error", "upstream error")

        if resp.status_code >= 400:
            # Capture upstream's error_description so the operator can diagnose
            # the specific Frontegg-side failure (typically one of:
            # redirect_uri mismatch, client_secret wrong, code expired or
            # already used, grant_type unsupported). The full upstream body is
            # logged; we surface only a sanitized hint to the MCP client.
            body_preview = (resp.text or "")[:300]
            upstream_error = ""
            upstream_description = ""
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    upstream_error = str(parsed.get("error", ""))
                    upstream_description = str(parsed.get("error_description", ""))
            except ValueError:
                pass

            _logger.error(
                "Frontegg /oauth/token rejected the code: status=%d "
                "upstream_error=%r upstream_description=%r body_preview=%r",
                resp.status_code,
                upstream_error,
                upstream_description,
                body_preview,
            )
            print(
                f"[oauth-proxy] Frontegg token exchange FAILED status={resp.status_code} "
                f"error={upstream_error!r} description={upstream_description!r}",
                file=sys.stderr,
                flush=True,
            )
            hint = upstream_description or upstream_error or "upstream rejected the code"
            return _error_response(400, "invalid_grant", hint)

        try:
            body = resp.json()
        except ValueError:
            return _error_response(
                502, "server_error", "upstream response was not JSON"
            )

        access_token = body.get("access_token")
        if not access_token:
            return _error_response(
                502, "server_error", "upstream did not return access_token"
            )

        await store.populate_pending(
            pending.code,
            frontegg_jwt=access_token,
            frontegg_refresh_token=body.get("refresh_token"),
            frontegg_expires_in=body.get("expires_in"),
        )

        # 302 → MCP client's redirect_uri with our code (and pass-through state).
        client_params = {"code": pending.code}
        if pending.state_to_client is not None:
            client_params["state"] = pending.state_to_client
        sep = "&" if "?" in pending.redirect_uri else "?"
        location = (
            pending.redirect_uri + sep + urllib.parse.urlencode(client_params)
        )
        return RedirectResponse(url=location, status_code=302)

    return _route


def make_token_route(store: OAuthStateStore, config: ProxyConfig):
    async def _route(request: Request):
        form = await request.form()
        grant_type = form.get("grant_type")

        if grant_type == "authorization_code":
            return await _handle_auth_code(form, store, config)
        if grant_type == "refresh_token":
            return await _handle_refresh(form, store, config)
        return _error_response(
            400,
            "unsupported_grant_type",
            "Supported grant_types: authorization_code, refresh_token",
        )

    return _route


async def _handle_auth_code(
    form, store: OAuthStateStore, config: ProxyConfig
):
    code = form.get("code")
    client_id = form.get("client_id")
    code_verifier = form.get("code_verifier")
    redirect_uri = form.get("redirect_uri")

    if not code or not client_id or not code_verifier:
        return _error_response(
            400,
            "invalid_request",
            "code, client_id, and code_verifier are required",
        )

    # Consumption is one-time and final — even on PKCE failure, we delete.
    pending = await store.consume_pending(code)
    if pending is None:
        return _error_response(
            400, "invalid_grant", "Unknown, expired, or already-consumed code"
        )

    if pending.client_id != client_id:
        return _error_response(
            400, "invalid_grant", "client_id does not match the original authorization"
        )

    if redirect_uri is not None and pending.redirect_uri != redirect_uri:
        return _error_response(
            400, "invalid_grant", "redirect_uri does not match the original authorization"
        )

    if not _pkce_verify(code_verifier, pending.code_challenge):
        return _error_response(
            400, "invalid_grant", "PKCE verifier does not match the challenge"
        )

    if not pending.frontegg_jwt:
        # Callback never populated — Frontegg side never completed.
        return _error_response(
            400, "invalid_grant", "Authorization not yet completed by upstream"
        )

    # Token passthrough.
    body = {
        "access_token": pending.frontegg_jwt,
        "token_type": "Bearer",
    }
    if pending.frontegg_expires_in is not None:
        body["expires_in"] = pending.frontegg_expires_in
    if pending.frontegg_refresh_token is not None:
        body["refresh_token"] = pending.frontegg_refresh_token
    return JSONResponse(body)


async def _handle_refresh(form, store: OAuthStateStore, config: ProxyConfig):  # noqa: ARG001
    refresh_token = form.get("refresh_token")
    if not refresh_token:
        return _error_response(
            400, "invalid_request", "refresh_token is required"
        )
    try:
        resp = await _forward_refresh_to_frontegg(config, refresh_token)
    except httpx.HTTPError:
        return _error_response(
            502, "server_error", "upstream identity provider unreachable"
        )
    # Pass through verbatim (status + body).
    try:
        body = resp.json()
    except ValueError:
        return _error_response(
            502, "server_error", "upstream response was not JSON"
        )
    if resp.status_code >= 500:
        body_preview = (resp.text or "")[:300]
        _logger.error(
            "Frontegg /oauth/token (refresh) returned %d. Body preview: %s",
            resp.status_code,
            body_preview,
        )
        return _error_response(502, "server_error", "upstream error")
    if resp.status_code >= 400:
        # Same diagnostic shape as the auth-code rejection path: surface
        # Frontegg's `error` + `error_description` so we can distinguish
        # "stale refresh_token" from "wrong client credentials".
        upstream_error = ""
        upstream_description = ""
        if isinstance(body, dict):
            upstream_error = str(body.get("error", ""))
            upstream_description = str(body.get("error_description", ""))
        _logger.error(
            "Frontegg /oauth/token (refresh) rejected: status=%d "
            "upstream_error=%r upstream_description=%r",
            resp.status_code,
            upstream_error,
            upstream_description,
        )
        print(
            f"[oauth-proxy] Frontegg refresh FAILED status={resp.status_code} "
            f"error={upstream_error!r} description={upstream_description!r}",
            file=sys.stderr,
            flush=True,
        )
    return JSONResponse(body, status_code=resp.status_code)


def register_oauth_proxy_routes(mcp, store: OAuthStateStore, config: ProxyConfig) -> None:
    """Mount the four OAuth Proxy routes on the given FastMCP instance."""
    mcp.custom_route(
        "/.well-known/oauth-authorization-server", methods=["GET"]
    )(make_as_metadata_route(config))
    mcp.custom_route("/oauth/authorize", methods=["GET"])(
        make_authorize_route(store, config)
    )
    mcp.custom_route("/oauth/callback", methods=["GET"])(
        make_callback_route(store, config)
    )
    mcp.custom_route("/oauth/token", methods=["POST"])(
        make_token_route(store, config)
    )
