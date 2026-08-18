"""Unit tests for src/analytics_context.py — scope lifecycle and allow-list."""

from __future__ import annotations

import contextvars
import threading

from src.analytics_context import (
    ALLOWED_KEYS,
    _reset_for_tests,
    annotate,
    call_scope,
)


def setup_function():
    _reset_for_tests()


def teardown_function():
    _reset_for_tests()


class TestCallScopeLifecycle:
    def test_annotate_noop_outside_scope(self):
        annotate(project_id="should-not-appear")  # must not raise

    def test_fresh_dict_per_scope(self):
        with call_scope() as a:
            annotate(project_id="first")
            assert a == {"project_id": "first"}
        with call_scope() as b:
            assert b == {}
            annotate(entity_type="scenario")
            assert b == {"entity_type": "scenario"}

    def test_scope_resets_on_exception(self):
        try:
            with call_scope() as bucket:
                annotate(project_id="x")
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        # Outside the scope the ContextVar is unbound again.
        with call_scope() as bucket:
            assert bucket == {}

    def test_concurrent_scopes_never_share_keys(self):
        seen: dict[str, dict] = {}
        barrier = threading.Barrier(2)

        def worker(label: str):
            with call_scope() as bucket:
                annotate(project_id=label)
                barrier.wait()
                seen[label] = dict(bucket)

        t1 = threading.Thread(target=worker, args=("A",))
        t2 = threading.Thread(target=worker, args=("B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert seen["A"] == {"project_id": "A"}
        assert seen["B"] == {"project_id": "B"}

    def test_annotations_survive_copy_context(self):
        with call_scope() as bucket:
            ctx = contextvars.copy_context()

            def _mutate():
                annotate(entity_id="from-copied-ctx")

            ctx.run(_mutate)
            assert bucket["entity_id"] == "from-copied-ctx"


class TestAllowList:
    def test_unknown_keys_dropped(self):
        with call_scope() as bucket:
            annotate(scenario_name="secret", project_id="ok")
            assert bucket == {"project_id": "ok"}

    def test_dict_list_object_dropped(self):
        with call_scope() as bucket:
            annotate(
                project_id="ok",
                entity_id={"nested": True},  # type: ignore[arg-type]
            )
            annotate(result_count=[1, 2, 3])  # type: ignore[arg-type]
            annotate(is_rerun=object())  # type: ignore[arg-type]
            assert bucket == {"project_id": "ok"}

    def test_none_dropped_as_absent(self):
        with call_scope() as bucket:
            annotate(project_id="ok", entity_id=None)
            assert bucket == {"project_id": "ok"}

    def test_strings_over_64_truncated(self):
        with call_scope() as bucket:
            annotate(language="x" * 100)
            assert bucket["language"] == "x" * 64

    def test_annotate_never_raises(self):
        annotate(**{k: "v" for k in list(ALLOWED_KEYS)[:3]})
        annotate(**{"not-a-key": object()})  # type: ignore[arg-type]
        with call_scope():
            annotate(project_id={"nope": 1})  # type: ignore[arg-type]
