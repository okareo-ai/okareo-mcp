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
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

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
        path = urlparse(
            str(build_resource_metadata_url(auth.resource_server_url))
        ).path
        routes = app.router.routes
        for index, route in enumerate(routes):
            if isinstance(route, Route) and route.path == path:
                routes[index] = _protected_resource_route(path, auth)
                return app
        # A newer SDK that mounts the document elsewhere, or not at all, would
        # otherwise put the trailing slash back in production without a trace.
        _logger.warning(
            "Auth settings are present but the SDK registered no route at %s; "
            "the protected resource document is being served by the SDK, whose "
            "authorization_servers may not equal the authorization server's issuer.",
            path,
        )
        return app
