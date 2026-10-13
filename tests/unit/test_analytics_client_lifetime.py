"""In HTTP mode the analytics client lives for the process (049).

The MCP SDK runs the server's ``lifespan`` for every request in stateless HTTP
mode, and ``lifespan`` used to create the analytics client on the way in and
close it on the way out. Overlapping requests then closed each other's client.
"""

import asyncio

import httpx
import pytest

from src import analytics, server


@pytest.fixture
def http_mode_analytics(monkeypatch):
    monkeypatch.setattr(server, "_HTTP_MODE", True)
    monkeypatch.setenv("OKAREO_ANALYTICS_OPT_IN", "1")
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test_dummy")
    monkeypatch.delenv("DEV", raising=False)
    monkeypatch.delenv("AIRGAP", raising=False)
    monkeypatch.setattr(server, "_analytics_client", None)

    delivered: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        delivered.append(request)
        return httpx.Response(200, json={"status": 1})

    real_async_client = httpx.AsyncClient

    def _local_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(analytics.httpx, "AsyncClient", _local_client)
    yield delivered

    client = server._analytics_client
    if client is not None and client.http_client is not None:
        asyncio.run(client.http_client.aclose())
    server._analytics_client = None


def _send_and_wait(client):
    async def _send():
        before = set(analytics._background_tasks)
        analytics.emit_tool_event(client, tool_name="list_projects", success=True, annotations={})
        await asyncio.gather(*(set(analytics._background_tasks) - before))
    return _send()


def test_overlapping_requests_share_one_open_client(http_mode_analytics):
    delivered = http_mode_analytics

    async def _run():
        first = server.lifespan(server.mcp)
        second = server.lifespan(server.mcp)
        await first.__aenter__()
        ctx_two = await second.__aenter__()
        await first.__aexit__(None, None, None)
        client = ctx_two["analytics"]
        assert client is server._analytics_client
        assert client.http_client is not None and not client.http_client.is_closed
        await _send_and_wait(client)
        await second.__aexit__(None, None, None)
        return client

    client = asyncio.run(_run())
    assert delivered, "the second request's event was not delivered"
    assert server._analytics_client is client


def test_a_send_scheduled_just_before_a_request_ends_is_delivered(http_mode_analytics):
    delivered = http_mode_analytics

    async def _run():
        cm = server.lifespan(server.mcp)
        ctx = await cm.__aenter__()
        before = set(analytics._background_tasks)
        analytics.emit_tool_event(
            ctx["analytics"], tool_name="list_projects", success=True, annotations={}
        )
        await cm.__aexit__(None, None, None)
        await asyncio.gather(*(set(analytics._background_tasks) - before))

    asyncio.run(_run())
    assert delivered
