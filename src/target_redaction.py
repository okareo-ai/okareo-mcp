"""Redaction sentinel helpers for Target round-trip (spec 023-tool-fixes US1).

`get_target` walks the Target's `sensitive_fields` list and substitutes
`REDACTION_SENTINEL` so a copilot can see which values must be re-supplied
before calling `create_or_update_target`. The same sentinel is detected on
the write path to prevent leaking the literal marker to the backend.
"""

from __future__ import annotations

import copy
from typing import Any

REDACTION_SENTINEL: str = "***REDACTED***"


def _split_path(path: str) -> list[str | int]:
    """Split a dot-path like 'auth_params.headers.Authorization' into segments.

    Bracketed list indices like 'foo.bar[0].baz' are split into integer steps.
    """
    parts: list[str | int] = []
    for raw in path.split("."):
        if not raw:
            continue
        # Split off any [N] suffixes — there may be more than one.
        while "[" in raw and raw.endswith("]"):
            head, _, index_part = raw.partition("[")
            if head:
                parts.append(head)
                head = ""
            try:
                parts.append(int(index_part[:-1]))
            except ValueError:
                # Not a numeric index — treat the bracketed bit as a key.
                parts.append(index_part[:-1])
            raw = head
        if raw:
            parts.append(raw)
    return parts


def apply_redaction(payload: dict, sensitive_paths: list[str]) -> dict:
    """Return a deep copy of `payload` with sentinel substituted at each path.

    Paths that don't resolve in the payload are silently skipped — the field
    was never set on the Target. List indices may appear as `foo[0].bar`.
    """
    result = copy.deepcopy(payload)
    for path in sensitive_paths or []:
        segments = _split_path(path)
        if not segments:
            continue
        cursor: Any = result
        for seg in segments[:-1]:
            if isinstance(cursor, dict) and isinstance(seg, str) and seg in cursor:
                cursor = cursor[seg]
            elif isinstance(cursor, list) and isinstance(seg, int) and 0 <= seg < len(cursor):
                cursor = cursor[seg]
            else:
                cursor = None
                break
        if cursor is None:
            continue
        last = segments[-1]
        if isinstance(cursor, dict) and isinstance(last, str) and last in cursor:
            cursor[last] = REDACTION_SENTINEL
        elif isinstance(cursor, list) and isinstance(last, int) and 0 <= last < len(cursor):
            cursor[last] = REDACTION_SENTINEL
    return result


def find_sentinel_paths(payload: Any, _prefix: str = "") -> list[str]:
    """Return every dot-path whose value equals REDACTION_SENTINEL.

    Exact string equality (`==`) — substring matches are NOT flagged.
    Walks dicts and lists; lists produce `[index]` path notation.
    Returns empty list when none present (safe to send).
    """
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            sub_prefix = f"{_prefix}.{key}" if _prefix else str(key)
            if value == REDACTION_SENTINEL:
                found.append(sub_prefix)
            elif isinstance(value, (dict, list)):
                found.extend(find_sentinel_paths(value, sub_prefix))
    elif isinstance(payload, list):
        for i, value in enumerate(payload):
            sub_prefix = f"{_prefix}[{i}]"
            if value == REDACTION_SENTINEL:
                found.append(sub_prefix)
            elif isinstance(value, (dict, list)):
                found.extend(find_sentinel_paths(value, sub_prefix))
    return found
