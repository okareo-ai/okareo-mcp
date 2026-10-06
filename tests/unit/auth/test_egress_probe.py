"""Unit tests for ``src/auth/egress_probe.py`` and the ``/health/egress`` route (047).

The identity provider is mocked with ``httpx.MockTransport``; nothing here
opens a network connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from src.auth import egress_probe
from src.auth.egress_probe import make_egress_health_route

DOMAIN = "idp.example.test"
JWKS_URL = f"https://{DOMAIN}/.well-known/jwks.json"
PATH = "/health/egress"
REPO_ROOT = Path(__file__).resolve().parents[3]

# Planted in mocked upstream responses; must never reach a log line.
BODY_SENTINEL = "BODY-SENTINEL-do-not-log"
HEADER_SENTINEL = "HEADER-SENTINEL-do-not-log"


class _Upstream:
    """Stands in for the identity provider. Records every request and every
    client the probe builds."""

    def __init__(self, respond):
        self._respond = respond
        self.requests: list[httpx.Request] = []
        self.clients: list[httpx.AsyncClient] = []

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._respond(request)

    def new_client(self) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        self.clients.append(client)
        return client


def _status(code: int):
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            code,
            json={"keys": [], "note": BODY_SENTINEL},
            headers={"x-upstream": HEADER_SENTINEL},
        )

    return respond


def _raise(exc_type: type[Exception]):
    async def respond(request: httpx.Request) -> httpx.Response:
        raise exc_type("upstream failure", request=request)

    return respond


@pytest.fixture
def upstream(monkeypatch):
    def install(respond) -> _Upstream:
        fake = _Upstream(respond)
        monkeypatch.setattr(egress_probe, "_new_client", fake.new_client)
        return fake

    return install


def _call(method: str, domain: str = DOMAIN) -> httpx.Response:
    app = Starlette(
        routes=[Route(PATH, make_egress_health_route(domain), methods=["GET", "HEAD"])]
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.request(method, PATH)

    return asyncio.run(run())


class TestEgressProbe:
    def test_upstream_200_is_200_ok(self, upstream):
        fake = upstream(_status(200))
        resp = _call("GET")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert [(r.method, str(r.url)) for r in fake.requests] == [("GET", JWKS_URL)]

    def test_upstream_403_is_503_with_the_status(self, upstream):
        upstream(_status(403))
        resp = _call("GET")
        assert resp.status_code == 503
        assert resp.json() == {"status": "fail", "upstream": 403}

    @pytest.mark.parametrize("code", [301, 404, 429, 500, 502])
    def test_any_other_upstream_status_is_503(self, upstream, code):
        upstream(_status(code))
        resp = _call("GET")
        assert resp.status_code == 503
        assert resp.json() == {"status": "fail", "upstream": code}

    @pytest.mark.parametrize("exc_type", [httpx.ConnectTimeout, httpx.ReadTimeout])
    def test_timeout_is_503_with_the_exception_class(self, upstream, exc_type):
        upstream(_raise(exc_type))
        resp = _call("GET")
        assert resp.status_code == 503
        assert resp.json() == {"status": "fail", "upstream": exc_type.__name__}

    def test_connection_error_is_503_with_the_exception_class(self, upstream):
        upstream(_raise(httpx.ConnectError))
        resp = _call("GET")
        assert resp.status_code == 503
        assert resp.json() == {"status": "fail", "upstream": "ConnectError"}

    def test_overall_deadline_bounds_a_slow_upstream(self, upstream, monkeypatch):
        async def never_answers(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)
            return httpx.Response(200)

        upstream(never_answers)
        monkeypatch.setattr(egress_probe, "OVERALL_TIMEOUT_S", 0.05)
        resp = _call("GET")
        assert resp.status_code == 503
        assert resp.json() == {"status": "fail", "upstream": "TimeoutError"}

    @pytest.mark.parametrize(
        ("respond", "expected"), [(_status(200), 200), (_status(403), 503)]
    )
    def test_head_returns_the_same_status_with_no_body(
        self, upstream, respond, expected
    ):
        fake = upstream(respond)
        resp = _call("HEAD")
        assert resp.status_code == expected
        assert resp.content == b""
        assert [r.method for r in fake.requests] == ["GET"]

    @pytest.mark.parametrize("domain", ["", "   "])
    def test_unset_domain_is_503_with_no_outbound_call(self, upstream, domain):
        fake = upstream(_status(200))
        resp = _call("GET", domain=domain)
        assert resp.status_code == 503
        assert resp.json() == {"status": "fail", "upstream": "frontegg_domain_unset"}
        assert fake.requests == []
        assert fake.clients == []

    def test_every_request_uses_a_fresh_client_that_is_closed_after(self, upstream):
        fake = upstream(_status(200))
        _call("GET")
        _call("GET")
        _call("HEAD")
        assert len(fake.requests) == 3
        assert len(fake.clients) == 3
        assert len({id(c) for c in fake.clients}) == 3
        assert all(c.is_closed for c in fake.clients)

    def test_results_are_not_cached(self, upstream):
        codes = iter([200, 403, 200])

        async def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(next(codes))

        upstream(respond)
        assert [_call("GET").status_code for _ in range(3)] == [200, 503, 200]

    def test_timeouts_stay_under_the_probe_timeout(self):
        client = egress_probe._new_client()
        timeout = client.timeout
        asyncio.run(client.aclose())
        assert timeout.connect == 3.0
        assert timeout.read == timeout.write == timeout.pool == 8.0
        assert egress_probe.OVERALL_TIMEOUT_S == 8.0
        assert egress_probe.CONNECT_TIMEOUT_S < egress_probe.OVERALL_TIMEOUT_S < 10.0


class TestFailureLog:
    def _warnings(self, caplog) -> list[logging.LogRecord]:
        return [
            r
            for r in caplog.records
            if r.name == egress_probe.__name__ and r.levelno == logging.WARNING
        ]

    def test_failure_logs_one_warning_with_status_elapsed_and_host(
        self, upstream, caplog
    ):
        upstream(_status(403))
        with caplog.at_level(logging.WARNING, logger=egress_probe.__name__):
            _call("GET")
        [record] = self._warnings(caplog)
        line = record.getMessage()
        assert "upstream=403" in line
        assert "elapsed_ms=" in line
        assert f"host={DOMAIN}" in line
        assert BODY_SENTINEL not in line
        assert HEADER_SENTINEL not in line

    def test_exception_logs_its_class(self, upstream, caplog):
        upstream(_raise(httpx.ConnectError))
        with caplog.at_level(logging.WARNING, logger=egress_probe.__name__):
            _call("GET")
        [record] = self._warnings(caplog)
        assert "upstream=ConnectError" in record.getMessage()

    def test_success_logs_nothing(self, upstream, caplog):
        upstream(_status(200))
        with caplog.at_level(logging.DEBUG, logger=egress_probe.__name__):
            _call("GET")
        assert [r for r in caplog.records if r.name == egress_probe.__name__] == []


# Imports src.server in a child process: the route only exists in
# streamable-http mode, and src.server reads TRANSPORT once at import, so
# importing it here in that mode would change the module every other test
# imports. FRONTEGG_DOMAIN is empty, so the probe makes no outbound call.
_SERVER_SCRIPT = """
import asyncio, json
import httpx
from src.server import mcp

async def main():
    app = mcp.streamable_http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        out = {}
        for method in ("GET", "HEAD"):
            r = await client.request(method, "/health/egress")
            out[method] = [r.status_code, r.text, r.headers.get("www-authenticate")]
        print(json.dumps(out))

asyncio.run(main())
"""


def test_server_serves_the_route_without_credentials():
    env = {
        **os.environ,
        "TRANSPORT": "streamable-http",
        "MCP_RESOURCE_SERVER_URL": "http://localhost:8080",
        "FRONTEGG_DOMAIN": "",
        "MCP_EMBEDDED_LOGIN_WEB_ROOT": str(REPO_ROOT / "does-not-exist"),
    }
    proc = subprocess.run(
        [sys.executable, "-c", _SERVER_SCRIPT],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    get_status, get_body, get_challenge = out["GET"]
    assert get_status == 503
    assert json.loads(get_body) == {
        "status": "fail",
        "upstream": "frontegg_domain_unset",
    }
    assert get_challenge is None
    assert out["HEAD"] == [503, "", None]
