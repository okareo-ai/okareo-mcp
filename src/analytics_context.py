"""Request-scoped analytics annotation channel.

Tools call ``annotate(**props)`` to contribute context to the analytics event
for the current tool invocation. The instrumented CallTool wrapper opens a
``call_scope()`` around dispatch and merges the resulting dict into the event.

A mutable dict held in a ContextVar (rather than rebinding the ContextVar)
survives ``contextvars.copy_context()`` and any future move of sync tools onto
a thread pool — see specs/034-analytics-entity-context/research.md R1.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_logger = logging.getLogger("okareo.analytics")

_call_props: ContextVar[dict | None] = ContextVar(
    "okareo_analytics_call_props", default=None
)

_MAX_STR = 64

ALLOWED_KEYS = frozenset({
    "project_id",
    "entity_type",
    "entity_id",
    "lookup_by",
    "include_transcripts",
    "result_count",
    "row_count",
    "input_source",
    "run_status",
    "repeats",
    "max_turns",
    "is_rerun",
    "is_voice",
    "language",
    "data_point_id",
})

_SCALAR_TYPES = (str, int, float, bool)


def annotate(**props) -> None:
    """Attach context to the analytics event for the current tool call.

    Silent no-op outside a ``call_scope()``. Never raises. Unknown keys and
    non-scalar values are dropped; strings longer than 64 chars are truncated.
    """
    try:
        bucket = _call_props.get()
        if bucket is None:
            return
        for key, value in props.items():
            if key not in ALLOWED_KEYS:
                _logger.debug("Dropping unknown analytics key=%s", key)
                continue
            if value is None:
                continue
            if not isinstance(value, _SCALAR_TYPES):
                _logger.debug(
                    "Dropping non-scalar analytics key=%s type=%s",
                    key,
                    type(value).__name__,
                )
                continue
            if isinstance(value, str) and len(value) > _MAX_STR:
                value = value[:_MAX_STR]
            bucket[key] = value
    except Exception:
        # Analytics must never fail a tool call.
        pass


@contextmanager
def call_scope() -> Iterator[dict]:
    """Open a fresh annotation dict for one tool call. Always resets on exit."""
    bucket: dict = {}
    token = _call_props.set(bucket)
    try:
        yield bucket
    finally:
        _call_props.reset(token)


def _reset_for_tests() -> None:
    """Clear any leftover scope binding. Production code must not call this."""
    _call_props.set(None)
