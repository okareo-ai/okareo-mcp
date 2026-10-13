"""Plain-function tools run on worker threads, so a tool never holds the event loop (049).

The MCP SDK calls a plain-function tool directly on the event loop, and while
it runs nobody else is served. These tests go through the production server
object, so they prove the hook is wired where tools are registered, not just
that a helper works in isolation. None of them waits on wall-clock time: tools
block on an event the test releases, or meet at a barrier, each with a timeout
so a regression fails instead of hanging.
"""

import ast
import asyncio
import pathlib
import threading
from unittest.mock import MagicMock

import anyio
import httpx
import mcp.types as types
import pytest
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from src import server

_SRC_TOOLS = pathlib.Path(__file__).resolve().parents[2] / "src" / "tools"
_WAIT = 5.0


def _handler():
    return server.mcp._mcp_server.request_handlers[types.CallToolRequest]


def _request(name: str, arguments: dict | None = None) -> types.CallToolRequest:
    return types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments or {}),
    )


@pytest.fixture
def stdio_ready(monkeypatch):
    """The stdio branch of the handler needs a client before it dispatches."""
    monkeypatch.setattr(server, "_okareo_client", MagicMock())


@pytest.fixture
def temporary_tools():
    """Register tools on the production server for one test, then remove them."""
    added: list[str] = []

    def _add(fn, name: str):
        server.mcp.add_tool(fn, name=name)
        added.append(name)

    yield _add
    for name in added:
        server.mcp._tool_manager._tools.pop(name, None)


class TestTheHookIsWiredIntoProduction:
    def test_the_production_class_lists_the_mixin_before_okareo_fastmcp(self):
        from src.auth.protected_resource import OkareoFastMCP
        from src.tool_threads import ToolThreadsMixin

        mro = type(server.mcp).__mro__
        assert ToolThreadsMixin in mro
        assert mro.index(ToolThreadsMixin) < mro.index(OkareoFastMCP)

    def test_every_plain_tool_runs_on_a_thread_and_async_tools_do_not(self):
        from src.tool_threads import RUNS_ON_THREAD

        tools = server.mcp._tool_manager._tools
        assert tools, "no tools registered"
        for name, tool in tools.items():
            wrapped = getattr(tool.fn, RUNS_ON_THREAD, False)
            if name in {"list_tenants", "switch_tenant"}:
                assert not wrapped, f"{name} is async and must stay on the loop"
            else:
                assert wrapped, f"{name} is not wrapped to run on a thread"


class TestToolsRunSideBySide:
    def test_two_tools_called_through_the_server_both_pass_a_barrier(
        self, stdio_ready, temporary_tools
    ):
        barrier = threading.Barrier(2, timeout=_WAIT)
        passed: list[str] = []

        def meet(who: str) -> str:
            barrier.wait()
            passed.append(who)
            return who

        temporary_tools(meet, "zz_test_meet")

        async def _both():
            return await asyncio.gather(
                _handler()(_request("zz_test_meet", {"who": "a"})),
                _handler()(_request("zz_test_meet", {"who": "b"})),
            )

        results = asyncio.run(_both())
        assert all(not r.root.isError for r in results), [r.root for r in results]
        assert sorted(passed) == ["a", "b"]

    def test_a_tool_raising_on_its_thread_returns_todays_error_content(
        self, stdio_ready, temporary_tools
    ):
        from src.auth.protected_resource import OkareoFastMCP

        def explode() -> str:
            raise RuntimeError("backend exploded on the worker thread")

        temporary_tools(explode, "zz_test_explode")
        # The same function registered without the hook, as every tool was
        # before this change.
        OkareoFastMCP.add_tool(server.mcp, explode, name="zz_test_explode_unwrapped")
        try:
            hooked = asyncio.run(_handler()(_request("zz_test_explode"))).root
            unwrapped = asyncio.run(_handler()(_request("zz_test_explode_unwrapped"))).root
        finally:
            server.mcp._tool_manager._tools.pop("zz_test_explode_unwrapped", None)
        assert hooked.isError is True
        assert hooked.content[0].text == unwrapped.content[0].text


class TestDiscoveryNeverWaitsOnATool:
    def _server(self):
        """A server of the production class, with the auth settings HTTP mode uses."""
        return type(server.mcp)(
            "test-discovery",
            auth=AuthSettings(
                issuer_url=AnyHttpUrl("http://localhost:8080"),
                resource_server_url=AnyHttpUrl("http://localhost:8080"),
            ),
            token_verifier=MagicMock(),
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False,
            ),
        )

    def test_the_protected_resource_document_answers_while_a_tool_is_still_running(self):
        mcp = self._server()
        release = threading.Event()
        tool_finished = threading.Event()

        def hold() -> str:
            release.wait(_WAIT)
            tool_finished.set()
            return "done"

        mcp.add_tool(hold, name="hold")
        app = mcp.streamable_http_app()

        async def _run():
            tool_call = asyncio.create_task(mcp.call_tool("hold", {}))
            await asyncio.sleep(0)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.get("/.well-known/oauth-protected-resource")
            answered_while_running = not tool_finished.is_set()
            release.set()
            await tool_call
            return response, answered_while_running

        response, answered_while_running = asyncio.run(_run())
        assert response.status_code == 200
        assert answered_while_running, "the document waited for the tool to finish"


class TestTheCapIsItsOwn:
    def test_busy_tools_borrow_nothing_from_the_shared_pool(self, monkeypatch):
        monkeypatch.setenv("OKAREO_TOOL_THREADS", "2")
        from src.tool_threads import run_on_thread, tool_limiter

        release = threading.Event()
        started = threading.Barrier(3, timeout=_WAIT)

        def hold() -> str:
            started.wait()
            release.wait(_WAIT)
            return "done"

        wrapped = run_on_thread(hold)

        async def _run():
            calls = [asyncio.create_task(wrapped()) for _ in range(2)]
            await anyio.to_thread.run_sync(started.wait)
            ours = tool_limiter().borrowed_tokens
            shared = anyio.to_thread.current_default_thread_limiter().borrowed_tokens
            # The shared pool (static files) still runs work while every tool
            # slot is taken.
            served = await anyio.to_thread.run_sync(lambda: "static file")
            release.set()
            await asyncio.gather(*calls)
            return ours, shared, served

        ours, shared, served = asyncio.run(_run())
        assert ours == 2
        # The barrier wait above borrows one shared token while it runs; the
        # tools themselves borrow none.
        assert shared <= 1
        assert served == "static file"

    def test_the_cap_defaults_to_the_instance_request_limit(self, monkeypatch):
        monkeypatch.delenv("OKAREO_TOOL_THREADS", raising=False)
        from src.tool_threads import tool_thread_cap

        assert tool_thread_cap() == 80

    def test_a_cancelled_caller_keeps_its_thread_counted_until_the_tool_returns(self):
        """The MCP SDK cancels a request through an anyio cancel scope (a
        client that disconnects, for example). The thread can't be stopped,
        so its slot must stay counted until the tool returns."""
        from src.tool_threads import run_on_thread, tool_limiter

        release = threading.Event()
        entered = threading.Event()

        def hold() -> str:
            entered.set()
            release.wait(_WAIT)
            return "done"

        wrapped = run_on_thread(hold)
        counts = {}

        async def _run():
            scopes: list[anyio.CancelScope] = []

            async def caller():
                with anyio.CancelScope() as scope:
                    scopes.append(scope)
                    await wrapped()

            async with anyio.create_task_group() as tg:
                tg.start_soon(caller)
                await anyio.to_thread.run_sync(entered.wait, _WAIT)
                scopes[0].cancel()
                for _ in range(5):
                    await anyio.sleep(0)
                counts["while_running"] = tool_limiter().borrowed_tokens
                release.set()
            counts["after"] = tool_limiter().borrowed_tokens

        asyncio.run(_run())
        assert counts["while_running"] == 1
        assert counts["after"] == 0


class TestAsyncToolsAreKnown:
    def test_only_list_tenants_and_switch_tenant_are_async_tools(self):
        """An async tool gets no thread, so a blocking call inside one would
        freeze the server again. A new one must be added here on purpose."""
        found = set()
        for path in sorted(_SRC_TOOLS.glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue
                for dec in node.decorator_list:
                    target = dec.func if isinstance(dec, ast.Call) else dec
                    if isinstance(target, ast.Attribute) and target.attr == "tool":
                        found.add(node.name)
        assert found == {"list_tenants", "switch_tenant"}


class TestTheCallersContextReachesTheThread:
    def test_the_callers_credential_is_seen_inside_the_worker_thread(
        self, stdio_ready, temporary_tools
    ):
        """One organization's call must run with its own credential after the
        hop to a thread, never another's."""
        from src.auth.context import (
            SessionCredential,
            get_session_credential_optional,
            set_session_credential,
        )

        seen = {}

        def whoami() -> str:
            cred = get_session_credential_optional()
            seen["org"] = cred.org_id if cred else None
            seen["thread"] = threading.current_thread().name
            return "ok"

        temporary_tools(whoami, "zz_test_whoami")

        async def _call_as(org):
            set_session_credential(
                SessionCredential(kind="api_key", api_key=f"key-{org}", org_id=org)
            )
            return await _handler()(_request("zz_test_whoami"))

        result = asyncio.run(_call_as("org-A")).root
        assert not result.isError
        assert seen["org"] == "org-A"
        assert seen["thread"] != threading.main_thread().name


class TestToolCallLogLines:
    def test_call_and_done_share_a_call_id_and_done_reports_threads_and_memory(
        self, stdio_ready, temporary_tools, caplog
    ):
        def quick() -> str:
            return "ok"

        temporary_tools(quick, "zz_test_quick")
        caplog.set_level("INFO", logger="src.server")
        asyncio.run(_handler()(_request("zz_test_quick")))
        lines = [r for r in caplog.records if "zz_test_quick" in r.getMessage()]
        call = next(r for r in lines if "[tool] CALL" in r.getMessage())
        done = next(r for r in lines if "[tool] DONE" in r.getMessage())
        assert call.call_id == done.call_id != "-"
        for field in ("busy_threads=", "threads=", "rss_mib=", "peak_mib="):
            assert field in done.getMessage()

    def test_a_cancelled_call_is_logged_as_cancelled(self, stdio_ready, temporary_tools, caplog):
        release, entered = threading.Event(), threading.Event()

        def hold() -> str:
            entered.set()
            release.wait(_WAIT)
            return "done"

        temporary_tools(hold, "zz_test_cancelled")
        caplog.set_level("INFO", logger="src.server")

        async def _run():
            scopes = []

            async def caller():
                with anyio.CancelScope() as scope:
                    scopes.append(scope)
                    await _handler()(_request("zz_test_cancelled"))

            async with anyio.create_task_group() as tg:
                tg.start_soon(caller)
                await anyio.to_thread.run_sync(entered.wait, _WAIT)
                scopes[0].cancel()
                release.set()

        asyncio.run(_run())
        done = [r.getMessage() for r in caplog.records
                if "[tool] DONE" in r.getMessage() and "zz_test_cancelled" in r.getMessage()]
        assert done and "outcome=CANCELLED" in done[0]

    def test_http_mode_writes_each_record_on_one_line_with_the_call_id(self):
        import logging

        from src.call_context import set_call_id

        root = logging.getLogger()
        saved = list(root.handlers)
        try:
            server._use_single_line_logs()
            assert len(root.handlers) == 1
            token = set_call_id("call-77")
            try:
                record = logging.getLogger("src.server").makeRecord(
                    "src.server", logging.INFO, __file__, 1, "line one\nline two", (), None
                )
            finally:
                token.var.reset(token)
            text = root.handlers[0].format(record)
        finally:
            for handler in list(root.handlers):
                root.removeHandler(handler)
            for handler in saved:
                root.addHandler(handler)
        assert text.startswith("INFO [call-77] src.server: line one")
        assert "\n" not in text


class TestEverythingElseIsServedWhileToolsAreBusy:
    def test_static_files_and_the_api_key_check_run_while_every_tool_thread_is_busy(
        self, monkeypatch, tmp_path
    ):
        """The login and landing pages are served by StaticFiles on anyio's
        shared pool; the API-key check runs on asyncio's default executor
        (asyncio.to_thread). Neither may wait on tool threads."""
        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.staticfiles import StaticFiles

        from src.tool_threads import run_on_thread

        monkeypatch.setenv("OKAREO_TOOL_THREADS", "2")
        (tmp_path / "index.html").write_text("<h1>Okareo</h1>")
        app = Starlette(routes=[Mount("/", StaticFiles(directory=tmp_path, html=True))])
        release = threading.Event()
        started = threading.Barrier(3, timeout=_WAIT)

        def hold() -> str:
            started.wait()
            release.wait(_WAIT)
            return "done"

        wrapped = run_on_thread(hold)

        async def _run():
            calls = [asyncio.create_task(wrapped()) for _ in range(2)]
            await anyio.to_thread.run_sync(started.wait)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                page = await client.get("/")
            api_key_check = await asyncio.to_thread(lambda: "verified")
            release.set()
            await asyncio.gather(*calls)
            return page, api_key_check

        page, api_key_check = asyncio.run(_run())
        assert page.status_code == 200 and "Okareo" in page.text
        assert api_key_check == "verified"
