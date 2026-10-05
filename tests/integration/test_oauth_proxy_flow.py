"""End-to-end test of the OAuth Proxy flow with a fake-Frontegg fixture.

Drives the full proxied flow as an MCP client would (minus the actual
browser hop): DCR /register → /oauth/authorize → simulated Frontegg
callback → /oauth/token → tools/list with the returned JWT. Asserts the
token-passthrough invariant (the JWT the client receives at /oauth/token
is byte-identical to what fake-Frontegg returned).

This is the executable form of the Phase 3b Independent Test. The actual
browser-mediated flow against a real Frontegg tenant is the manual
quickstart verification.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import urllib.parse
from unittest.mock import MagicMock, patch

import httpx
import pytest
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from src.auth.context import get_session_credential_optional
from src.auth.oauth_proxy import ProxyConfig, register_oauth_proxy_routes
from src.auth.oauth_state import OAuthStateStore
from src.auth.protected_resource import OkareoFastMCP
from tests.integration.oauth_discovery import authorization_server_metadata_url


def _pkce_pair() -> tuple[str, str]:
    verifier = "test-verifier-43-chars-or-more-rfc-7636-compliant"
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


@pytest.fixture
def wired_proxy_server(jwks_doc, jwt_signer, default_claims, issuer_url):
    """A full FastMCP wired with OAuth Proxy routes + verifier + DCR.

    Frontegg's /oauth/token is mocked at the function boundary; the JWT
    returned is signed with the in-process test JWKS so the verifier accepts
    it when the MCP client uses it on /mcp.
    """
    from src.auth.jwks_cache import JWKSCache
    from src.auth.verifier import CombinedTokenVerifier

    resource_server_url = "http://localhost:8080"
    state = OAuthStateStore()
    config = ProxyConfig(
        resource_server_url=resource_server_url,
        frontegg_domain="example.frontegg.com",
        frontegg_client_id="fake-frontegg-app",
    )

    async def _stub_get_key(kid: str):
        for k in jwks_doc["keys"]:
            if k["kid"] == kid:
                return k
        return None

    jwks = JWKSCache(issuer_url)
    jwks.get_key = _stub_get_key  # type: ignore[method-assign]

    async def _api_key_resolver(_: str):
        return None  # bearer-fallback disabled for this test

    verifier = CombinedTokenVerifier(
        issuer_url=issuer_url,
        resource_server_url=resource_server_url,
        jwks_cache=jwks,
        api_key_resolver=_api_key_resolver,
        required_scope="okareo:use",
    )

    mcp = OkareoFastMCP(
        "test-okareo-mcp",
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

    # DCR
    from src.auth.dcr_proxy import build_dcr_app

    _dcr_app = build_dcr_app(state)
    _dcr_route = next(
        r for r in _dcr_app.router.routes if getattr(r, "path", "") == "/register"
    )
    mcp.custom_route("/register", methods=["POST"])(_dcr_route.endpoint)

    # A simple tool so tools/list has something to return.
    @mcp.tool()
    async def whoami() -> dict:
        cred = get_session_credential_optional()
        return {"org_id": cred.org_id if cred else None}

    # Sign a fixture JWT that the verifier will accept.
    fixture_jwt = jwt_signer(default_claims)

    return mcp, state, config, fixture_jwt


def _post(app, path: str, **kwargs):
    transport = httpx.ASGITransport(app=app)

    async def _run():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            return await client.post(path, **kwargs)

    return asyncio.run(_run())


def _get(app, path: str, **kwargs):
    transport = httpx.ASGITransport(app=app)

    async def _run():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            return await client.get(path, **kwargs)

    return asyncio.run(_run())


class TestFullProxyFlow:
    def test_register_authorize_callback_token_returns_jwt_passthrough(
        self, wired_proxy_server
    ):
        """The headline happy path. Drives steps 3 through 9 of the OAuth
        Proxy flow diagram (DCR → authorize → simulated Frontegg callback →
        token redemption) and asserts the token-passthrough invariant.
        """
        mcp, state, config, fixture_jwt = wired_proxy_server
        app = mcp.streamable_http_app()
        verifier, challenge = _pkce_pair()
        client_redirect_uri = "http://127.0.0.1:33418/"

        # --- Step 3: DCR ---
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

        # --- Step 4: /oauth/authorize → 302 to Frontegg ---
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
        # Post 021-embedded-login: /oauth/authorize redirects to the same-origin
        # /login page with our pending-auth code in `?pending=`. The new flow
        # goes through /oauth/handoff (see test_embedded_login_flow.py).
        #
        # This test continues to exercise the /oauth/callback defense-in-depth
        # path retained for misconfigured Frontegg Applications that might
        # still redirect to it (research.md R4). Both paths share the same
        # PendingAuthorization state model.
        login_redirect = r_authorize.headers["location"]
        assert "/login?" in login_redirect, login_redirect
        assert "frontegg" not in urllib.parse.urlsplit(login_redirect).netloc.lower()
        qs = urllib.parse.parse_qs(
            urllib.parse.urlsplit(login_redirect).query
        )
        our_code = qs["pending"][0]
        assert our_code.startswith("okm_")

        # --- Steps 5/6: simulate Frontegg redirecting back to /oauth/callback ---
        # /oauth/callback exchanges Frontegg's code for a JWT via the
        # _exchange_frontegg_code seam. We mock that to return our fixture JWT.
        # NOTE: this is the defense-in-depth path (research.md R4); the new
        # embedded flow uses /oauth/handoff instead — covered by
        # test_embedded_login_flow.py.
        async def fake_exchange(_config, _code, _verifier):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {
                "access_token": fixture_jwt,
                "token_type": "Bearer",
                "expires_in": 600,
                "refresh_token": "fake-refresh-token-value",
            }
            return resp

        with patch(
            "src.auth.oauth_proxy._exchange_frontegg_code", fake_exchange
        ):
            r_callback = _get(
                app,
                "/oauth/callback",
                params={
                    "code": "frontegg-code-12345",
                    "state": our_code,
                },
            )
        assert r_callback.status_code == 302
        # 302 should go back to the MCP-client redirect with our code
        client_callback = r_callback.headers["location"]
        client_qs = urllib.parse.parse_qs(
            urllib.parse.urlsplit(client_callback).query
        )
        assert client_qs["state"] == ["vsc-state-nonce"]
        assert client_qs["code"] == [our_code]

        # --- Step 9: /oauth/token with PKCE ---
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
        assert r_token.status_code == 200
        token_body = r_token.json()

        # Token passthrough invariant: the JWT we hand the client is byte-
        # identical to what fake-Frontegg returned at the callback exchange.
        assert token_body["access_token"] == fixture_jwt
        assert token_body["token_type"] == "Bearer"
        assert token_body["expires_in"] == 600
        assert token_body["refresh_token"] == "fake-refresh-token-value"


class TestRefreshTokenGrant:
    def test_refresh_forwards_to_frontegg(self, wired_proxy_server):
        mcp, _state, _config, _fixture_jwt = wired_proxy_server
        app = mcp.streamable_http_app()

        async def fake_refresh(_config, _refresh):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {
                "access_token": "rotated.jwt",
                "token_type": "Bearer",
                "expires_in": 600,
                "refresh_token": "rotated.refresh",
            }
            return resp

        with patch(
            "src.auth.oauth_proxy._forward_refresh_to_frontegg", fake_refresh
        ):
            r = _post(
                app,
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": "old.refresh",
                    "client_id": "any",
                },
            )

        assert r.status_code == 200
        body = r.json()
        assert body["access_token"] == "rotated.jwt"
        assert body["refresh_token"] == "rotated.refresh"


class TestASMetadataPublished:
    def test_well_known_advertises_our_endpoints(self, wired_proxy_server):
        mcp, _state, _config, _fixture_jwt = wired_proxy_server
        app = mcp.streamable_http_app()
        r = _get(app, "/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        body = r.json()
        assert body["authorization_endpoint"].endswith("/oauth/authorize")
        assert body["token_endpoint"].endswith("/oauth/token")
        assert body["registration_endpoint"].endswith("/register")
        # Discovery doc must explicitly NOT point at Frontegg.
        assert "frontegg" not in body["authorization_endpoint"]
        assert "frontegg" not in body["token_endpoint"]


class TestAuthorizationServerDiscovery:
    """046: the two discovery documents name the authorization server identically.

    A client that follows RFC 8414 §3.3 (the Copilot CLI does) takes
    ``authorization_servers[0]`` from the protected resource document, builds
    the authorization server document's URL from it, and refuses the document
    unless ``issuer`` is identical, character for character.
    """

    def test_issuer_is_identical_to_the_advertised_authorization_server(
        self, wired_proxy_server
    ):
        mcp, _state, _config, _fixture_jwt = wired_proxy_server
        app = mcp.streamable_http_app()

        prm = _get(app, "/.well-known/oauth-protected-resource")
        assert prm.status_code == 200
        advertised = prm.json()["authorization_servers"][0]

        # Redirects are followed so that a trailing slash fails here on the
        # issuer comparison, not on Starlette's 307 for the slashed path.
        as_doc = _get(
            app,
            authorization_server_metadata_url(advertised),
            follow_redirects=True,
        )
        assert as_doc.status_code == 200, as_doc.text
        assert as_doc.json()["issuer"] == advertised

    def test_discovery_chain_from_an_unauthenticated_request(self, wired_proxy_server):
        """The whole chain the Copilot CLI walks before it can register:
        401 → ``resource_metadata`` → protected resource document →
        ``authorization_servers[0]`` → authorization server document, whose
        ``issuer`` must be identical and which must offer registration."""
        mcp, _state, _config, _fixture_jwt = wired_proxy_server
        app = mcp.streamable_http_app()

        unauthenticated = _post(
            app,
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
        )
        assert unauthenticated.status_code == 401
        challenge = unauthenticated.headers["www-authenticate"]
        match = re.search(r'resource_metadata="([^"]+)"', challenge)
        assert match, challenge
        prm_url = match.group(1)

        prm = _get(app, prm_url)
        assert prm.status_code == 200, prm.text
        advertised = prm.json()["authorization_servers"][0]

        as_doc = _get(
            app,
            authorization_server_metadata_url(advertised),
            follow_redirects=True,
        )
        assert as_doc.status_code == 200, as_doc.text
        body = as_doc.json()
        assert body["issuer"] == advertised
        assert body["registration_endpoint"] == f"{advertised}/register"

    def test_document_differs_from_the_sdk_default_only_in_authorization_servers(
        self,
    ):
        """Everything else — resource, scopes, bearer methods, caching, CORS —
        is what the installed SDK serves for the same settings.

        If this fails because the stock document no longer carries the trailing
        slash, the SDK has fixed its rendering: ``OkareoFastMCP`` and this
        test can be deleted, with ``server.py`` back on plain ``FastMCP``.
        """

        class _RejectEveryToken(TokenVerifier):
            async def verify_token(self, token: str):
                return None

        def _document(server_class):
            server = server_class(
                "prm-probe",
                token_verifier=_RejectEveryToken(),
                auth=AuthSettings(
                    issuer_url=AnyHttpUrl("http://localhost:8080"),
                    resource_server_url=AnyHttpUrl("http://localhost:8080"),
                    required_scopes=["okareo:use"],
                ),
                stateless_http=True,
                json_response=True,
                host="127.0.0.1",
                port=0,
                transport_security=TransportSecuritySettings(
                    enable_dns_rebinding_protection=False,
                ),
            )
            return _get(
                server.streamable_http_app(),
                "/.well-known/oauth-protected-resource",
                headers={"origin": "https://client.example"},
            )

        stock, ours = _document(FastMCP), _document(OkareoFastMCP)
        assert stock.status_code == 200 and ours.status_code == 200
        stock_body, ours_body = stock.json(), ours.json()
        # The SDK's rendering is the bug; ours is the issuer identifier.
        assert stock_body.pop("authorization_servers") == ["http://localhost:8080/"]
        assert ours_body.pop("authorization_servers") == ["http://localhost:8080"]
        assert ours_body == stock_body
        for header in ("cache-control", "access-control-allow-origin"):
            assert ours.headers[header] == stock.headers[header], header
