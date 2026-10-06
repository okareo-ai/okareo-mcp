"""Protected resource metadata (RFC 9728) whose issuer matches the AS document.

The MCP SDK writes ``authorization_servers`` through pydantic ``AnyHttpUrl``,
which renders a bare origin with a trailing slash (``https://tools.okareo.com/``).
Our authorization server document (``oauth_proxy.make_as_metadata_route``)
publishes ``issuer`` without one. RFC 8414 §3.3 and the MCP authorization
specification require the two to be identical, and GitHub Copilot CLI refuses
to connect when they differ. The SDK has no setting for the rendering and
appends custom routes after its own, so the only hook is the Starlette app
``streamable_http_app()`` returns.

See specs/046-oauth-issuer-exact-match/research.md R3–R5 and R10–R11.

The same hook gives the SDK's ``AuthenticationMiddleware`` an ``on_error``,
so API-key failures raised by the verifier become a 401 that names the key or
a 503 when okareo-server is unreachable, instead of Starlette's plain-text 400
(specs/048-api-key-shared-connections research R6).
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from mcp.server.auth.routes import build_resource_metadata_url, cors_middleware
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import ProtectedResourceMetadata
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.authentication import AuthenticationError
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from src.auth.errors import CredentialUnavailableError, InvalidAPIKeyError

_logger = logging.getLogger(__name__)


def issuer_identifier(resource_server_url: str | AnyHttpUrl) -> str:
    """This MCP's issuer identifier: its own URL with no trailing slash (RFC 8414 §2).

    Both discovery documents take the string from here, so they cannot drift
    apart again. The SDK receives the setting as a pydantic ``AnyHttpUrl`` and
    the OAuth proxy as the raw string, so the string is normalized through
    ``AnyHttpUrl`` first: a default port and upper-case scheme or host
    (``https://tools.okareo.com:443``, ``HTTPS://Tools.Okareo.com``) would
    otherwise give the two documents different spellings of one origin.
    """
    return str(AnyHttpUrl(str(resource_server_url))).rstrip("/")


def _protected_resource_route(path: str, auth: AuthSettings) -> Route:
    # Built from the SDK's own model so every other field and default stays
    # exactly what the installed SDK serves; only the one string the SDK
    # cannot render without a slash is overwritten.
    document = ProtectedResourceMetadata(
        resource=auth.resource_server_url,
        authorization_servers=[auth.issuer_url],
        scopes_supported=auth.required_scopes,
    ).model_dump(mode="json", exclude_none=True)
    document["authorization_servers"] = [issuer_identifier(auth.issuer_url)]

    async def _serve(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(
            document, headers={"Cache-Control": "public, max-age=3600"}
        )

    return Route(
        path,
        endpoint=cors_middleware(_serve, ["GET", "OPTIONS"]),
        methods=["GET", "OPTIONS"],
    )


def _auth_error_handler(resource_metadata_url: str):
    def _on_error(conn: HTTPConnection, exc: AuthenticationError) -> Response:  # noqa: ARG001
        if isinstance(exc, CredentialUnavailableError):
            return JSONResponse(
                {"error": "temporarily_unavailable", "error_description": exc.description},
                status_code=503,
                headers={"Retry-After": "5"},
            )
        if isinstance(exc, InvalidAPIKeyError):
            # Same header shape as the SDK's own 401, so a client that
            # rediscovers from `resource_metadata` behaves the same either way.
            www_authenticate = (
                f'Bearer error="invalid_token", '
                f'error_description="{exc.description}", '
                f'resource_metadata="{resource_metadata_url}"'
            )
            return JSONResponse(
                {"error": "invalid_token", "error_description": exc.description},
                status_code=401,
                headers={"WWW-Authenticate": www_authenticate},
            )
        # Starlette's own default for anything else.
        return PlainTextResponse(str(exc), status_code=400)

    return _on_error


class OkareoFastMCP(FastMCP):
    """``FastMCP`` whose protected resource document names the authorization
    server exactly as that server's ``issuer`` names itself.

    Without auth settings (stdio mode) the SDK mounts no such document and this
    class changes nothing.
    """

    def streamable_http_app(self, *args, **kwargs) -> Starlette:
        app = super().streamable_http_app(*args, **kwargs)
        auth = self.settings.auth
        if auth is None or auth.resource_server_url is None:
            return app
        metadata_url = str(build_resource_metadata_url(auth.resource_server_url))
        _replace_protected_resource_route(app, urlparse(metadata_url).path, auth)
        _install_auth_error_handler(app, metadata_url)
        return app


def _replace_protected_resource_route(app: Starlette, path: str, auth: AuthSettings) -> None:
    routes = app.router.routes
    for index, route in enumerate(routes):
        if isinstance(route, Route) and route.path == path:
            routes[index] = _protected_resource_route(path, auth)
            return
    # A newer SDK that mounts the document elsewhere, or not at all, would
    # otherwise put the trailing slash back in production without a trace.
    _logger.warning(
        "Auth settings are present but the SDK registered no route at %s; "
        "the protected resource document is being served by the SDK, whose "
        "authorization_servers may not equal the authorization server's issuer.",
        path,
    )


def _install_auth_error_handler(app: Starlette, resource_metadata_url: str) -> None:
    # The SDK builds this middleware inside streamable_http_app() with no
    # on_error, and Starlette builds the middleware stack lazily on the first
    # request, so setting the kwarg here takes effect.
    for entry in app.user_middleware:
        if entry.cls is AuthenticationMiddleware:
            entry.kwargs["on_error"] = _auth_error_handler(resource_metadata_url)
            return
    _logger.warning(
        "Auth settings are present but the SDK built no AuthenticationMiddleware; "
        "an invalid API key will not get its explanatory 401 and an okareo-server "
        "outage will not get its 503."
    )
