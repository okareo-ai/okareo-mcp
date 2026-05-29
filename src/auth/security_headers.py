"""Security headers for the embedded login page and handoff endpoint.

Applied as a Starlette middleware in HTTP mode only. Stdio mode never
imports this module (see ``src/server.py`` HTTP-mode gating).

Header policy is documented in specs/021-embedded-login/research.md R7 and
the contract at specs/021-embedded-login/contracts/login-page-contract.md §4.5.
"""

from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


_logger = logging.getLogger(__name__)


_HEADER_PATHS = ("/login", "/oauth/handoff")


def _is_tls_production() -> bool:
    """Decide whether to emit HSTS. Cloud Run terminates TLS; local dev does not.

    Heuristic: emit HSTS only if ``MCP_RESOURCE_SERVER_URL`` is https. The
    dev-mode env var override is intentionally ignored here — HSTS on
    http://localhost would be a misconfiguration.
    """
    return os.environ.get("MCP_RESOURCE_SERVER_URL", "").startswith("https://")


def _build_csp(frontegg_domain: str) -> str:
    """Construct the CSP header value.

    The page's ONLY legitimate outbound network destinations are:
      - `'self'` — the MCP origin (POST to /oauth/handoff).
      - `https://${FRONTEGG_DOMAIN}` — the user's tenant-scoped Frontegg URL.
        Whether that's the Frontegg-native subdomain (`app-xxxx.frontegg.com`)
        or a customer-branded domain (`auth-dev.okareo.com`), it must match
        the value of `NEXT_PUBLIC_FRONTEGG_BASE_URL` baked into the bundle.

    We deliberately do NOT allow `https://api.frontegg.com` (Frontegg's
    multi-tenant aggregator). The page never calls it; allowing it would
    (a) widen the outbound attack surface and (b) defeat CSP as a
    defense-in-depth check against the silent-fallback misconfiguration
    we hit during initial demo testing.

    If `frontegg_domain` is empty (operator misconfiguration), the page
    cannot reach Frontegg at all; the resulting CSP violation in the
    browser console surfaces this loudly rather than silently allowing
    a wrong host.

    Mantine + Next.js inline boot scripts require ``script-src 'unsafe-inline'``
    and ``style-src 'unsafe-inline'``; CSP nonces are tracked as a v1.1
    hardening item (see research.md R7).
    """
    connect_src_parts = ["'self'"]
    domain = (frontegg_domain or "").strip()
    if domain:
        connect_src_parts.append(f"https://{domain}")
    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline'",
            "style-src 'self' 'unsafe-inline'",
            f"connect-src {' '.join(connect_src_parts)}",
            "img-src 'self' data:",
            "font-src 'self'",
            "frame-ancestors 'none'",
            "form-action 'self'",
            "base-uri 'self'",
        ]
    )


class EmbeddedLoginSecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach security headers to ``/login*`` and ``/oauth/handoff`` responses.

    Other routes (``/mcp``, ``/oauth/authorize``, ``/oauth/token``,
    ``/oauth/callback``, ``/.well-known/*``, ``/register``, ``/health``)
    are untouched — their headers are governed by 020-remote-mcp's existing
    posture and we do not retroactively change them here.
    """

    def __init__(self, app, frontegg_domain: str) -> None:
        super().__init__(app)
        self._csp = _build_csp(frontegg_domain)
        self._tls = _is_tls_production()
        if not (frontegg_domain or "").strip():
            _logger.warning(
                "EmbeddedLoginSecurityHeadersMiddleware constructed with empty "
                "FRONTEGG_DOMAIN; the CSP connect-src will reject ALL outbound "
                "calls from the embedded login page (the page won't be able to "
                "authenticate). Set FRONTEGG_DOMAIN to the tenant URL the page's "
                "NEXT_PUBLIC_FRONTEGG_BASE_URL points at."
            )

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)

        path = request.url.path
        if not any(path == p or path.startswith(p + "/") for p in _HEADER_PATHS):
            return response

        response.headers["Content-Security-Policy"] = self._csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if self._tls:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )

        # Explicitly emit NO Access-Control-Allow-* headers in production.
        # The handoff endpoint is same-origin only; absence of CORS headers is
        # the correct posture. In dev mode the page lives on :3000 but talks
        # to :8080 — and we still don't emit CORS here because the dev mode
        # is documented as a developer convenience (browsers will block the
        # cross-origin POST unless CORS is enabled some other way; the dev
        # workflow uses Next.js's rewrite config to proxy /oauth/handoff
        # through the dev server's same-origin :3000).
        return response
