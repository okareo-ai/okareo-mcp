"""Starting a Run and finding the Run each call started (049).

Two paths. For the Target types the backend queues, the Run is submitted and
the API answers with it; its status is then read until it ends or the buffer
window closes. Everything else runs on a background thread with ``run_test``,
and the Run is found by listing the Scenario's Runs filtered by a tag unique
to the call. These tests pin both paths' outcomes, which must match what the
tools returned before (finished, running, failed), and the cases the old
name-and-difference search got wrong.
"""

import contextvars
import threading
from types import SimpleNamespace

import pytest

from src import run_start
from src.run_start import (
    CALL_TAG_PREFIX,
    ListedRun,
    new_call_tag,
    run_and_find,
    start_run,
    submit_and_poll,
    submits,
)

_WAIT = 5.0


def _item(rid, status="RUNNING", failure=None, link=None):
    return SimpleNamespace(
        id=rid,
        name=f"run-{rid}",
        status=status,
        failure_message=failure,
        app_link=link or f"https://app/{rid}",
    )


class _Clock:
    """A clock the test advances; sleeping advances it."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class TestCallTags:
    def test_a_tag_is_unique_to_each_call_and_marked_as_a_system_tag(self):
        a, b = new_call_tag(), new_call_tag()
        assert a != b
        assert a.startswith(CALL_TAG_PREFIX)
        assert CALL_TAG_PREFIX.startswith("__")


class TestWhichTargetsAreSubmitted:
    @pytest.mark.parametrize(
        "target_type",
        ["voice", "custom_endpoint", "generation", "openai", "openai_assistant", "cohere"],
    )
    def test_queued_target_types_are_submitted(self, target_type):
        assert submits(target_type) is True

    @pytest.mark.parametrize(
        "target_type",
        ["custom", "custom_batch", "custom_target", "custom_target_async", None, ""],
    )
    def test_everything_else_keeps_run_test(self, target_type):
        assert submits(target_type) is False


class TestSubmitAndPoll:
    def _poll(self, submit, statuses, window=25.0, **kw):
        clock = _Clock()
        reads = iter(statuses)

        def read_status(run_id, timeout):
            value = next(reads)
            if isinstance(value, Exception):
                raise value
            return value

        return submit_and_poll(
            submit,
            tags=["__mcp_call:t1"],
            window_seconds=window,
            poll_seconds=2.0,
            read_status=read_status,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            **kw,
        )

    def test_finished_inside_the_window_gives_finished_with_that_run(self):
        done = _item("r1", "FINISHED")
        out = self._poll(lambda tags: _item("r1"), [_item("r1"), done])
        assert out.status == "finished"
        assert out.result is done
        assert out.run_id == "r1"

    def test_outlasting_the_window_gives_running_with_the_id(self):
        out = self._poll(lambda tags: _item("r1"), [_item("r1")] * 50, window=6.0)
        assert out.status == "running"
        assert out.result is None
        assert out.run_id == "r1"
        assert out.app_link == "https://app/r1"

    def test_failed_inside_the_window_gives_todays_failure(self):
        from okareo.model_under_test import TestRunError

        out = self._poll(
            lambda tags: _item("r1"),
            [_item("r1", "FAILED", failure="latency check rejected")],
        )
        assert out.status == "failed"
        assert isinstance(out.result, TestRunError)
        assert str(out.result) == "latency check rejected"
        assert out.run_id == "r1"

    def test_a_failed_run_with_no_message_says_so(self):
        out = self._poll(lambda tags: _item("r1"), [_item("r1", "FAILED")])
        assert str(out.result) == "Test run failed."

    def test_submit_raising_gives_the_same_error_run_test_gives(self):
        """ModelUnderTest.run_test wraps every error in TestRunError; submit
        must too, so the tool's error response doesn't change (FR-005)."""
        from okareo.model_under_test import TestRunError

        cause = ValueError("500: backend error")

        def refuse(tags):
            raise cause

        out = self._poll(refuse, [])
        assert out.status == "failed"
        assert isinstance(out.result, TestRunError)
        assert str(out.result) == "500: backend error"
        assert out.result.__cause__ is cause
        assert out.run_id is None

    def test_a_submit_that_times_out_keeps_the_timeout_error_other_tools_give(self):
        import httpx

        timeout = httpx.ReadTimeout("read timed out")

        def slow(tags):
            raise timeout

        out = self._poll(slow, [])
        assert out.status == "failed"
        assert out.result is timeout

    def test_a_window_of_zero_hands_back_the_run_at_once(self):
        """The OKAREO_SIM_BUFFER_SECONDS=0 stopgap: no waiting, the Run's id
        straight from the submit answer."""
        out = self._poll(lambda tags: _item("r1"), [], window=0.0)
        assert out.status == "running"
        assert out.run_id == "r1"

    def test_run_kwargs_may_not_carry_tags(self):
        with pytest.raises(ValueError):
            start_run(
                lambda client: None, submit=True, run_kwargs={"tags": ["x"]},
                okareo=SimpleNamespace(client=None, api_key="k"),
                project_id="p", scenario_set_id="s",
            )

    def test_a_submit_answer_without_a_run_id_fails_instead_of_inventing_one(self):
        out = self._poll(lambda tags: _item(None), [])
        assert out.status == "failed"
        assert out.run_id is None

    def test_a_status_read_that_fails_once_is_retried(self):
        done = _item("r1", "FINISHED")
        out = self._poll(lambda tags: _item("r1"), [TimeoutError("slow"), done])
        assert out.status == "finished"
        assert out.result is done

    @pytest.mark.parametrize("status", [None, "UNKNOWN", "RUNNING", ""])
    def test_a_status_that_is_not_terminal_keeps_polling(self, status):
        done = _item("r1", "FINISHED")
        out = self._poll(lambda tags: _item("r1"), [_item("r1", status), done])
        assert out.status == "finished"

    def test_the_given_tags_reach_submit(self):
        seen = {}

        def submit(tags):
            seen["tags"] = tags
            return _item("r1", "FINISHED")

        clock = _Clock()
        submit_and_poll(
            submit,
            tags=["mine", "__mcp_call:abc"],
            window_seconds=1.0,
            poll_seconds=2.0,
            read_status=lambda run_id, timeout: _item("r1", "FINISHED"),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        assert seen["tags"] == ["mine", "__mcp_call:abc"]

    def test_status_reads_carry_a_timeout_no_longer_than_the_window_left(self):
        timeouts = []
        clock = _Clock()

        def read_status(run_id, timeout):
            timeouts.append(timeout)
            return _item("r1")

        submit_and_poll(
            lambda tags: _item("r1"),
            tags=["t"],
            window_seconds=7.0,
            poll_seconds=2.0,
            read_status=read_status,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        assert timeouts
        assert all(0 < t <= run_start.STATUS_READ_TIMEOUT_SECONDS for t in timeouts)


class TestRunAndFind:
    """The ``run_test`` path: the Run is found only by the call's tag."""

    def test_the_run_is_found_by_its_tag_and_handed_back_while_running(self):
        release = threading.Event()
        registered = threading.Event()
        runs = {}

        def run(tags):
            runs["r-own"] = ListedRun(name="sim", app_link="https://app/own", tags=list(tags))
            registered.set()
            release.wait(_WAIT)
            return _item("r-own", "FINISHED")

        def find_runs(tag):
            registered.wait(_WAIT)
            return {rid: r for rid, r in runs.items() if tag in r.tags}

        try:
            out = run_and_find(
                run,
                tags=["__mcp_call:own"],
                call_tag="__mcp_call:own",
                find_runs=find_runs,
                window_seconds=0.3,
                poll_seconds=0.02,
            )
        finally:
            release.set()
        assert out.status == "running"
        assert out.run_id == "r-own"
        assert out.app_link == "https://app/own"

    def test_a_run_that_registers_after_its_window_is_never_handed_to_another_call(self):
        """A's Run appears only after A's window ended, during B's window.
        Tag lookup means B can't mistake it for its own."""
        runs = {}
        a_window_over = threading.Event()
        b_registered = threading.Event()
        release = threading.Event()

        def run_a(tags):
            a_window_over.wait(_WAIT)
            runs["r-a"] = ListedRun(name="same-name", app_link="", tags=list(tags))
            release.wait(_WAIT)
            return _item("r-a", "FINISHED")

        def run_b(tags):
            runs["r-b"] = ListedRun(name="same-name", app_link="", tags=list(tags))
            b_registered.set()
            release.wait(_WAIT)
            return _item("r-b", "FINISHED")

        def finder(tag):
            return {rid: r for rid, r in runs.items() if tag in r.tags}

        try:
            out_a = run_and_find(
                run_a, tags=["__mcp_call:a"], call_tag="__mcp_call:a",
                find_runs=finder, window_seconds=0.1, poll_seconds=0.02,
            )
            a_window_over.set()
            out_b = run_and_find(
                run_b, tags=["__mcp_call:b"], call_tag="__mcp_call:b",
                find_runs=finder, window_seconds=0.3, poll_seconds=0.02,
            )
        finally:
            release.set()
        assert out_a.status == "running" and out_a.run_id is None
        assert out_b.run_id == "r-b"

    def test_a_failed_listing_picks_no_run(self):
        release = threading.Event()

        def run(tags):
            release.wait(_WAIT)
            return _item("r1", "FINISHED")

        def broken_find(tag):
            raise RuntimeError("find_test_runs failed")

        try:
            out = run_and_find(
                run, tags=["__mcp_call:x"], call_tag="__mcp_call:x",
                find_runs=broken_find, window_seconds=0.1, poll_seconds=0.02,
            )
        finally:
            release.set()
        assert out.status == "running"
        assert out.run_id is None

    def test_a_listing_that_ignores_the_tag_picks_no_run(self):
        """If the backend ever returned Runs without the tag, take none of them."""
        release = threading.Event()

        def run(tags):
            release.wait(_WAIT)
            return _item("r1", "FINISHED")

        def unfiltered(tag):
            return {
                "someone-else": ListedRun(name="n", app_link="", tags=["__mcp_call:other"]),
            }

        try:
            out = run_and_find(
                run, tags=["__mcp_call:x"], call_tag="__mcp_call:x",
                find_runs=unfiltered, window_seconds=0.1, poll_seconds=0.02,
            )
        finally:
            release.set()
        assert out.run_id is None

    def test_finished_inline_gives_finished_with_the_result(self):
        done = _item("r1", "FINISHED")
        out = run_and_find(
            lambda tags: done, tags=["t"], call_tag="t",
            find_runs=lambda tag: {}, window_seconds=1.0, poll_seconds=0.02,
        )
        assert out.status == "finished"
        assert out.result is done
        assert out.run_id == "r1"

    def test_raising_inline_gives_failed_with_the_exception(self):
        def boom(tags):
            raise ValueError("bad config")

        out = run_and_find(
            boom, tags=["t"], call_tag="t",
            find_runs=lambda tag: {}, window_seconds=1.0, poll_seconds=0.02,
        )
        assert out.status == "failed"
        assert isinstance(out.result, ValueError)

    def test_the_background_thread_sees_the_callers_context(self):
        marker = contextvars.ContextVar("marker", default=None)
        seen = {}

        def run(tags):
            seen["marker"] = marker.get()
            return _item("r1", "FINISHED")

        token = marker.set("caller")
        try:
            run_and_find(
                run, tags=["t"], call_tag="t",
                find_runs=lambda tag: {}, window_seconds=1.0, poll_seconds=0.02,
            )
        finally:
            marker.reset(token)
        assert seen["marker"] == "caller"

    def test_on_done_runs_when_the_run_ends_not_when_the_call_returns(self):
        release = threading.Event()
        closed = threading.Event()

        def run(tags):
            release.wait(_WAIT)
            return _item("r1", "FINISHED")

        out = run_and_find(
            run, tags=["t"], call_tag="t",
            find_runs=lambda tag: {}, window_seconds=0.05, poll_seconds=0.01,
            on_done=closed.set,
        )
        assert out.status == "running"
        assert not closed.is_set()
        release.set()
        assert closed.wait(_WAIT)


class _RunClient:
    def get_httpx_client(self):
        return self

    def close(self):
        pass


@pytest.fixture
def untimed_client(monkeypatch):
    monkeypatch.setattr(run_start, "run_request_client", lambda okareo: _RunClient())


class TestStartRun:
    def test_submitted_runs_close_their_model_when_done(self):
        closed = []
        mut = SimpleNamespace(
            submit_test=lambda **kw: _item("r1", "FINISHED"),
            run_test=lambda **kw: pytest.fail("run_test must not be called"),
            close=lambda: closed.append(True),
        )
        out = start_run(
            lambda client: mut, submit=True, run_kwargs={"name": "sim"},
            okareo=SimpleNamespace(client=None, api_key="k"),
            project_id="p", scenario_set_id="s", tags=None,
            read_status=lambda run_id, timeout: _item("r1", "FINISHED"),
        )
        assert out.status == "finished"
        assert closed == [True]

    def test_both_paths_pass_the_same_arguments_and_only_run_test_adds_the_call_tag(
        self, untimed_client
    ):
        calls = {}

        def record(method):
            def _call(**kw):
                calls[method] = kw
                return _item("r1", "FINISHED")
            return _call

        mut = SimpleNamespace(
            submit_test=record("submit_test"), run_test=record("run_test"), close=lambda: None,
        )
        kwargs = {"name": "sim", "checks": ["latency"], "driver_id": "d1"}
        for submit in (True, False):
            start_run(
                lambda client: mut, submit=submit, run_kwargs=dict(kwargs),
                okareo=SimpleNamespace(client=None, api_key="k"),
                project_id="p", scenario_set_id="s", tags=["mine"],
                read_status=lambda run_id, timeout: _item("r1", "FINISHED"),
                find_runs=lambda *a, **k: {},
            )
        for method in ("submit_test", "run_test"):
            sent = dict(calls[method])
            sent.pop("tags")
            assert sent == kwargs
        # Submit answers with the Run, so it needs no call tag; only the
        # run_test path, which finds its Run by listing, carries one.
        assert calls["submit_test"]["tags"] == ["mine"]
        run_tags = calls["run_test"]["tags"]
        assert run_tags[0] == "mine"
        assert run_tags[1].startswith(CALL_TAG_PREFIX)

    def test_each_path_builds_the_model_on_the_client_it_needs(self, monkeypatch):
        tool_client, run_client = object(), _RunClient()
        monkeypatch.setattr(run_start, "run_request_client", lambda okareo: run_client)
        clients = {}

        def build(path):
            def _build(client):
                clients[path] = client
                return SimpleNamespace(
                    submit_test=lambda **kw: _item("r1", "FINISHED"),
                    run_test=lambda **kw: _item("r1", "FINISHED"),
                    close=lambda: None,
                )
            return _build

        okareo = SimpleNamespace(client=tool_client, api_key="k")
        for submit in (True, False):
            start_run(
                build(submit), submit=submit, run_kwargs={}, okareo=okareo,
                project_id="p", scenario_set_id="s",
                read_status=lambda run_id, timeout: _item("r1", "FINISHED"),
                find_runs=lambda *a, **k: {},
            )
        assert clients[True] is tool_client
        assert clients[False] is run_client

    def test_the_run_test_path_closes_its_model_and_client_when_the_run_ends(self, monkeypatch):
        closed = []

        class _Client:
            def get_httpx_client(self):
                return self

            def close(self):
                closed.append("client")

        monkeypatch.setattr(run_start, "run_request_client", lambda okareo: _Client())
        release = threading.Event()
        mut = SimpleNamespace(
            run_test=lambda **kw: (release.wait(_WAIT), _item("r1", "FINISHED"))[1],
            close=lambda: closed.append("model"),
        )
        out = start_run(
            lambda client: mut, submit=False, run_kwargs={},
            okareo=SimpleNamespace(client=None, api_key="k"),
            project_id="p", scenario_set_id="s", window_seconds=0.05,
            poll_seconds=0.01, find_runs=lambda *a, **k: {},
        )
        assert out.status == "running"
        assert closed == []
        release.set()
        for _ in range(500):
            if len(closed) == 2:
                break
            threading.Event().wait(0.01)
        assert closed == ["model", "client"]

    def test_polling_never_uses_the_sdk_helpers_that_print(self, monkeypatch, capsys):
        import okareo.model_under_test as sdk

        monkeypatch.setattr(
            sdk, "wait_for_test_run", lambda *a, **k: pytest.fail("prints to stdout"),
            raising=False,
        )
        reads = []

        def fake_fetch(client, api_key, run_id, timeout_seconds):
            reads.append(timeout_seconds)
            return _item(run_id, "FINISHED")

        monkeypatch.setattr(sdk, "fetch_test_run", fake_fetch)
        mut = SimpleNamespace(
            submit_test=lambda **kw: _item("r1"),
            get_test_run=lambda *a, **k: pytest.fail("prints and has no timeout"),
            close=lambda: None,
        )
        out = start_run(
            lambda client: mut, submit=True, run_kwargs={},
            okareo=SimpleNamespace(client=object(), api_key="k"),
            project_id="p", scenario_set_id="s", tags=None,
        )
        assert out.status == "finished"
        assert reads
        assert capsys.readouterr().out == ""


class TestRealModelsLeaveNoThread:
    """okareo 0.0.159 starts a ``ModelUnderTest``'s datapoint thread when the
    model is built and has no ``close()``; ``start_run`` stops it on both
    paths without printing (FR-010, research R15)."""

    @staticmethod
    def _builder(stand_in_model, built):
        def _build(client):
            built.append(stand_in_model())
            return built[-1]

        return _build

    def test_a_submitted_run_leaves_no_model_thread(
        self, monkeypatch, capfd, stand_in_model
    ):
        from okareo.model_under_test import ModelUnderTest

        monkeypatch.setattr(
            ModelUnderTest, "submit_test", lambda self, **kw: _item("r1", "FINISHED")
        )
        built = []
        out = start_run(
            self._builder(stand_in_model, built), submit=True,
            run_kwargs={"name": "sim"},
            okareo=SimpleNamespace(client=None, api_key="k"),
            project_id="p", scenario_set_id="s",
            read_status=lambda run_id, timeout: pytest.fail("finished at submit"),
        )
        assert out.status == "finished"
        assert not built[0].worker_thread.is_alive()
        assert capfd.readouterr().out == ""

    def test_a_run_test_run_leaves_no_model_thread_once_it_ends(
        self, monkeypatch, capfd, stand_in_model, untimed_client
    ):
        from okareo.model_under_test import ModelUnderTest

        release = threading.Event()
        monkeypatch.setattr(
            ModelUnderTest, "run_test",
            lambda self, **kw: (release.wait(_WAIT), _item("r1", "FINISHED"))[1],
        )
        built = []
        out = start_run(
            self._builder(stand_in_model, built), submit=False, run_kwargs={},
            okareo=SimpleNamespace(client=None, api_key="k"),
            project_id="p", scenario_set_id="s", window_seconds=0.05,
            poll_seconds=0.01, find_runs=lambda *a, **k: {},
        )
        assert out.status == "running"
        thread = built[0].worker_thread
        assert thread.is_alive()  # the Run still has its model
        release.set()
        thread.join(_WAIT)
        assert not thread.is_alive()
        assert capfd.readouterr().out == ""
