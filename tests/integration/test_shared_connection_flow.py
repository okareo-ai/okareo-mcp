"""End to end: a hand-configured OAuth client connects with an API key (048 US2).

Drives authorize → token → /mcp → revoke → /mcp → refresh against a real
``OkareoFastMCP`` with the real ``CombinedTokenVerifier`` and
``OkareoAPIKeyVerifier``; only okareo-server is mocked.

Each step builds a fresh server from the same keys and the same key
verifier, which is also what Cloud Run does to us: the authorize and token
calls may land on different instances, and nothing but the signing key is
shared between them (research R8).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import urllib.parse

import httpx
import jwt as pyjwt
import pytest
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from src.auth.api_key_verifier import OkareoAPIKeyVerifier
from src.auth.context import get_session_credential_optional
from src.auth.jwks_cache import JWKSCache
from src.auth.protected_resource import OkareoFastMCP
from src.auth.shared_connection import (
    CLIENT_ID,
    SharedConnectionKeys,
    register_shared_connection_routes,
)
from src.auth.verifier import CombinedTokenVerifier

RESOURCE = "http://localhost:8080"
# Slack's callback, standing in for any customer tool; nothing in the server
# knows it.
CALLBACK = "https://oauth2.slack.com/external/auth/callback"
SIGNING_KEY = "a-stable-dcr-signing-key-of-at-least-32-bytes"
API_KEY = pyjwt.encode(
    {"type": "apiKey", "tenantId": "tenant-shared", "sub": "admin-1"},
    "fixture-signing-key-that-is-32-bytes!",
    algorithm="HS256",
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _OkareoServer:
    def __init__(self) -> None:
        self.revoked = False
        self.keys_seen: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("api-key", "")
        self.keys_seen.append(key)
        if key != API_KEY or self.revoked:
            return httpx.Response(401)
        return httpx.Response(200, json=[])


class _World:
    def __init__(self) -> None:
        self.clock = _Clock()
        self.okareo = _OkareoServer()
        self.keys = SharedConnectionKeys.from_signing_key(SIGNING_KEY)
        self.key_verifier = OkareoAPIKeyVerifier(
            base_url="https://api.okareo.example/",
            transport=httpx.MockTransport(self.okareo.handler),
            clock=self.clock,
        )

    def server(self) -> OkareoFastMCP:
        verifier = CombinedTokenVerifier(
            issuer_url="https://auth.okareo.example",
            resource_server_url=RESOURCE,
            jwks_cache=JWKSCache("https://auth.okareo.example"),
            api_key_validator=self.key_verifier.validate,
            required_scope="",
            shared_keys=self.keys,
        )
        mcp = OkareoFastMCP(
            "shared-flow",
            token_verifier=verifier,
            auth=AuthSettings(
                issuer_url=AnyHttpUrl(RESOURCE),
                resource_server_url=AnyHttpUrl(RESOURCE),
            ),
            stateless_http=True,
            json_response=True,
            host="127.0.0.1",
            port=0,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )
        register_shared_connection_routes(
            mcp,
            keys=self.keys,
            validator=self.key_verifier.validate,
            base_url=RESOURCE,
        )

        @mcp.tool()
        async def whoami() -> dict:
            """Reports the session the tool runs in (test only)."""
            cred = get_session_credential_optional()
            return {
                "kind": cred.kind,
                "org_id": cred.org_id,
                "api_key_sha256": hashlib.sha256(cred.api_key.encode()).hexdigest(),
            }

        return mcp


@pytest.fixture
def world() -> _World:
    return _World()


def _basic() -> dict:
    raw = f"{CLIENT_ID}:{urllib.parse.quote(API_KEY, safe='')}"
    return {"Authorization": "Basic " + base64.b64encode(raw.encode()).decode()}


def _request(mcp: OkareoFastMCP, method: str, url: str, *, run_session=False, **kw):
    app = mcp.streamable_http_app()

    async def _go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            if run_session:
                async with mcp.session_manager.run():
                    return await client.request(method, url, **kw)
            return await client.request(method, url, **kw)

    return asyncio.run(_go())


def _call_whoami(world: _World, access_token: str) -> httpx.Response:
    return _request(
        world.server(),
        "POST",
        "/mcp",
        run_session=True,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}},
        },
        headers={
            "authorization": f"Bearer {access_token}",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        },
    )


def _connect(world: _World) -> dict:
    verifier = "v" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    r = _request(
        world.server(),
        "GET",
        "/oauth/shared/authorize",
        params={
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": CALLBACK,
            "state": "client-state",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert r.status_code == 302, r.text
    q = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
    assert q["state"] == ["client-state"]

    r = _request(
        world.server(),  # a different "instance" from the authorize call
        "POST",
        "/oauth/shared/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": CALLBACK,
            "code_verifier": verifier,
        },
        headers=_basic(),
    )
    assert r.status_code == 200, r.text
    return r.json()


class TestSharedConnectionFlow:
    def test_connect_use_revoke_refresh(self, world):
        tokens = _connect(world)

        # A member's tool call runs as the key's organization, and tools get
        # the inner key, never the envelope.
        r = _call_whoami(world, tokens["access_token"])
        assert r.status_code == 200, r.text
        result = json.loads(r.json()["result"]["content"][0]["text"])
        assert result == {
            "kind": "shared_api_key",
            "org_id": "tenant-shared",
            "api_key_sha256": hashlib.sha256(API_KEY.encode()).hexdigest(),
        }
        assert set(world.okareo.keys_seen) == {API_KEY}

        # Revoke; once the 30 s cache has expired the next call is refused.
        world.okareo.revoked = True
        world.clock.now += 31
        r = _call_whoami(world, tokens["access_token"])
        assert r.status_code == 401
        assert "API key is not valid" in r.json()["error_description"]

        # The client's renewal is refused too, so the member is asked to reconnect.
        r = _request(
            world.server(),
            "POST",
            "/oauth/shared/token",
            data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
            headers=_basic(),
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_refresh_keeps_a_working_connection(self, world):
        tokens = _connect(world)
        r = _request(
            world.server(),
            "POST",
            "/oauth/shared/token",
            data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
            headers=_basic(),
        )
        assert r.status_code == 200, r.text
        assert _call_whoami(world, r.json()["access_token"]).status_code == 200

    def test_discovery_does_not_advertise_the_shared_routes(self, world):
        r = _request(world.server(), "GET", "/.well-known/oauth-protected-resource")
        assert "/oauth/shared" not in r.text
