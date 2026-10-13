"""Concurrent run_simulation and run_test calls through the production server (049).

Calls go through ``src.server.mcp``'s own tool-call handler, so the worker
-thread hook, the call tag and the Run-starting module all run as they do in
production. The Okareo API is faked with one Run list shared by every call,
which is what makes a call that picks someone else's Run visible.

Each fake start waits at a barrier the calls can only pass together. On a
server that runs tools one at a time the barrier times out and the call fails.
"""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import mcp.types as types
import pytest

from src import server
from src.okareo_client import ResolvedProject, _reset_for_tests

_WAIT = 5.0
_SCENARIO = {"name": "billing-cases", "scenario_id": "sc-1", "scenario_count": 4}


class _Backend:
    """One Run list shared by every call, like the real API."""

    def __init__(self, parties: int, finish: bool):
        self.runs: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(parties, timeout=1.5)
        self.finish = finish
        self.release = threading.Event()
        # Run name -> seconds to wait before the backend registers that Run.
        self.register_after: dict[str, float] = {}

    def _create(self, kw) -> SimpleNamespace:
        with self.lock:
            rid = f"run-{len(self.runs) + 1}"
            self.runs[rid] = {
                "tags": list(kw.get("tags") or []),
                "name": kw.get("name"),
                "status": "FINISHED" if self.finish else "RUNNING",
                "app_link": f"https://app/{rid}",
            }
        return self._item(rid)

    def _item(self, rid):
        run = self.runs[rid]
        return SimpleNamespace(
            id=rid, name=run["name"], status=run["status"],
            failure_message=None, app_link=run["app_link"],
        )

    def submit_test(self, _mut=None, **kw):
        self.barrier.wait()
        return self._create(kw)

    def run_test(self, _mut=None, **kw):
        delay = self.register_after.get(kw.get("name"), 0.0)
        if delay:
            self.barrier.wait()
            threading.Event().wait(delay)
            item = self._create(kw)
        else:
            item = self._create(kw)
            self.barrier.wait()
        if not self.finish:
            self.release.wait(_WAIT)
        return item

    def fetch(self, client, api_key, run_id, timeout_seconds):
        return self._item(str(run_id))

    def find_by_tag(self, okareo, project_id, scenario_set_id, types, tag):
        from src.run_start import ListedRun

        with self.lock:
            return {
                rid: ListedRun(name=r["name"], app_link=r["app_link"], tags=r["tags"])
                for rid, r in self.runs.items() if tag in r["tags"]
            }


def _handler():
    return server.mcp._mcp_server.request_handlers[types.CallToolRequest]


def _in_a_request():
    """Bind an MCP request context, as the transport does for a real request.
    The tools read their provider keys from its lifespan context."""
    from mcp.server.lowlevel.server import request_ctx
    from mcp.shared.context import RequestContext

    request_ctx.set(
        RequestContext(
            request_id=1, meta=None, session=MagicMock(),
            lifespan_context={"key_registry": {}},
        )
    )


def _call(name, arguments):
    return _handler()(
        types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(name=name, arguments=arguments),
        )
    )


def _payload(result):
    root = result.root
    assert not root.isError, root.content[0].text
    return json.loads(root.content[0].text)


@pytest.fixture(autouse=True)
def _stdio_ready(monkeypatch):
    _reset_for_tests()
    monkeypatch.setenv("OKAREO_API_KEY", "test-key")
    monkeypatch.setenv("OKAREO_SIM_BUFFER_SECONDS", "2")
    monkeypatch.setattr(server, "_okareo_client", MagicMock())
    yield
    _reset_for_tests()


def _okareo():
    from okareo_api_client import Client

    okareo = MagicMock()
    okareo.api_key = "k"
    # A real client: the run_test path copies it without a timeout.
    okareo.client = Client(base_url="http://okareo.test", raise_on_unexpected_status=True)
    okareo.get_driver_by_name.return_value = SimpleNamespace(id="d1", name="caller")
    return okareo


def _simulation_patches(backend, target_type):
    from okareo_api_client.api import default as pkg

    scen_mod = MagicMock()
    scen_mod.sync.return_value = [_SCENARIO]
    target = SimpleNamespace(
        id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        name="Agent",
        models={target_type: {"type": target_type}},
    )
    return [
        patch("src.tools.simulations.get_okareo_client", side_effect=lambda: _okareo()),
        patch(
            "src.tools.simulations.resolve_project",
            return_value=ResolvedProject(id="p1", name="Demos", basis="explicit"),
        ),
        patch.object(pkg, "get_scenario_sets_v0_scenario_sets_get", scen_mod, create=True),
        patch("src.tools.simulations.resolve_artifact_by_name", return_value=target),
        patch("okareo.model_under_test.ModelUnderTest.submit_test", backend.submit_test),
        patch("okareo.model_under_test.ModelUnderTest.run_test", backend.run_test),
        patch("okareo.model_under_test.fetch_test_run", backend.fetch),
        patch("src.run_start.find_runs_by_tag", backend.find_by_tag),
    ]


def _run_burst(backend, target_type, n=5, names=None):
    patches = _simulation_patches(backend, target_type)
    for p in patches:
        p.start()
    try:
        async def _burst():
            _in_a_request()
            return await asyncio.gather(*[
                _call("run_simulation", {
                    "name": names[i] if names else "same-name",
                    "scenario_name": "billing-cases",
                    "target_name": "Agent",
                    "driver_name": "caller",
                })
                for i in range(n)
            ])

        return [_payload(r) for r in asyncio.run(_burst())]
    finally:
        backend.release.set()
        for p in patches:
            p.stop()


def _assert_each_call_got_its_own_run(backend, outs, tagged=True):
    ids = [o["test_run_id"] for o in outs]
    assert all(ids), outs
    assert len(set(ids)) == len(ids), f"calls shared a Run: {ids}"
    call_tags = [
        [t for t in backend.runs[rid]["tags"] if t.startswith("__mcp_call:")]
        for rid in ids
    ]
    if tagged:
        # Found by listing: each Run carries exactly its own call's tag.
        assert all(len(t) == 1 for t in call_tags), call_tags
        assert len({t[0] for t in call_tags}) == len(ids)
    else:
        # Submitted: the API handed back the Run; no system tag is added.
        assert all(not t for t in call_tags), call_tags


class TestABurstOfSimulations:
    def test_submitted_simulations_overlap_and_each_gets_its_own_run(self):
        backend = _Backend(parties=5, finish=False)
        outs = _run_burst(backend, "voice")
        assert all(o["status"] == "running" for o in outs), outs
        _assert_each_call_got_its_own_run(backend, outs, tagged=False)

    def test_run_test_path_simulations_overlap_and_each_gets_its_own_run(self):
        backend = _Backend(parties=5, finish=False)
        outs = _run_burst(backend, "custom_target")
        assert all(o["status"] == "running" for o in outs), outs
        _assert_each_call_got_its_own_run(backend, outs)


    def test_different_names_registering_out_of_order_each_get_their_own_run(self):
        """The old search took the only new Run when no name matched; Runs
        that register out of order handed calls each other's Runs."""
        backend = _Backend(parties=5, finish=False)
        names = [f"sim-{i}" for i in range(5)]
        backend.register_after = {"sim-0": 0.3, "sim-1": 0.2, "sim-2": 0.1}
        outs = _run_burst(backend, "custom_target", names=names)
        _assert_each_call_got_its_own_run(backend, outs)
        for out in outs:
            assert backend.runs[out["test_run_id"]]["name"] == out["name"]

    def test_a_run_that_registers_after_its_window_is_never_handed_to_another_call(self):
        backend = _Backend(parties=5, finish=False)
        names = [f"sim-{i}" for i in range(5)]
        backend.register_after = {"sim-0": 3.0}  # past the 2 s window
        outs = _run_burst(backend, "custom_target", names=names)
        late = next(o for o in outs if o["name"] == "sim-0")
        assert late["status"] == "running" and late["test_run_id"] == ""
        others = [o for o in outs if o["name"] != "sim-0"]
        _assert_each_call_got_its_own_run(backend, others)
        for out in others:
            assert backend.runs[out["test_run_id"]]["name"] == out["name"]


class TestTwoTestsWithTheDefaultName:
    def test_each_run_test_call_gets_its_own_run(self):
        from okareo_api_client.api import default as pkg

        backend = _Backend(parties=2, finish=False)
        scen_mod = MagicMock()
        scen_mod.sync.return_value = [SimpleNamespace(name="my-scenario", scenario_id="sid")]
        okareo = _okareo()
        okareo.get_all_checks.return_value = [SimpleNamespace(name="coherence", id="c1")]
        resolved = MagicMock()
        resolved.id = "mut-1"
        resolved.name = "my-model"
        resolved.models = {}
        patches = [
            patch("src.tools.tests.get_okareo_client", return_value=okareo),
            patch(
                "src.tools.tests.resolve_project",
                return_value=ResolvedProject(id="p1", name="Global", basis="default"),
            ),
            patch.object(pkg, "get_scenario_sets_v0_scenario_sets_get", scen_mod, create=True),
            patch("src.tools.tests.resolve_artifact_by_name", return_value=resolved),
            patch("okareo.model_under_test.ModelUnderTest.run_test", backend.run_test),
            patch("src.run_start.find_runs_by_tag", backend.find_by_tag),
        ]
        for p in patches:
            p.start()
        try:
            async def _both():
                _in_a_request()
                return await asyncio.gather(*[
                    _call("run_test", {
                        "scenario_name": "my-scenario",
                        "model_name": "my-model",
                        "checks": ["coherence"],
                    })
                    for _ in range(2)
                ])

            outs = [_payload(r) for r in asyncio.run(_both())]
        finally:
            backend.release.set()
            for p in patches:
                p.stop()
        assert [o["name"] for o in outs] == ["my-scenario-my-model"] * 2
        _assert_each_call_got_its_own_run(backend, outs)


class TestNoThreadIsLeftBehind:
    @pytest.mark.parametrize("target_type", ["voice", "custom_target"])
    def test_a_simulation_leaves_no_thread_running_after_it_ends(self, target_type):
        def _ours():
            return {
                t for t in threading.enumerate()
                if not t.name.startswith("AnyIO worker thread")
            }

        before = _ours()
        backend = _Backend(parties=1, finish=True)
        outs = _run_burst(backend, target_type, n=1)
        assert outs[0]["status"] == "finished"
        for t in _ours() - before:
            t.join(_WAIT)
        leftover = {t.name for t in _ours() - before if t.is_alive()}
        assert not leftover, f"threads left running: {leftover}"


class TestSubmitErrorsMatchRunTest:
    """A Run that fails to start reads the same whichever path started it:
    ModelUnderTest.run_test wraps every error in TestRunError, so the submit
    path does too (FR-005). A timeout keeps the timeout error other tools give."""

    def _fail_with(self, exc):
        backend = _Backend(parties=1, finish=True)

        def refuse(_mut=None, **kw):
            raise exc

        backend.submit_test = refuse
        patches = _simulation_patches(backend, "voice")
        for p in patches:
            p.start()
        try:
            async def _one():
                _in_a_request()
                return await _call("run_simulation", {
                    "name": "sim", "scenario_name": "billing-cases",
                    "target_name": "Agent", "driver_name": "caller",
                })
            return _payload(asyncio.run(_one()))
        finally:
            for p in patches:
                p.stop()

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_an_unexpected_status_reads_as_run_test_reported_it(self, status):
        from okareo.model_under_test import TestRunError
        from okareo_api_client.errors import UnexpectedStatus

        from src.error_handling import format_tool_error

        exc = UnexpectedStatus(status, b'{"detail": "backend said no"}')
        expected = json.loads(format_tool_error(TestRunError(str(exc)), {}))
        assert self._fail_with(exc) == expected

    def test_a_connection_error_reads_as_run_test_reported_it(self):
        import httpx
        from okareo.model_under_test import TestRunError

        from src.error_handling import format_tool_error

        exc = httpx.ConnectError("connection refused")
        expected = json.loads(format_tool_error(TestRunError(str(exc)), {}))
        assert self._fail_with(exc) == expected

    def test_a_timeout_keeps_the_timeout_error(self):
        import httpx

        from src.error_handling import format_tool_error

        exc = httpx.ReadTimeout("read timed out")
        assert self._fail_with(exc) == json.loads(format_tool_error(exc, {}))
