"""The project list cache never keeps a list fetched before a clear (049).

With tools running side by side, one call can fetch the project list while
another creates, renames, clones, archives or unarchives a project and clears
the cache. The fetch must not then store the old list, or every project-scoped
tool in that organization answers from it for up to 60 seconds.
"""

import threading
from unittest.mock import MagicMock

import pytest

from src import okareo_client
from src.okareo_client import (
    _reset_for_tests,
    get_projects_cached,
    invalidate_projects_cache,
)

_WAIT = 5.0


@pytest.fixture(autouse=True)
def _fresh_cache():
    _reset_for_tests()
    yield
    _reset_for_tests()


def _client(fetch):
    okareo = MagicMock()
    okareo.api_key = "key-for-org-a"
    okareo.get_projects.side_effect = fetch
    return okareo


def _fetch_that_waits(started, release, result):
    def fetch():
        started.set()
        release.wait(_WAIT)
        return result
    return fetch


def test_a_fetch_that_started_before_an_entry_clear_doesnt_store_the_old_list():
    """create, update, clone, archive and unarchive all clear the entry through
    invalidate_projects_cache, which is what this drives."""
    started, release = threading.Event(), threading.Event()
    old, new = [MagicMock(name="old")], [MagicMock(name="new")]
    okareo = _client(_fetch_that_waits(started, release, old))

    fetched = {}
    t = threading.Thread(target=lambda: fetched.setdefault("v", get_projects_cached(okareo)))
    t.start()
    assert started.wait(_WAIT)
    # The change happens while the fetch is in flight; its tool clears the entry.
    invalidate_projects_cache(okareo)
    release.set()
    t.join(_WAIT)

    assert fetched["v"] == old  # the in-flight caller still gets its answer
    okareo.get_projects.side_effect = None
    okareo.get_projects.return_value = new
    assert get_projects_cached(okareo) == new, "stale list kept after the clear"


def test_a_fetch_that_started_before_a_whole_cache_clear_doesnt_store_the_old_list(monkeypatch):
    started, release = threading.Event(), threading.Event()
    old, new = [MagicMock(name="old")], [MagicMock(name="new")]
    okareo = _client(_fetch_that_waits(started, release, old))

    t = threading.Thread(target=lambda: get_projects_cached(okareo))
    t.start()
    assert started.wait(_WAIT)
    # invalidate_projects_cache clears everything when it can't work out the
    # caller's key.
    monkeypatch.setattr(
        okareo_client, "_project_cache_scope", lambda o: (_ for _ in ()).throw(RuntimeError())
    )
    invalidate_projects_cache(okareo)
    monkeypatch.undo()
    release.set()
    t.join(_WAIT)

    okareo.get_projects.side_effect = None
    okareo.get_projects.return_value = new
    assert get_projects_cached(okareo) == new


def test_an_untouched_fetch_is_cached_as_before():
    first = [MagicMock(name="p")]
    okareo = _client(lambda: first)
    assert get_projects_cached(okareo) == first
    okareo.get_projects.side_effect = lambda: pytest.fail("should come from the cache")
    assert get_projects_cached(okareo) == first
