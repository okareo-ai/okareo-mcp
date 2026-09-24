"""Unit tests for the shared depth primitive (043).

Five tools share one depth vocabulary and one page window. The window matters
more than it looks: FR-003c requires the conversations and the row-level
metrics block to be sliced by *the same* bounds, and that is only testable if
one function computes them.
"""

import json

from src.response_depth import (
    DEPTHS_LISTING,
    DEPTHS_RUN,
    page_window,
    validate_depth,
)


class TestValidateDepth:
    def test_accepts_every_depth_it_is_given(self):
        for depth in DEPTHS_RUN:
            assert validate_depth(depth, DEPTHS_RUN) is None

    def test_rejects_unknown_depth_with_structured_error(self):
        err = validate_depth("verbose", DEPTHS_RUN)
        assert err is not None
        payload = json.loads(err)
        assert "verbose" in payload["error"]
        # The message must name what IS valid -- an LLM caller recovers from
        # the error text, not from a status code.
        for depth in DEPTHS_RUN:
            assert depth in payload["error"]

    def test_listings_reject_full(self):
        # FR-010: a tool rejects a depth it does not implement rather than
        # silently serving a lower one.
        err = validate_depth("full", DEPTHS_LISTING)
        assert err is not None
        payload = json.loads(err)
        assert "full" in payload["error"]
        assert "summary" in payload["error"] and "detailed" in payload["error"]

    def test_listing_error_does_not_advertise_full(self):
        payload = json.loads(validate_depth("nope", DEPTHS_LISTING))
        # "full" must not appear as a suggestion for a tool that has no full.
        suggestions = payload["error"].split(":", 1)[1]
        assert "full" not in suggestions

    def test_rejects_non_string(self):
        assert validate_depth(None, DEPTHS_RUN) is not None
        assert validate_depth(3, DEPTHS_RUN) is not None


class TestPageWindow:
    def test_default_window_bounds_a_large_total(self):
        start, stop, has_more = page_window(50, limit=20, offset=0)
        assert (start, stop) == (0, 20)
        assert has_more is True

    def test_last_page_reports_no_more(self):
        start, stop, has_more = page_window(50, limit=20, offset=40)
        assert (start, stop) == (40, 50)
        assert has_more is False

    def test_limit_zero_returns_everything(self):
        # 0 stays an explicit opt-in for "all" -- it is only barred as a
        # *default* (spec Assumptions).
        start, stop, has_more = page_window(50, limit=0, offset=0)
        assert (start, stop) == (0, 50)
        assert has_more is False

    def test_limit_zero_with_offset_returns_remainder(self):
        start, stop, has_more = page_window(50, limit=0, offset=10)
        assert (start, stop) == (10, 50)
        assert has_more is False

    def test_offset_beyond_total_yields_empty_window(self):
        # Edge case: neither list may fall back to the whole run.
        start, stop, has_more = page_window(3, limit=20, offset=99)
        assert start >= stop
        assert has_more is False

    def test_offset_equal_to_total_yields_empty_window(self):
        start, stop, has_more = page_window(3, limit=20, offset=3)
        assert start >= stop
        assert has_more is False

    def test_empty_total_yields_empty_window(self):
        start, stop, has_more = page_window(0, limit=20, offset=0)
        assert (start, stop) == (0, 0)
        assert has_more is False

    def test_negative_offset_is_clamped(self):
        start, stop, _ = page_window(50, limit=20, offset=-5)
        assert start == 0
        assert stop == 20

    def test_negative_limit_is_treated_as_unbounded(self):
        start, stop, has_more = page_window(50, limit=-1, offset=0)
        assert (start, stop) == (0, 50)
        assert has_more is False

    def test_window_is_reusable_across_two_lists(self):
        """FR-003c: the same window slices both lists, so they cannot disagree."""
        conversations = list(range(50))
        scores = list(range(50))
        start, stop, _ = page_window(len(conversations), limit=5, offset=10)
        assert conversations[start:stop] == scores[start:stop]
        assert len(conversations[start:stop]) == 5
        # Element i of the slice denotes conversation offset + i.
        assert conversations[start:stop][0] == 10
