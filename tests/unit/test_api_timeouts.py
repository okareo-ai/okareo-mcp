"""Tool API requests give up in HTTP mode; the request that runs a Run never does (049).

Cloud Run ends a request at 300 seconds, after which nobody is listening, so
in HTTP mode every API request a tool makes gives up after 290. The request
that starts a Run with ``run_test`` stays open until the Run ends, so it gets
its own client with no timeout. Local (stdio) installs keep today's waits.
"""

import socket
import time
from unittest.mock import patch

import httpx
import pytest

from src import okareo_client


@pytest.fixture
def http_mode(monkeypatch):
    monkeypatch.setenv("TRANSPORT", "streamable-http")
    monkeypatch.delenv("OKAREO_API_TIMEOUT_SECONDS", raising=False)


@pytest.fixture
def stdio_mode(monkeypatch):
    monkeypatch.setenv("TRANSPORT", "stdio")


def test_http_mode_tool_requests_give_up_after_290_seconds(http_mode):
    assert okareo_client.api_timeout_seconds() == 290.0


def test_stdio_mode_keeps_no_timeout(stdio_mode):
    assert okareo_client.api_timeout_seconds() is None


def test_the_clients_own_first_request_and_later_ones_give_up_in_http_mode(
    http_mode, projects_request_timeouts
):
    """Read from the HTTP client itself: okareo 0.0.159 accepts ``timeout`` and
    ignores it, so a check of the argument proves nothing (research R15)."""
    okareo = okareo_client.create_okareo_client("k", "https://api.example")
    assert projects_request_timeouts == [httpx.Timeout(290.0)]
    assert okareo.client.get_httpx_client().timeout == httpx.Timeout(290.0)


def test_stdio_mode_builds_a_plain_client_with_no_timeout(
    stdio_mode, projects_request_timeouts
):
    from okareo import Okareo

    with patch("src.okareo_client.Okareo", wraps=Okareo) as okareo_cls:
        okareo = okareo_client.create_okareo_client("k", "https://api.example")
    assert "timeout" not in okareo_cls.call_args.kwargs
    assert projects_request_timeouts == [httpx.Timeout(None)]
    assert okareo.client.get_httpx_client().timeout == httpx.Timeout(None)


def test_the_run_request_client_has_no_timeout_and_leaves_the_tool_client_alone(http_mode):
    from types import SimpleNamespace

    from okareo_api_client import Client

    from src.run_start import run_request_client

    tool_client = Client(
        base_url="https://api.example", raise_on_unexpected_status=True,
        timeout=httpx.Timeout(290.0),
    )
    tool_httpx = tool_client.get_httpx_client()
    run_client = run_request_client(SimpleNamespace(client=tool_client))
    timeout = run_client.get_httpx_client().timeout
    assert timeout.read is None and timeout.connect is None
    assert run_client.get_httpx_client() is not tool_httpx
    assert tool_client.get_httpx_client().timeout.read == 290.0


def test_the_run_request_client_copied_from_a_real_http_mode_tool_client_has_none(
    http_mode, projects_request_timeouts
):
    from src.run_start import run_request_client

    okareo = okareo_client.create_okareo_client("k", "https://api.example")
    run_client = run_request_client(okareo)
    assert run_client.get_httpx_client().timeout == httpx.Timeout(None)
    assert okareo.client.get_httpx_client().timeout == httpx.Timeout(290.0)


def test_calibrate_check_waits_290_in_http_mode_and_720_in_stdio(monkeypatch):
    from src.tools import checks

    monkeypatch.setenv("TRANSPORT", "streamable-http")
    assert checks.calibrate_timeout_seconds() == 290.0
    monkeypatch.setenv("TRANSPORT", "stdio")
    assert checks.calibrate_timeout_seconds() == 720.0


@pytest.mark.timeout(20)
def test_a_tool_client_against_a_server_that_never_answers_gives_up(monkeypatch):
    """The server takes the connection and never replies. Building the client
    makes a request itself (GET /v0/projects); in HTTP mode it must end."""
    monkeypatch.setenv("TRANSPORT", "streamable-http")
    monkeypatch.setenv("OKAREO_API_TIMEOUT_SECONDS", "0.5")

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)  # the kernel completes the handshake; nobody ever reads
    port = listener.getsockname()[1]

    started = time.monotonic()
    try:
        with pytest.raises(httpx.TimeoutException) as raised:
            okareo_client.create_okareo_client("k", f"http://127.0.0.1:{port}")
    finally:
        listener.close()
    assert time.monotonic() - started < 5, raised.value


def test_raw_requests_inherit_the_tool_clients_timeout(http_mode):
    """okareo_api_request goes through the tool client's own HTTP client, so a
    request without its own timeout gets the client's."""
    seen = {}

    class _HttpxClient:
        def request(self, method, path, **kwargs):
            seen.update(kwargs)
            return httpx.Response(200, json={}, request=httpx.Request(method, "http://x" + path))

    class _Okareo:
        api_key = "k"

        class client:  # noqa: N801 - mirrors the SDK attribute
            @staticmethod
            def get_httpx_client():
                return _HttpxClient()

    okareo_client.okareo_api_request(_Okareo(), "GET", "/v0/projects")
    assert "timeout" not in seen  # the client's configured timeout applies
