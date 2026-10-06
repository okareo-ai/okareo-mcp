"""Egress readiness probe: can this instance reach the identity provider?

``GET|HEAD /health/egress`` fetches the identity provider's public JWKS
document on every request and answers 200 only when that fetch returns 200.
A startup probe pointed here keeps a new instance out of rotation until its
outbound path works; an uptime check pointed here alerts when the identity
provider refuses this service's requests or cannot be reached.

The host is ``FRONTEGG_DOMAIN``, the one the OAuth proxy sends token calls to,
so a 200 here means the same host answers this instance.

See specs/047-egress-health-probe/.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_logger = logging.getLogger(__name__)

# Both stay under the 10 s timeout of the startup probe and uptime check that
# call this endpoint, so a slow upstream yields our 503 with a reason instead
# of a bare probe timeout. httpx bounds each phase; the overall deadline also
# bounds an upstream that trickles bytes.
CONNECT_TIMEOUT_S = 3.0
OVERALL_TIMEOUT_S = 8.0


def _new_client() -> httpx.AsyncClient:
    # One client per request: a pooled connection would report on a path
    # opened earlier, not on whether this instance can connect now.
    return httpx.AsyncClient(
        timeout=httpx.Timeout(OVERALL_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
    )


async def _fetch_status(url: str) -> int:
    async with _new_client() as client:
        response = await client.get(url)
        return response.status_code


async def probe_identity_provider(frontegg_domain: str) -> tuple[int, dict]:
    """Return ``(status_code, body)`` for ``/health/egress``."""
    domain = (frontegg_domain or "").strip()
    started = time.monotonic()
    if not domain:
        upstream: int | str = "frontegg_domain_unset"
    else:
        try:
            upstream = await asyncio.wait_for(
                _fetch_status(f"https://{domain}/.well-known/jwks.json"),
                OVERALL_TIMEOUT_S,
            )
        except Exception as exc:
            upstream = type(exc).__name__
    if upstream == 200:
        return 200, {"status": "ok"}
    _logger.warning(
        "Egress probe failed: upstream=%s elapsed_ms=%d host=%s",
        upstream,
        int((time.monotonic() - started) * 1000),
        domain or "-",
    )
    return 503, {"status": "fail", "upstream": upstream}


def make_egress_health_route(frontegg_domain: str):
    """Starlette endpoint for ``GET|HEAD /health/egress``."""

    async def _egress_health(request: Request) -> Response:
        status_code, body = await probe_identity_provider(frontegg_domain)
        if request.method == "HEAD":
            return Response(status_code=status_code)
        return JSONResponse(body, status_code=status_code)

    return _egress_health
