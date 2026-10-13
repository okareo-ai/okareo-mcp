"""Every log record a tool call causes carries that call's id (049).

With tools running side by side, the lines of overlapping calls interleave.
The call id ties them back together, including lines written from the tool's
worker thread and from a Run's background thread. In HTTP mode each record is
one log entry with the id first, so Cloud Run doesn't split it.
"""

import asyncio
import logging

import anyio

from src.call_context import (
    current_call_id,
    install_log_record_factory,
    new_call_id,
    set_call_id,
    single_line_formatter,
)


def _capture(logger_name: str):
    records: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger(logger_name)
    handler = _Keep()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return records, lambda: logger.removeHandler(handler)


def test_records_outside_a_call_carry_a_dash():
    install_log_record_factory()
    records, done = _capture("test.call_context.outside")
    try:
        logging.getLogger("test.call_context.outside").info("hello")
    finally:
        done()
    assert records[0].call_id == "-"


def test_a_record_from_a_worker_thread_carries_the_callers_id():
    install_log_record_factory()
    records, done = _capture("test.call_context.thread")

    async def _call():
        set_call_id("call-123")
        await anyio.to_thread.run_sync(
            lambda: logging.getLogger("test.call_context.thread").info("from the thread")
        )

    try:
        asyncio.run(_call())
    finally:
        done()
    assert records[0].call_id == "call-123"


def test_a_record_from_a_runs_background_thread_carries_the_callers_id():
    from src.run_start import run_and_find

    install_log_record_factory()
    records, done = _capture("test.call_context.run")

    def run(tags):
        logging.getLogger("test.call_context.run").info("from the run thread")
        return type("R", (), {"id": "r1", "status": "FINISHED", "app_link": ""})()

    token = set_call_id("call-run")
    try:
        run_and_find(
            run, tags=["t"], call_tag="t",
            find_runs=lambda tag: {}, window_seconds=1.0, poll_seconds=0.01,
        )
    finally:
        done()
        token.var.reset(token)
    assert records[0].call_id == "call-run"


def test_installing_twice_doesnt_wrap_twice():
    install_log_record_factory()
    first = logging.getLogRecordFactory()
    install_log_record_factory()
    assert logging.getLogRecordFactory() is first


def test_call_ids_are_short_and_distinct():
    a, b = new_call_id(), new_call_id()
    assert a != b
    assert 8 <= len(a) <= 16


def test_current_call_id_defaults_to_a_dash():
    async def _fresh():
        return current_call_id()

    assert asyncio.run(_fresh()) == "-"


def test_the_http_format_is_one_line_with_the_call_id_first():
    install_log_record_factory()
    formatter = single_line_formatter()
    token = set_call_id("call-9")
    try:
        try:
            raise RuntimeError("Cannot send a request, as the client has been closed")
        except RuntimeError:
            record = logging.getLogger("src.analytics").makeRecord(
                "src.analytics", logging.ERROR, __file__, 1,
                "PostHog capture send failed for event=%s", ("okareo_mcp_tool_call",),
                exc_info=__import__("sys").exc_info(),
            )
    finally:
        token.var.reset(token)
    line = formatter.format(record)
    assert "\n" not in line
    assert line.startswith("ERROR [call-9] src.analytics: PostHog capture send failed")
    assert "Cannot send a request, as the client has been closed" in line
