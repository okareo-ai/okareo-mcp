"""Unit tests for the faux-async simulation handoff (spec 025).

Covers the runtime estimate, the bounded background-thread buffering
(finished / running / failed outcomes), and the handoff response builder.
"""

import threading

from src.tools.simulations import (
    _build_handoff_response,
    _buffered_submit,
    _estimate_runtime_seconds,
    _format_duration,
)


def _fake_result(rid="run-xyz", link="http://app/x"):
    class R:
        pass

    r = R()
    r.id = rid
    r.name = "the-run"
    r.app_link = link
    return r


class TestEstimate:
    def test_voice_slower_than_text(self):
        assert _estimate_runtime_seconds(11, 5, 1, True) > _estimate_runtime_seconds(
            11, 5, 1, False
        )

    def test_monotonic_in_each_input(self):
        assert _estimate_runtime_seconds(20, 5, 1, False) > _estimate_runtime_seconds(
            5, 5, 1, False
        )
        assert _estimate_runtime_seconds(11, 10, 1, False) > _estimate_runtime_seconds(
            11, 2, 1, False
        )
        assert _estimate_runtime_seconds(11, 5, 3, False) > _estimate_runtime_seconds(
            11, 5, 1, False
        )

    def test_format_duration(self):
        assert _format_duration(45).endswith("s")
        assert "min" in _format_duration(600)


class TestBufferedSubmit:
    def test_finished_inline(self, monkeypatch):
        monkeypatch.setattr(
            "src.tools.simulations._find_runs", lambda *a, **k: {}
        )
        status, payload, run_id, link = _buffered_submit(
            lambda: _fake_result("done-1"),
            okareo=None,
            project_id="p",
            scenario_set_id="s",
            name="the-run",
        )
        assert status == "finished"
        assert payload.id == "done-1"

    def test_failed_inline(self, monkeypatch):
        monkeypatch.setattr(
            "src.tools.simulations._find_runs", lambda *a, **k: {}
        )

        def boom():
            raise ValueError("bad config")

        status, payload, run_id, link = _buffered_submit(
            boom, okareo=None, project_id="p", scenario_set_id="s", name="the-run"
        )
        assert status == "failed"
        assert isinstance(payload, ValueError)

    def test_running_handoff_returns_discovered_id(self, monkeypatch):
        monkeypatch.setenv("OKAREO_SIM_BUFFER_SECONDS", "0.3")
        monkeypatch.setattr("src.tools.simulations._SIM_POLL_INTERVAL_SECONDS", 0.05)

        release = threading.Event()
        calls = {"n": 0}

        def fake_find(okareo, project_id, scenario_set_id, types):
            calls["n"] += 1
            if calls["n"] == 1:  # snapshot before the run is created
                return {}
            return {"new-run-1": {"name": "the-run", "app_link": "http://app/new"}}

        monkeypatch.setattr("src.tools.simulations._find_runs", fake_find)

        def blocker():
            release.wait(5)
            return _fake_result()

        try:
            status, payload, run_id, link = _buffered_submit(
                blocker,
                okareo=None,
                project_id="p",
                scenario_set_id="s",
                name="the-run",
            )
            assert status == "running"
            assert payload is None
            assert run_id == "new-run-1"
            assert link == "http://app/new"
        finally:
            release.set()  # let the daemon thread complete


class TestHandoffResponse:
    def test_running_includes_estimate_and_extra(self):
        resp = _build_handoff_response(
            "running", None, "rid", "http://l",
            name="the-run", project_id="p", estimate_seconds=120,
            based_on_run_id=None, extra={"rows": 5, "target": "T"},
        )
        assert resp["status"] == "running"
        assert resp["test_run_id"] == "rid"
        assert resp["app_link"] == "http://l"
        assert "estimated_runtime" in resp
        assert resp["estimated_runtime_seconds"] == 120
        assert resp["rows"] == 5
        assert resp["target"] == "T"

    def test_running_without_id_constructs_link_and_warns(self):
        resp = _build_handoff_response(
            "running", None, None, "",
            name="the-run", project_id="proj-1", estimate_seconds=10,
            based_on_run_id=None, extra=None,
        )
        assert resp["test_run_id"] == ""
        # No id means no eval link can be constructed.
        assert resp["app_link"] == ""
        assert "list_simulations" in resp["message"]

    def test_finished_uses_result_id(self):
        resp = _build_handoff_response(
            "finished", _fake_result("done-1"), None, "",
            name="the-run", project_id="p", estimate_seconds=10,
            based_on_run_id="orig-run", extra=None,
        )
        assert resp["status"] == "finished"
        assert resp["test_run_id"] == "done-1"
        assert resp["based_on_run_id"] == "orig-run"
        # No estimate noise on a completed run.
        assert "estimated_runtime" not in resp
