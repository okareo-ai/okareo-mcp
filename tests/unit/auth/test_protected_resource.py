"""Unit tests for ``src/auth/protected_resource.py`` (046)."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.auth.oauth_proxy import ProxyConfig, register_oauth_proxy_routes
from src.auth.oauth_state import OAuthStateStore
from src.auth.protected_resource import OkareoFastMCP, issuer_identifier

PRM_PATH = "/.well-known/oauth-protected-resource"
AS_PATH = "/.well-known/oauth-authorization-server"

# The same origin spelled the way an operator might type it into
# MCP_RESOURCE_SERVER_URL. pydantic normalizes each of these to
# https://tools.okareo.com/; a raw rstrip("/") leaves them as typed.
NON_CANONICAL_ORIGINS = [
    "https://tools.okareo.com:443",
    "HTTPS://Tools.Okareo.com",
    "https://tools.okareo.com:443/",
]


class _RejectEveryToken(TokenVerifier):
    async def verify_token(self, token: str):
        return None


def _get(app, path: str) -> httpx.Response:
    async def _run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.get(path)

    return asyncio.run(_run())


def _server(**kwargs) -> OkareoFastMCP:
    return OkareoFastMCP(
        "probe",
        host="127.0.0.1",
        port=0,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
        **kwargs,
    )


def _server_with_auth(resource_server_url: str) -> OkareoFastMCP:
    return _server(
        token_verifier=_RejectEveryToken(),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(resource_server_url),
            resource_server_url=AnyHttpUrl(resource_server_url),
        ),
    )


def _prm_routes(app) -> list[Route]:
    return [r for r in app.router.routes if isinstance(r, Route) and r.path == PRM_PATH]


class TestIssuerIdentifier:
    def test_bare_origin_is_unchanged(self):
        assert issuer_identifier("https://tools.okareo.com") == "https://tools.okareo.com"

    def test_trailing_slash_is_removed(self):
        assert issuer_identifier("https://tools.okareo.com/") == "https://tools.okareo.com"

    def test_pydantic_url_loses_the_slash_it_adds(self):
        assert (
            issuer_identifier(AnyHttpUrl("https://tools.okareo.com"))
            == "https://tools.okareo.com"
        )

    @pytest.mark.parametrize("raw", NON_CANONICAL_ORIGINS)
    def test_non_canonical_spellings_give_the_canonical_identifier(self, raw):
        """A default port and upper-case scheme or host name the same origin
        (RFC 3986 §6.2.3); the identifier must not depend on how it was typed."""
        assert issuer_identifier(raw) == "https://tools.okareo.com"

    def test_a_path_is_kept_without_a_trailing_slash(self):
        assert issuer_identifier("https://tools.okareo.com/mcp/") == "https://tools.okareo.com/mcp"


class TestOkareoFastMCP:
    def test_with_auth_settings_the_document_names_the_exact_issuer(self):
        server = _server_with_auth("http://localhost:8080")

        @server.custom_route("/health", methods=["GET"])
        async def _health(request: Request):  # noqa: ARG001
            return JSONResponse({"status": "ok"})

        app = server.streamable_http_app()
        prm = _get(app, PRM_PATH)
        assert prm.status_code == 200
        assert prm.json()["authorization_servers"] == ["http://localhost:8080"]
        assert prm.headers["cache-control"] == "public, max-age=3600"
        # The SDK's route is replaced in place, not added to or dropped;
        # routes registered on the server are still reachable.
        assert len(_prm_routes(app)) == 1
        assert _get(app, "/health").json() == {"status": "ok"}

    def test_warns_when_the_sdk_registered_no_route_to_replace(self, monkeypatch, caplog):
        """If a future SDK moves or drops the protected-resource route, the
        override must say so instead of silently serving the SDK's slash."""
        monkeypatch.setattr(
            FastMCP, "streamable_http_app", lambda self, *args, **kwargs: Starlette()
        )
        server = _server_with_auth("http://localhost:8080")

        with caplog.at_level(logging.WARNING, logger="src.auth.protected_resource"):
            app = server.streamable_http_app()

        assert _prm_routes(app) == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, caplog.text
        assert PRM_PATH in warnings[0].getMessage()

    def test_arguments_reach_the_sdk_unchanged(self, monkeypatch):
        seen: list[tuple] = []

        def _record(self, *args, **kwargs):
            seen.append((args, kwargs))
            return Starlette()

        monkeypatch.setattr(FastMCP, "streamable_http_app", _record)
        _server().streamable_http_app("positional", keyword=True)

        assert seen == [(("positional",), {"keyword": True})]


class TestBothDocumentsAgree:
    """Review finding 1: ``server.py`` hands the SDK a pydantic-normalized URL
    and the OAuth proxy the raw setting with its slash stripped. Both
    documents must still name the authorization server identically."""

    @staticmethod
    def _documents(raw: str) -> tuple[dict, dict]:
        server = _server_with_auth(raw)
        # Exactly how src/server.py builds ProxyConfig from the environment.
        config = ProxyConfig(
            resource_server_url=raw.rstrip("/"),
            frontegg_domain="example.frontegg.com",
            frontegg_client_id="fake-frontegg-app",
        )
        register_oauth_proxy_routes(server, OAuthStateStore(), config)
        app = server.streamable_http_app()
        prm, as_doc = _get(app, PRM_PATH), _get(app, AS_PATH)
        assert prm.status_code == 200 and as_doc.status_code == 200
        return prm.json(), as_doc.json()

    @pytest.mark.parametrize("raw", NON_CANONICAL_ORIGINS)
    def test_non_canonical_setting_yields_one_issuer_string(self, raw):
        prm, as_doc = self._documents(raw)
        assert prm["authorization_servers"] == [as_doc["issuer"]]
        assert as_doc["issuer"] == "https://tools.okareo.com"
