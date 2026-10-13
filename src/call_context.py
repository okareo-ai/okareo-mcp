"""The id of the tool call in progress, carried on every log record it causes.

Tool calls run side by side, so their log lines interleave. A context
variable holds the current call's id; a log-record factory copies it onto
every record as ``call_id``. Worker threads and a Run's background thread run
in a copy of the caller's context, so their records carry the same id.
"""

import logging
import uuid
from contextvars import ContextVar, Token

_NO_CALL = "-"
_call_id: ContextVar[str] = ContextVar("okareo_call_id", default=_NO_CALL)

# The format of every record in HTTP mode: one entry per record, call id first.
HTTP_LOG_FORMAT = "%(levelname)s [%(call_id)s] %(name)s: %(message)s"


def new_call_id() -> str:
    """A short id, unique enough to tell one call's lines from another's."""
    return uuid.uuid4().hex[:12]


def set_call_id(value: str) -> Token[str]:
    """Make ``value`` the current call's id; reset with the returned token."""
    return _call_id.set(value)


def current_call_id() -> str:
    return _call_id.get()


def install_log_record_factory() -> None:
    """Add ``call_id`` to every log record. Safe to call more than once."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_okareo_call_id", False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.call_id = _call_id.get()
        return record

    factory._okareo_call_id = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


class _SingleLineFormatter(logging.Formatter):
    """Writes each record, traceback included, on one line.

    Cloud Run stores every line of stderr as its own log entry, so a wrapped
    or multi-line record is split apart and its pieces can't be searched or
    tied back to the call.
    """

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "call_id"):
            record.call_id = _NO_CALL
        return super().format(record).replace("\r", "").replace("\n", " \\n ")


def single_line_formatter() -> logging.Formatter:
    return _SingleLineFormatter(HTTP_LOG_FORMAT)
