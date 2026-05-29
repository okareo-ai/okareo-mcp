"""Tests for src/auth/oauth_proxy.py (the four OAuth Proxy routes).

These exercise the route handlers in isolation against an in-process
OAuthStateStore. The Frontegg upstream is mocked at the
`_exchange_frontegg_code` / `_forward_refresh_to_frontegg` function boundary.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import urllib.parse
from unittest.mock import MagicMock, patch

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route


@pytest.fixture
def config():
    from src.auth.oauth_proxy import ProxyConfig

    return ProxyConfig(
        resource_server_url="http://localhost:8080",
        frontegg_domain="example.frontegg.com",
        frontegg_client_id="frontegg-app-id",
    )


@pytest.fixture
def store():
    from src.auth.oauth_state import OAuthStateStore

    return OAuthStateStore()


@pytest.fixture
def app(store, config):
    """A standalone Starlette app exposing the four proxy routes for tests.

    We avoid spinning up FastMCP here — testing routes directly through ASGI
    is sufficient for unit-level checks of request/response shape.
    """
    from src.auth.oauth_proxy import (
        make_as_metadata_route,
        make_authorize_route,
        make_callback_route,
        make_token_route,
    )

    return Starlette(
        routes=[
            Route(
                "/.well-known/oauth-authorization-server",
                make_as_metadata_route(config),
                methods=["GET"],
            ),
            Route(
                "/oauth/authorize",
                make_authorize_route(store, config),
                methods=["GET"],
            ),
            Route(
                "/oauth/callback",
                make_callback_route(store, config),
                methods=["GET"],
            ),
            Route(
                "/oauth/token",
                make_token_route(store, config),
                methods=["POST"],
            ),
        ]
    )


def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    )


def _pkce_pair() -> tuple[str, str]:
    """Return (verifier, S256-challenge)."""
    verifier = "test-verifier-with-enough-entropy-to-be-rfc-compliant-12345"
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


async def _register(store, redirect_uri="http://127.0.0.1:33418/"):
    return await store.register_client(
        client_name="test-client",
        redirect_uris=(redirect_uri,),
    )


class TestASMetadata:
    def test_advertises_our_own_endpoints(self, app, config):
        async def run():
            async with _client(app) as c:
                return await c.get("/.well-known/oauth-authorization-server")

        r = asyncio.run(run())
        assert r.status_code == 200
        body = r.json()
        assert body["issuer"] == config.resource_server_url
        assert body["authorization_endpoint"].endswith("/oauth/authorize")
        assert body["token_endpoint"].endswith("/oauth/token")
        assert body["registration_endpoint"].endswith("/register")
        assert body["code_challenge_methods_supported"] == ["S256"]
        assert "authorization_code" in body["grant_types_supported"]
        assert "refresh_token" in body["grant_types_supported"]


class TestAuthorize:
    def test_happy_path_302s_to_embedded_login(self, app, store, config):
        """021-embedded-login FR-002: /oauth/authorize redirects to the
        same-origin embedded login page, not to Frontegg's hosted login.
        The page authenticates the user against Frontegg's identity REST
        API directly and posts the resulting JWT to /oauth/handoff.
        """

        async def run():
            client = await _register(store)
            _, challenge = _pkce_pair()
            async with _client(app) as c:
                return await c.get(
                    "/oauth/authorize",
                    params={
                        "client_id": client.client_id,
                        "redirect_uri": "http://127.0.0.1:33418/",
                        "response_type": "code",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "state": "vsc-nonce",
                    },
                )

        r = asyncio.run(run())
        assert r.status_code == 302
        location = r.headers["location"]
        # Same-origin redirect to the MCP server's own /login (NOT Frontegg).
        assert location.startswith(config.resource_server_url.rstrip("/") + "/login?"), location
        # Must NOT redirect to any frontegg.com hostname.
        assert "frontegg" not in urllib.parse.urlsplit(location).netloc.lower(), location
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
        # The page receives our pending-auth code as `pending` (not as `state`).
        assert "pending" in qs
        assert qs["pending"][0].startswith("okm_")
        # The MCP-client's `state` is NOT exposed in the URL — it's persisted
        # on the PendingAuthorization record server-side and only re-emerges
        # in the final redirect to the MCP client at /oauth/handoff time.
        assert "state" not in qs

    def test_unknown_client_id_returns_401(self, app, store):
        _, challenge = _pkce_pair()

        async def run():
            async with _client(app) as c:
                return await c.get(
                    "/oauth/authorize",
                    params={
                        "client_id": "not-registered",
                        "redirect_uri": "http://127.0.0.1:33418/",
                        "response_type": "code",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                    },
                )

        r = asyncio.run(run())
        assert r.status_code == 401
        assert r.json()["error"] == "invalid_client"

    def test_redirect_uri_not_in_allowlist_returns_400(self, app, store):
        _, challenge = _pkce_pair()

        async def run():
            client = await _register(store, redirect_uri="http://127.0.0.1:33418/")
            async with _client(app) as c:
                return await c.get(
                    "/oauth/authorize",
                    params={
                        "client_id": client.client_id,
                        "redirect_uri": "http://malicious.example/",
                        "response_type": "code",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                    },
                )

        r = asyncio.run(run())
        assert r.status_code == 400

    def test_missing_pkce_returns_400(self, app, store):
        async def run():
            client = await _register(store)
            async with _client(app) as c:
                return await c.get(
                    "/oauth/authorize",
                    params={
                        "client_id": client.client_id,
                        "redirect_uri": "http://127.0.0.1:33418/",
                        "response_type": "code",
                        # no code_challenge
                    },
                )

        r = asyncio.run(run())
        assert r.status_code == 400

    def test_authorize_persists_upstream_code_verifier_dead_letter(
        self, app, store
    ):
        """021-embedded-login: the upstream PKCE verifier is still generated
        and persisted on the PendingAuthorization record as dead-letter for
        the retained /oauth/callback defense-in-depth path (research.md R4).
        New embedded flows do not read it, but it must still be present so
        /oauth/callback continues to work if a misconfigured Frontegg
        Application ever redirects to it.
        """
        _, challenge = _pkce_pair()

        async def run():
            client = await _register(store)
            async with _client(app) as c:
                r = await c.get(
                    "/oauth/authorize",
                    params={
                        "client_id": client.client_id,
                        "redirect_uri": "http://127.0.0.1:33418/",
                        "response_type": "code",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                    },
                )
            # The pending-auth code is now in `?pending=` on the /login URL.
            our_code = urllib.parse.parse_qs(
                urllib.parse.urlsplit(r.headers["location"]).query
            )["pending"][0]
            pending = await store.get_pending(our_code)
            return pending

        pending = asyncio.run(run())
        assert pending is not None
        # Non-empty verifier matching the RFC 7636 length range — required
        # by the retained /oauth/callback path.
        assert 43 <= len(pending.upstream_code_verifier) <= 128

    def test_authorize_preserves_client_state_on_pending_record(
        self, app, store
    ):
        """The MCP-client-supplied `state` is NOT echoed in the /login URL
        (it'd leak unnecessarily into the browser-visible URL). Instead it
        lives on the PendingAuthorization record and is re-emitted in the
        final redirect to the MCP client at /oauth/handoff completion time.
        """
        _, challenge = _pkce_pair()

        async def run():
            client = await _register(store)
            async with _client(app) as c:
                r = await c.get(
                    "/oauth/authorize",
                    params={
                        "client_id": client.client_id,
                        "redirect_uri": "http://127.0.0.1:33418/",
                        "response_type": "code",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "state": "client-state-xyz",
                    },
                )
            our_code = urllib.parse.parse_qs(
                urllib.parse.urlsplit(r.headers["location"]).query
            )["pending"][0]
            pending = await store.get_pending(our_code)
            return pending

        pending = asyncio.run(run())
        assert pending is not None
        assert pending.state_to_client == "client-state-xyz"

    def test_non_s256_method_returns_400(self, app, store):
        async def run():
            client = await _register(store)
            async with _client(app) as c:
                return await c.get(
                    "/oauth/authorize",
                    params={
                        "client_id": client.client_id,
                        "redirect_uri": "http://127.0.0.1:33418/",
                        "response_type": "code",
                        "code_challenge": "x",
                        "code_challenge_method": "plain",
                    },
                )

        r = asyncio.run(run())
        assert r.status_code == 400


class TestCallback:
    def test_happy_path_exchanges_code_and_302s_to_client(
        self, app, store, config
    ):
        verifier, challenge = _pkce_pair()
        frontegg_jwt = "frontegg.fake.jwt"

        async def fake_exchange(_config, _code, _verifier):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {
                "access_token": frontegg_jwt,
                "token_type": "Bearer",
                "expires_in": 600,
                "refresh_token": "refresh-fake",
            }
            return resp

        async def run():
            client = await _register(store)
            pending = await store.create_pending(
                client_id=client.client_id,
                redirect_uri="http://127.0.0.1:33418/",
                code_challenge=challenge,
                code_challenge_method="S256",
                state_to_client="vsc-state",
            )
            with patch(
                "src.auth.oauth_proxy._exchange_frontegg_code",
                fake_exchange,
            ):
                async with _client(app) as c:
                    r = await c.get(
                        "/oauth/callback",
                        params={
                            "code": "frontegg-code",
                            "state": pending.code,
                        },
                    )
            return r, await store.get_pending(pending.code)

        r, populated = asyncio.run(run())
        assert r.status_code == 302
        # location goes back to MCP-client redirect_uri with our code + state
        loc = r.headers["location"]
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query)
        assert qs["state"] == ["vsc-state"]
        assert qs["code"][0].startswith("okm_")
        # pending now populated
        assert populated is not None
        assert populated.frontegg_jwt == frontegg_jwt

    def test_callback_sends_upstream_code_verifier_not_basic_auth(
        self, app, store, config
    ):
        """2026-05-18 PKCE-upstream: /oauth/callback's exchange must send
        the stored upstream `code_verifier` to Frontegg and MUST NOT send
        an HTTP Basic auth header (Frontegg's Web-app types reject that).
        """
        _, challenge = _pkce_pair()
        captured = {}

        async def fake_exchange(captured_config, captured_code, captured_verifier):
            captured["config"] = captured_config
            captured["code"] = captured_code
            captured["verifier"] = captured_verifier
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {
                "access_token": "jwt",
                "token_type": "Bearer",
                "expires_in": 600,
                "refresh_token": "r",
            }
            return resp

        async def run():
            client = await _register(store)
            pending = await store.create_pending(
                client_id=client.client_id,
                redirect_uri="http://127.0.0.1:33418/",
                code_challenge=challenge,
                code_challenge_method="S256",
                state_to_client=None,
                upstream_code_verifier="UPSTREAM-VERIFIER-FROM-STORE",
            )
            with patch(
                "src.auth.oauth_proxy._exchange_frontegg_code", fake_exchange
            ):
                async with _client(app) as c:
                    return await c.get(
                        "/oauth/callback",
                        params={
                            "code": "frontegg-code",
                            "state": pending.code,
                        },
                    )

        r = asyncio.run(run())
        assert r.status_code == 302
        assert captured["verifier"] == "UPSTREAM-VERIFIER-FROM-STORE"
        assert captured["code"] == "frontegg-code"

    def test_unknown_state_returns_400(self, app):
        async def run():
            async with _client(app) as c:
                return await c.get(
                    "/oauth/callback",
                    params={"code": "frontegg-code", "state": "unknown"},
                )

        r = asyncio.run(run())
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_frontegg_5xx_returns_502(self, app, store):
        verifier, challenge = _pkce_pair()

        async def fake_exchange(_config, _code, _verifier):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 500
            resp.text = "internal server error"
            return resp

        async def run():
            client = await _register(store)
            pending = await store.create_pending(
                client_id=client.client_id,
                redirect_uri="http://127.0.0.1:33418/",
                code_challenge=challenge,
                code_challenge_method="S256",
                state_to_client=None,
            )
            with patch(
                "src.auth.oauth_proxy._exchange_frontegg_code", fake_exchange
            ):
                async with _client(app) as c:
                    return await c.get(
                        "/oauth/callback",
                        params={
                            "code": "frontegg-code",
                            "state": pending.code,
                        },
                    )

        r = asyncio.run(run())
        assert r.status_code == 502

    def test_frontegg_network_error_returns_502(self, app, store):
        verifier, challenge = _pkce_pair()

        async def boom(_config, _code, _verifier):
            raise httpx.ConnectError("upstream unreachable")

        async def run():
            client = await _register(store)
            pending = await store.create_pending(
                client_id=client.client_id,
                redirect_uri="http://127.0.0.1:33418/",
                code_challenge=challenge,
                code_challenge_method="S256",
                state_to_client=None,
            )
            with patch("src.auth.oauth_proxy._exchange_frontegg_code", boom):
                async with _client(app) as c:
                    return await c.get(
                        "/oauth/callback",
                        params={
                            "code": "frontegg-code",
                            "state": pending.code,
                        },
                    )

        r = asyncio.run(run())
        assert r.status_code == 502


class TestToken:
    def _seed_populated_pending(self, store, verifier_challenge):
        verifier, challenge = verifier_challenge

        async def setup():
            client = await store.register_client(
                client_name="t",
                redirect_uris=("http://127.0.0.1:33418/",),
            )
            pending = await store.create_pending(
                client_id=client.client_id,
                redirect_uri="http://127.0.0.1:33418/",
                code_challenge=challenge,
                code_challenge_method="S256",
                state_to_client=None,
            )
            await store.populate_pending(
                pending.code,
                frontegg_jwt="frontegg.jwt.value",
                frontegg_refresh_token="frontegg.refresh.value",
                frontegg_expires_in=600,
            )
            return client, pending

        return asyncio.run(setup())

    def test_happy_path_returns_frontegg_jwt_verbatim(self, app, store):
        verifier, challenge = _pkce_pair()
        client, pending = self._seed_populated_pending(store, (verifier, challenge))

        async def run():
            async with _client(app) as c:
                return await c.post(
                    "/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": pending.code,
                        "client_id": client.client_id,
                        "code_verifier": verifier,
                        "redirect_uri": "http://127.0.0.1:33418/",
                    },
                )

        r = asyncio.run(run())
        assert r.status_code == 200
        body = r.json()
        assert body["access_token"] == "frontegg.jwt.value"
        assert body["token_type"] == "Bearer"
        assert body["refresh_token"] == "frontegg.refresh.value"
        assert body["expires_in"] == 600

    def test_pkce_mismatch_returns_invalid_grant_and_consumes_record(
        self, app, store
    ):
        verifier, challenge = _pkce_pair()
        client, pending = self._seed_populated_pending(store, (verifier, challenge))

        async def run():
            async with _client(app) as c:
                r = await c.post(
                    "/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": pending.code,
                        "client_id": client.client_id,
                        "code_verifier": "WRONG-VERIFIER",
                        "redirect_uri": "http://127.0.0.1:33418/",
                    },
                )
            return r, await store.get_pending(pending.code)

        r, after = asyncio.run(run())
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"
        # record consumed even though PKCE failed (one-time use)
        assert after is None

    def test_replay_consumed_code_returns_invalid_grant(self, app, store):
        verifier, challenge = _pkce_pair()
        client, pending = self._seed_populated_pending(store, (verifier, challenge))

        async def run():
            async with _client(app) as c:
                first = await c.post(
                    "/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": pending.code,
                        "client_id": client.client_id,
                        "code_verifier": verifier,
                        "redirect_uri": "http://127.0.0.1:33418/",
                    },
                )
                second = await c.post(
                    "/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": pending.code,
                        "client_id": client.client_id,
                        "code_verifier": verifier,
                        "redirect_uri": "http://127.0.0.1:33418/",
                    },
                )
            return first, second

        first, second = asyncio.run(run())
        assert first.status_code == 200
        assert second.status_code == 400
        assert second.json()["error"] == "invalid_grant"

    def test_unknown_code_returns_invalid_grant(self, app, store):
        verifier, _ = _pkce_pair()

        async def run():
            async with _client(app) as c:
                return await c.post(
                    "/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": "okm_unknown",
                        "client_id": "any",
                        "code_verifier": verifier,
                        "redirect_uri": "http://127.0.0.1:33418/",
                    },
                )

        r = asyncio.run(run())
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_client_id_mismatch_returns_invalid_grant(self, app, store):
        verifier, challenge = _pkce_pair()
        client, pending = self._seed_populated_pending(store, (verifier, challenge))

        async def run():
            async with _client(app) as c:
                return await c.post(
                    "/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": pending.code,
                        "client_id": "different-client",
                        "code_verifier": verifier,
                        "redirect_uri": "http://127.0.0.1:33418/",
                    },
                )

        r = asyncio.run(run())
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_refresh_token_grant_forwards_to_frontegg(self, app, store):
        async def fake_refresh(_config, _refresh):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {
                "access_token": "new.jwt",
                "token_type": "Bearer",
                "expires_in": 600,
                "refresh_token": "rotated.refresh",
            }
            return resp

        async def run():
            with patch(
                "src.auth.oauth_proxy._forward_refresh_to_frontegg",
                fake_refresh,
            ):
                async with _client(app) as c:
                    return await c.post(
                        "/oauth/token",
                        data={
                            "grant_type": "refresh_token",
                            "refresh_token": "old.refresh",
                            "client_id": "mcp-client-id",
                        },
                    )

        r = asyncio.run(run())
        assert r.status_code == 200
        assert r.json()["access_token"] == "new.jwt"

    def test_unsupported_grant_returns_400(self, app):
        async def run():
            async with _client(app) as c:
                return await c.post(
                    "/oauth/token",
                    data={"grant_type": "client_credentials"},
                )

        r = asyncio.run(run())
        assert r.status_code == 400
        assert r.json()["error"] == "unsupported_grant_type"
