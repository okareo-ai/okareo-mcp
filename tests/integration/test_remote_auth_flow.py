"""End-to-end auth boundary tests for the remote MCP endpoint.

These tests stand up an in-process ``FastMCP`` server with the real
``CombinedTokenVerifier`` wired in, then drive it via ``httpx.AsyncClient``
over the ASGI transport. They cover:

- OAuth happy path: a fixture-signed JWT is accepted; tools/list returns.
- Bearer fallback happy path: a fixture API key is accepted; tools/list returns.
- 401 paths: missing / malformed / expired / wrong-aud tokens are rejected
  with the right ``WWW-Authenticate`` header.
- FR-014 sanity: provider keys remain server-startup env, not per-request.
- Long-running ops shape (U1): a tool returning a job-id immediately works
  through the new transport (the async pattern is unchanged by the transport).

These tests do NOT validate the production ``src/server.py`` wiring; that is
covered by the manual quickstart smoke tests against a real Frontegg dev tenant.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from src.auth.context import (
    SessionCredential,
    get_session_credential_optional,
)


@pytest.fixture
def wired_server_factory(rsa_keypair, jwks_doc, issuer_url, resource_server_url):
    """Returns a factory that builds a fresh wired FastMCP per call.

    The session manager can only run once per instance, so happy-path tests
    that need ``session_manager.run()`` must construct fresh servers.
    """
    from src.auth.jwks_cache import JWKSCache
    from src.auth.verifier import CombinedTokenVerifier

    async def _stub_get_key(kid: str):
        for k in jwks_doc["keys"]:
            if k["kid"] == kid:
                return k
        return None

    async def _api_key_resolver(api_key: str):
        if api_key == "okareo-VALID-FIXTURE-KEY":
            return SessionCredential(
                kind="api_key", api_key=api_key, org_id="org-via-api-key"
            )
        return None

    def _factory() -> FastMCP:
        jwks = JWKSCache(issuer_url)
        jwks.get_key = _stub_get_key  # type: ignore[method-assign]

        verifier = CombinedTokenVerifier(
            issuer_url=issuer_url,
            resource_server_url=resource_server_url,
            jwks_cache=jwks,
            api_key_resolver=_api_key_resolver,
            required_scope="okareo:use",
        )

        mcp = FastMCP(
            "test-okareo-mcp",
            token_verifier=verifier,
            auth=AuthSettings(
                # Post 2026-05-16 OAuth Proxy redesign: PRM advertises the
                # MCP server itself as the AS (issuer_url == resource_server_url).
                # Verifier still validates JWT `iss` against the real Frontegg
                # issuer via its own constructor arg.
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

        @mcp.tool()
        async def whoami() -> dict:
            """Returns the current session credential's org_id (for testing)."""
            cred = get_session_credential_optional()
            return {"org_id": cred.org_id if cred else None}

        @mcp.tool()
        async def submit_long_running_job() -> dict:
            """Long-running ops pattern: return a job id immediately (U1)."""
            return {"test_run_id": "trun-fixture-1", "status": "PROCESSING"}

        return mcp

    return _factory


@pytest.fixture
def wired_server(wired_server_factory):
    """One-shot server for tests that only need the auth boundary
    (no session-manager execution)."""
    return wired_server_factory()


def _post_jsonrpc(app, method: str, params: dict | None, headers: dict):
    """Drive a single JSON-RPC call against the app via ASGI transport.

    Used for auth-boundary tests where the request never reaches the session
    manager (rejected at the auth middleware). For tests that need the tool
    layer, use ``_call_with_session_manager`` instead.
    """
    transport = httpx.ASGITransport(app=app)
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params

    async def _run():
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post(
                "/mcp",
                json=body,
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                    **headers,
                },
            )

    return asyncio.run(_run())


def _call_with_session_manager(mcp, method: str, params: dict | None, headers: dict):
    """Drive a call that needs the FastMCP session manager running.

    Required for happy-path tests that actually invoke a tool — the session
    manager must be inside its ``run()`` context for streamable-http requests
    to be processed. A fresh ``mcp`` instance is required per call since the
    session manager can only be ``run()`` once per instance.
    """
    app = mcp.streamable_http_app()
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params

    async def _run():
        async with mcp.session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.post(
                    "/mcp",
                    json=body,
                    headers={
                        "content-type": "application/json",
                        "accept": "application/json, text/event-stream",
                        **headers,
                    },
                )

    return asyncio.run(_run())


class TestAuthBoundary:
    def test_missing_token_returns_401_with_www_authenticate(self, wired_server):
        r = _post_jsonrpc(wired_server.streamable_http_app(), "tools/list", None, {})
        assert r.status_code == 401
        www_auth = r.headers.get("www-authenticate", "")
        assert "Bearer" in www_auth
        assert "resource_metadata=" in www_auth

    def test_prm_advertises_self_as_authorization_server(
        self, wired_server, resource_server_url
    ):
        """T047: post the OAuth Proxy redesign, the PRM document's
        `authorization_servers` field MUST point at the MCP server itself,
        not Frontegg. MCP clients discover us, do DCR with us, and run the
        OAuth flow through us — Frontegg is invisible at the protocol layer.
        """
        transport = httpx.ASGITransport(app=wired_server.streamable_http_app())

        async def _run():
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.get(
                    "/.well-known/oauth-protected-resource"
                )

        r = asyncio.run(_run())
        assert r.status_code == 200
        body = r.json()
        # The resource_server_url may render with a trailing slash; strip
        # both sides before comparison.
        advertised = [s.rstrip("/") for s in body["authorization_servers"]]
        assert resource_server_url.rstrip("/") in advertised, body

    def test_malformed_token_returns_401(self, wired_server):
        r = _post_jsonrpc(
            wired_server.streamable_http_app(),
            "tools/list",
            None,
            {"authorization": "Bearer garbage.not.a.jwt"},
        )
        assert r.status_code == 401

    def test_expired_jwt_returns_401(
        self, wired_server, jwt_signer, default_claims
    ):
        default_claims["exp"] = int(time.time()) - 100
        default_claims["iat"] = int(time.time()) - 200
        token = jwt_signer(default_claims)
        r = _post_jsonrpc(
            wired_server.streamable_http_app(),
            "tools/list",
            None,
            {"authorization": f"Bearer {token}"},
        )
        assert r.status_code == 401

    def test_wrong_aud_returns_401(
        self, wired_server, jwt_signer, default_claims
    ):
        default_claims["aud"] = "https://malicious.example"
        token = jwt_signer(default_claims)
        r = _post_jsonrpc(
            wired_server.streamable_http_app(),
            "tools/list",
            None,
            {"authorization": f"Bearer {token}"},
        )
        assert r.status_code == 401


class TestHappyPaths:
    def test_valid_jwt_lists_tools(
        self, wired_server_factory, jwt_signer, default_claims
    ):
        token = jwt_signer(default_claims)
        r = _call_with_session_manager(
            wired_server_factory(),
            "tools/list",
            None,
            {"authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        body = _parse_jsonrpc_body(r)
        assert "result" in body
        tool_names = {t["name"] for t in body["result"]["tools"]}
        assert {"whoami", "submit_long_running_job"} <= tool_names

    def test_valid_api_key_lists_tools(self, wired_server_factory):
        r = _call_with_session_manager(
            wired_server_factory(),
            "tools/list",
            None,
            {"authorization": "Bearer okareo-VALID-FIXTURE-KEY"},
        )
        assert r.status_code == 200
        body = _parse_jsonrpc_body(r)
        assert "result" in body

    def test_oauth_and_bearer_return_equivalent_tool_catalog(
        self, wired_server_factory, jwt_signer, default_claims
    ):
        token = jwt_signer(default_claims)
        oauth = _call_with_session_manager(
            wired_server_factory(),
            "tools/list",
            None,
            {"authorization": f"Bearer {token}"},
        )
        bearer = _call_with_session_manager(
            wired_server_factory(),
            "tools/list",
            None,
            {"authorization": "Bearer okareo-VALID-FIXTURE-KEY"},
        )
        oauth_tools = {t["name"] for t in _parse_jsonrpc_body(oauth)["result"]["tools"]}
        bearer_tools = {
            t["name"] for t in _parse_jsonrpc_body(bearer)["result"]["tools"]
        }
        assert oauth_tools == bearer_tools


class TestEnvironmentInvariants:
    def test_provider_key_forwarding_remains_env_scoped(self):
        """FR-014: provider keys (OPENAI_API_KEY, etc.) are read from the
        server-process env by ``src/key_registry.py`` at startup. Per-request
        bearer credentials must NOT be required to supply them. This test
        asserts the env-scan code path is what runs, independent of the
        request credential.
        """
        from src.key_registry import scan_provider_keys

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-from-env"}, clear=False):
            keys = scan_provider_keys()
        assert keys.get("openai") == "sk-test-from-env"


class TestLongRunningOps:
    def test_job_id_pattern_survives_streamable_http(
        self, wired_server_factory, jwt_signer, default_claims
    ):
        """U1: confirm a tool returning a job id immediately (run_simulation
        pattern) round-trips cleanly over the new transport."""
        token = jwt_signer(default_claims)
        r = _call_with_session_manager(
            wired_server_factory(),
            "tools/call",
            {"name": "submit_long_running_job", "arguments": {}},
            {"authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        body = _parse_jsonrpc_body(r)
        assert "result" in body
        assert body["result"]["content"]


def _parse_jsonrpc_body(response: httpx.Response) -> dict[str, Any]:
    """Parse a JSON-RPC body from either a plain JSON body or an SSE-shaped
    response (the SDK can return either depending on Accept negotiation).
    """
    ctype = response.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        # Parse SSE: find lines starting with "data: " and decode the JSON.
        import json

        for line in response.text.splitlines():
            if line.startswith("data:"):
                payload = line[len("data:") :].strip()
                if payload:
                    return json.loads(payload)
        raise AssertionError(f"No data line in SSE body: {response.text!r}")
    return response.json()
