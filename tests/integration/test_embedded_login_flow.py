"""End-to-end test of the EMBEDDED-LOGIN OAuth flow (feature 021-embedded-login).

Drives the new path that replaces the redirect-to-Frontegg-hosted-login step:

    DCR /register
      → /oauth/authorize  (302 to /login?pending=...)
      → (simulated browser submits to) /oauth/handoff with a Frontegg JWT
      → /oauth/token  (MCP client redeems the code)

Asserts the byte-identical-JWT invariant (FR-004 / SC-003): the JWT the MCP
client receives at /oauth/token is exactly what was POSTed to /oauth/handoff,
matching what the page would have received from Frontegg's identity REST API
in the real flow.

Frontegg is fully mocked at the page-side boundary: the page would have
called Frontegg's `POST /identity/resources/auth/v1/user` and received a
JWT + refresh_token; here we just sign a fixture JWT and feed it to the
handoff endpoint directly.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
import urllib.parse
from typing import Any

import httpx
import pytest
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from src.auth.oauth_proxy import ProxyConfig, register_oauth_proxy_routes
from src.auth.oauth_state import OAuthStateStore


def _pkce_pair() -> tuple[str, str]:
    verifier = "test-verifier-43-chars-or-more-rfc-7636-compliant"
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _good_claims(now: int | None = None) -> dict[str, Any]:
    now = now or int(time.time())
    return {
        "iss": "https://test.frontegg.example",
        "aud": "http://localhost:8080",
        "sub": "user-embed-1",
        "exp": now + 600,
        "iat": now,
        "organization_id": "org-embedded",
        "scope": "okareo:use",
    }


@pytest.fixture
def wired_embedded_server(jwks_doc):
    """A full FastMCP wired with OAuth Proxy + DCR + /oauth/handoff.

    Mirrors the test_oauth_proxy_flow.py setup but adds the new handoff
    route via make_handoff_route — exactly as src/server.py does in
    HTTP mode.
    """
    from src.auth.embedded_handoff import make_handoff_route
    from src.auth.jwks_cache import JWKSCache

    resource_server_url = "http://localhost:8080"
    issuer = "https://test.frontegg.example"
    state = OAuthStateStore()
    config = ProxyConfig(
        resource_server_url=resource_server_url,
        frontegg_domain="test.frontegg.example",
        frontegg_client_id="fake-frontegg-app",
    )

    async def _stub_get_key(kid: str):
        for k in jwks_doc["keys"]:
            if k["kid"] == kid:
                return k
        return None

    jwks = JWKSCache(issuer)
    jwks.get_key = _stub_get_key  # type: ignore[method-assign]

    async def _api_key_resolver(_: str):
        return None

    from src.auth.verifier import CombinedTokenVerifier

    verifier = CombinedTokenVerifier(
        issuer_url=issuer,
        resource_server_url=resource_server_url,
        jwks_cache=jwks,
        api_key_resolver=_api_key_resolver,
        required_scope="okareo:use",
    )

    mcp = FastMCP(
        "test-embedded-mcp",
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(resource_server_url),
            resource_server_url=AnyHttpUrl(resource_server_url),
            required_scopes=["okareo:use"],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["okareo:use"],
                default_scopes=["okareo:use"],
            ),
        ),
        stateless_http=True,
        json_response=True,
        host="127.0.0.1",
        port=0,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )

    register_oauth_proxy_routes(mcp, state, config)

    handoff_handler = make_handoff_route(
        state, config, jwks,
        audience_aliases=[resource_server_url, resource_server_url + "/"],
    )

    @mcp.custom_route("/oauth/handoff", methods=["POST", "OPTIONS"])
    async def _handoff(request):
        return await handoff_handler(request)

    from src.auth.dcr_proxy import build_dcr_app

    _dcr_app = build_dcr_app(state)
    _dcr_route = next(
        r for r in _dcr_app.router.routes if getattr(r, "path", "") == "/register"
    )
    mcp.custom_route("/register", methods=["POST"])(_dcr_route.endpoint)

    return mcp, state, config


def _post(app, path, **kwargs):
    transport = httpx.ASGITransport(app=app)

    async def _run():
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost:8080", follow_redirects=False
        ) as c:
            return await c.post(path, **kwargs)

    return asyncio.run(_run())


def _get(app, path, **kwargs):
    transport = httpx.ASGITransport(app=app)

    async def _run():
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost:8080", follow_redirects=False
        ) as c:
            return await c.get(path, **kwargs)

    return asyncio.run(_run())


class TestEmbeddedLoginFlow:
    def test_full_flow_jwt_passthrough(self, wired_embedded_server, jwt_signer):
        """FR-004 / SC-003: the JWT the MCP client receives at /oauth/token
        is byte-identical to what the page POSTed to /oauth/handoff.
        """
        mcp, _state, _config = wired_embedded_server
        app = mcp.streamable_http_app()
        verifier, challenge = _pkce_pair()
        client_redirect_uri = "http://127.0.0.1:33418/"
        fixture_jwt = jwt_signer(_good_claims())

        # --- 1. DCR ---
        r_register = _post(
            app,
            "/register",
            json={
                "redirect_uris": [client_redirect_uri],
                "client_name": "VS Code",
                "token_endpoint_auth_method": "none",
                "scope": "okareo:use",
            },
            headers={"content-type": "application/json"},
        )
        assert r_register.status_code == 201
        client_id = r_register.json()["client_id"]

        # --- 2. /oauth/authorize → 302 to /login?pending=... ---
        r_authorize = _get(
            app,
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": client_redirect_uri,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "vsc-state-nonce",
            },
        )
        assert r_authorize.status_code == 302
        login_url = r_authorize.headers["location"]
        assert "/login?" in login_url
        assert "frontegg" not in urllib.parse.urlsplit(login_url).netloc.lower()
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(login_url).query)
        our_code = qs["pending"][0]

        # --- 3. (simulated page) POST to /oauth/handoff ---
        r_handoff = _post(
            app,
            "/oauth/handoff",
            json={
                "pending_code": our_code,
                "frontegg_access_token": fixture_jwt,
                "frontegg_refresh_token": "embedded-refresh-token-1",
                "frontegg_expires_in": 3600,
            },
            headers={
                "content-type": "application/json",
                "origin": "http://localhost:8080",
                "sec-fetch-site": "same-origin",
            },
        )
        assert r_handoff.status_code == 200, r_handoff.text
        handoff_redirect = r_handoff.json()["redirect_url"]
        assert handoff_redirect.startswith(client_redirect_uri)
        handoff_qs = urllib.parse.parse_qs(
            urllib.parse.urlsplit(handoff_redirect).query
        )
        assert handoff_qs["code"][0] == our_code
        assert handoff_qs["state"] == ["vsc-state-nonce"]

        # --- 4. MCP client redeems at /oauth/token ---
        r_token = _post(
            app,
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": our_code,
                "client_id": client_id,
                "code_verifier": verifier,
                "redirect_uri": client_redirect_uri,
            },
        )
        assert r_token.status_code == 200, r_token.text
        token_body = r_token.json()

        # JWT passthrough invariant (FR-004): byte-identical to what the page
        # handed off. This is the regression guard that catches any future
        # change that accidentally re-mints or transforms the token.
        assert token_body["access_token"] == fixture_jwt
        assert token_body["token_type"] == "Bearer"
        assert token_body["expires_in"] == 3600
        assert token_body["refresh_token"] == "embedded-refresh-token-1"

    def test_double_handoff_then_token_yields_invalid_grant_on_second_token(
        self, wired_embedded_server, jwt_signer
    ):
        """One-time-use invariant: /oauth/token consumes the pending record.
        A second /oauth/token call with the same code must fail.
        """
        mcp, _state, _config = wired_embedded_server
        app = mcp.streamable_http_app()
        verifier, challenge = _pkce_pair()
        client_redirect_uri = "http://127.0.0.1:33418/"
        fixture_jwt = jwt_signer(_good_claims())

        r_register = _post(
            app,
            "/register",
            json={
                "redirect_uris": [client_redirect_uri],
                "client_name": "VS Code",
                "token_endpoint_auth_method": "none",
                "scope": "okareo:use",
            },
            headers={"content-type": "application/json"},
        )
        client_id = r_register.json()["client_id"]
        r_authorize = _get(
            app,
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": client_redirect_uri,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        our_code = urllib.parse.parse_qs(
            urllib.parse.urlsplit(r_authorize.headers["location"]).query
        )["pending"][0]

        _post(
            app,
            "/oauth/handoff",
            json={
                "pending_code": our_code,
                "frontegg_access_token": fixture_jwt,
                "frontegg_refresh_token": "rt",
                "frontegg_expires_in": 3600,
            },
            headers={
                "content-type": "application/json",
                "origin": "http://localhost:8080",
            },
        )

        # First /oauth/token redeems and consumes.
        r1 = _post(
            app,
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": our_code,
                "client_id": client_id,
                "code_verifier": verifier,
                "redirect_uri": client_redirect_uri,
            },
        )
        assert r1.status_code == 200

        # Second redemption with the same code MUST fail.
        r2 = _post(
            app,
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": our_code,
                "client_id": client_id,
                "code_verifier": verifier,
                "redirect_uri": client_redirect_uri,
            },
        )
        assert r2.status_code == 400
        assert r2.json()["error"] == "invalid_grant"
