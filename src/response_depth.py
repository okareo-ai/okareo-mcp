"""Shared depth vocabulary and page windows for tiered tool responses (043).

One vocabulary governs every tool in the feature: ``summary`` identifies,
``detailed`` adds the row-level metrics block, ``full`` adds transcripts and
media. Listings implement the first two only.

Two functions rather than a class, and one module rather than a helper copied
into each tool file, because FR-003c requires the conversations and the
row-level metrics block to be sliced by the *same* window. Computing that in
one place is what makes "they never disagree" a testable property instead of
an intention.
"""

import json

SUMMARY = "summary"
DETAILED = "detailed"
FULL = "full"

# Ordered cheapest-first: the error message lists them in this order, and each
# level is a strict superset of the one before it.
DEPTHS_RUN = (SUMMARY, DETAILED, FULL)
DEPTHS_LISTING = (SUMMARY, DETAILED)


def validate_depth(value, allowed: tuple[str, ...]) -> str | None:
    """Return a JSON error payload for an unusable depth, or None if it is fine.

    Called before any network request so an invalid depth costs nothing. The
    message names the depths the *calling tool* implements, never the full set
    — advertising ``full`` on a listing that cannot serve it would send an LLM
    caller straight into a second error.
    """
    if isinstance(value, str) and value in allowed:
        return None
    return json.dumps({
        "error": (
            f"Invalid detail_level {value!r}. "
            f"Valid values: {', '.join(allowed)}."
        ),
    })


def page_window(total: int, limit: int, offset: int) -> tuple[int, int, bool]:
    """Resolve ``(start, stop, has_more)`` for a page over ``total`` items.

    ``limit=0`` means everything, which stays supported as an explicit request
    and is barred only as a *default* — an unbounded default was the defect
    this feature corrects.

    An offset past the end returns an empty window rather than the last page or
    the whole list: a caller that pages off the end must see that it did, and
    both of the lists sliced by this window must empty together.
    """
    total = max(0, int(total))
    start = max(0, int(offset))

    if limit is None or int(limit) <= 0:
        stop = total
    else:
        stop = min(total, start + int(limit))

    if start >= total:
        # Empty, and explicitly not "more available" -- there is nothing
        # further on in this direction.
        return start, start, False

    return start, stop, stop < total
