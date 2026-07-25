"""Unit tests for the REPS baseline engine (src/reps_baseline.py).

All GitHub traffic is mocked at the ``httpx.get`` boundary — no live
network calls.
"""

import threading

import httpx
import pytest

from src import reps_baseline
from tests.unit.reps_fixtures import (
    DEFAULT_FILES,
    build_release_tarball,
    build_release_tarball_with_symlink,
)

RELEASE_URL_LATEST = (
    f"{reps_baseline.GITHUB_API_BASE}/repos/{reps_baseline.DEFAULT_REPO}/releases/latest"
)
TARBALL_URL = "https://api.github.com/repos/okareo-ai/okareo-tools/tarball/vX"


class FakeResponse:
    def __init__(self, *, json_data=None, content=b"", status_code=200):
        self._json = json_data
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None
            )

    def json(self):
        return self._json


class FakeGitHub:
    """Routes httpx.get calls to canned release JSON / tarball bytes."""

    def __init__(self, tag="v0.5.1", tarball=None):
        self.tag = tag
        self.tarball = tarball if tarball is not None else build_release_tarball()
        self.calls = []  # (url, headers)
        self.fail_all = False
        self.fail_tarball = False
        self.release_status = 200

    def __call__(self, url, headers=None, timeout=None, follow_redirects=None):
        self.calls.append((url, headers or {}))
        if self.fail_all:
            raise httpx.ConnectError("boom")
        if "/releases/" in url:
            if self.release_status >= 400:
                return FakeResponse(status_code=self.release_status)
            return FakeResponse(
                json_data={"tag_name": self.tag, "tarball_url": TARBALL_URL}
            )
        if self.fail_tarball:
            raise httpx.ConnectError("tarball boom")
        return FakeResponse(content=self.tarball)

    @property
    def release_calls(self):
        return [u for u, _ in self.calls if "/releases/" in u]

    @property
    def tarball_calls(self):
        return [u for u, _ in self.calls if "/releases/" not in u]


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    reps_baseline._reset_for_tests()
    for var in (
        "OKAREO_REPS_REFRESH_SECONDS",
        "OKAREO_REPS_PINNED_TAG",
        "OKAREO_REPS_REPO",
        "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    reps_baseline._reset_for_tests()


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(reps_baseline.httpx, "get", fake)
    return fake


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 1_000_000.0}
    monkeypatch.setattr(reps_baseline, "_now", lambda: state["now"])
    return state


# ---------------------------------------------------------------------------
# Resolution (T002)
# ---------------------------------------------------------------------------

class TestResolution:
    def test_fetches_latest_release_and_populates_snapshot(self, github):
        snap = reps_baseline.get_snapshot()
        assert snap.tag == "v0.5.1"
        assert github.release_calls == [RELEASE_URL_LATEST]
        assert github.tarball_calls == [TARBALL_URL]
        assert snap.stale is False
        assert snap.pinned is False

    def test_no_auth_header_without_token(self, github):
        reps_baseline.get_snapshot()
        for _, headers in github.calls:
            assert "Authorization" not in headers

    def test_auth_header_with_token(self, github, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "gh-tok")
        reps_baseline.get_snapshot()
        for _, headers in github.calls:
            assert headers["Authorization"] == "Bearer gh-tok"

    def test_repo_override(self, github, monkeypatch):
        monkeypatch.setenv("OKAREO_REPS_REPO", "acme/other-tools")
        reps_baseline.get_snapshot()
        assert "acme/other-tools" in github.release_calls[0]

    def test_release_http_error_cold_raises_unavailable(self, github):
        github.release_status = 404
        with pytest.raises(reps_baseline.BaselineUnavailableError):
            reps_baseline.get_snapshot()


# ---------------------------------------------------------------------------
# Extraction and filtering (T003)
# ---------------------------------------------------------------------------

class TestExtraction:
    def test_paths_relative_to_reps_with_top_dir_stripped(self, github):
        snap = reps_baseline.get_snapshot()
        assert "S-security/scenarios/verification-gate.jsonl" in snap.files
        assert "README.md" in snap.files
        assert not any(p.startswith("reps/") for p in snap.files)
        assert not any("okareo-tools" in p for p in snap.files)

    def test_content_byte_fidelity(self, github):
        snap = reps_baseline.get_snapshot()
        record = snap.files["S-security/scenarios/verification-gate.jsonl"]
        expected = DEFAULT_FILES["reps/S-security/scenarios/verification-gate.jsonl"]
        assert record.content == expected
        assert record.size == len(expected)

    def test_pillar_assignment(self, github):
        snap = reps_baseline.get_snapshot()
        assert snap.files["S-security/coverage.json"].pillar == "S-security"
        assert snap.files["explore/probe.md"].pillar == "explore"
        assert snap.files["shared/rubric.md"].pillar is None
        assert snap.files["README.md"].pillar is None

    def test_excludes_pycache_dotfiles_and_non_reps(self, github):
        github.tarball = build_release_tarball(
            extra_members=[
                ("reps/__pycache__/common.cpython-310.pyc", b"junk"),
                ("reps/S-security/templates/.gitkeep", b""),
                ("src/server.py", b"not baseline"),
                ("README.md", b"repo root readme, not reps/"),
            ]
        )
        snap = reps_baseline.get_snapshot()
        assert not any("__pycache__" in p for p in snap.files)
        assert not any(p.endswith(".gitkeep") for p in snap.files)
        assert "server.py" not in {p.split("/")[-1] for p in snap.files}
        # reps/README.md is kept; the repo-root README.md is not (same
        # tree-relative name, but content proves which one survived).
        assert snap.files["README.md"].content == DEFAULT_FILES["reps/README.md"]

    def test_excludes_symlinks(self, github):
        github.tarball = build_release_tarball_with_symlink(
            dict(DEFAULT_FILES),
            link_path="reps/S-security/evil-link.md",
            link_target="/etc/passwd",
        )
        snap = reps_baseline.get_snapshot()
        assert "S-security/evil-link.md" not in snap.files

    def test_oversize_file_flagged_without_content(self, github, monkeypatch):
        monkeypatch.setattr(reps_baseline, "PER_FILE_CAP_BYTES", 10)
        snap = reps_baseline.get_snapshot()
        big = snap.files["S-security/scenarios/verification-gate.jsonl"]
        assert big.oversize is True
        assert big.content is None
        small = snap.files["S-security/scenarios/verification-gate_meta.md"]
        assert small.oversize is False

    def test_tree_cap_aborts_cold_start(self, github, monkeypatch):
        monkeypatch.setattr(reps_baseline, "TREE_CAP_BYTES", 20)
        with pytest.raises(reps_baseline.BaselineUnavailableError):
            reps_baseline.get_snapshot()

    def test_empty_reps_tree_is_a_fetch_error(self, github):
        github.tarball = build_release_tarball(files={"src/other.py": b"x"})
        with pytest.raises(reps_baseline.BaselineUnavailableError):
            reps_baseline.get_snapshot()


# ---------------------------------------------------------------------------
# Snapshot cache semantics (T004)
# ---------------------------------------------------------------------------

class TestSnapshotCache:
    def test_second_call_served_from_cache(self, github):
        reps_baseline.get_snapshot()
        calls_after_first = len(github.calls)
        reps_baseline.get_snapshot()
        assert len(github.calls) == calls_after_first

    def test_concurrent_cold_start_fetches_once(self, github):
        results = []

        def worker():
            results.append(reps_baseline.get_snapshot())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(github.release_calls) == 1
        assert len(github.tarball_calls) == 1
        assert all(s is results[0] for s in results)

    def test_cold_start_failure_raises_unavailable(self, github):
        github.fail_all = True
        with pytest.raises(reps_baseline.BaselineUnavailableError) as exc_info:
            reps_baseline.get_snapshot()
        assert "no cached copy" in str(exc_info.value)


# ---------------------------------------------------------------------------
# TTL refresh (T015) and stale fallback (T016) — US3
# ---------------------------------------------------------------------------

class TestRefresh:
    def test_no_recheck_inside_ttl(self, github, clock):
        reps_baseline.get_snapshot()
        clock["now"] += 899
        reps_baseline.get_snapshot()
        assert len(github.release_calls) == 1

    def test_same_tag_restamps_without_download(self, github, clock):
        snap = reps_baseline.get_snapshot()
        first_fetched_at = snap.fetched_at
        clock["now"] += 901
        snap2 = reps_baseline.get_snapshot()
        assert snap2 is snap
        assert len(github.release_calls) == 2
        assert len(github.tarball_calls) == 1  # no re-download
        assert snap2.fetched_at > first_fetched_at

    def test_new_tag_swaps_snapshot(self, github, clock):
        old = reps_baseline.get_snapshot()
        github.tag = "v0.6.0"
        github.tarball = build_release_tarball(
            files={"reps/S-security/coverage.json": b'{"v": 6}\n'}
        )
        clock["now"] += 901
        new = reps_baseline.get_snapshot()
        assert new is not old
        assert new.tag == "v0.6.0"
        assert new.files["S-security/coverage.json"].content == b'{"v": 6}\n'
        assert new.stale is False

    def test_github_down_serves_cached_stale(self, github, clock):
        reps_baseline.get_snapshot()
        github.fail_all = True
        clock["now"] += 901
        snap = reps_baseline.get_snapshot()
        assert snap.tag == "v0.5.1"
        assert snap.stale is True
        assert snap.stale_reason == "github_unreachable"

    def test_failed_refresh_not_retried_inside_ttl(self, github, clock):
        reps_baseline.get_snapshot()
        github.fail_all = True
        clock["now"] += 901
        reps_baseline.get_snapshot()
        failed_calls = len(github.calls)
        reps_baseline.get_snapshot()  # still inside the post-failure TTL window
        assert len(github.calls) == failed_calls

    def test_recovery_clears_stale(self, github, clock):
        reps_baseline.get_snapshot()
        github.fail_all = True
        clock["now"] += 901
        assert reps_baseline.get_snapshot().stale is True
        github.fail_all = False
        clock["now"] += 901
        snap = reps_baseline.get_snapshot()
        assert snap.stale is False
        assert snap.stale_reason is None

    def test_refresh_does_not_block_concurrent_readers(self, github, clock):
        snap = reps_baseline.get_snapshot()
        clock["now"] += 901
        # Simulate another thread mid-refresh by holding the module lock.
        assert reps_baseline._lock.acquire(blocking=False)
        try:
            result = reps_baseline.get_snapshot()
            assert result is snap  # served without blocking, no refresh
        finally:
            reps_baseline._lock.release()

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", 900),
            ("59", 60),
            ("3601", 3600),
            ("300", 300),
            ("garbage", 900),
        ],
    )
    def test_refresh_seconds_clamping(self, monkeypatch, raw, expected):
        if raw:
            monkeypatch.setenv("OKAREO_REPS_REFRESH_SECONDS", raw)
        assert reps_baseline._refresh_seconds() == expected


# ---------------------------------------------------------------------------
# Operator pin (T019) — US4
# ---------------------------------------------------------------------------

class TestPin:
    def test_pin_resolves_tags_endpoint_and_sets_pinned(self, github, monkeypatch):
        monkeypatch.setenv("OKAREO_REPS_PINNED_TAG", "v0.4.0")
        github.tag = "v0.4.0"
        snap = reps_baseline.get_snapshot()
        assert snap.pinned is True
        assert snap.tag == "v0.4.0"
        assert github.release_calls == [
            f"{reps_baseline.GITHUB_API_BASE}/repos/"
            f"{reps_baseline.DEFAULT_REPO}/releases/tags/v0.4.0"
        ]

    def test_unset_pin_resumes_latest_on_next_refresh(
        self, github, monkeypatch, clock
    ):
        monkeypatch.setenv("OKAREO_REPS_PINNED_TAG", "v0.4.0")
        github.tag = "v0.4.0"
        assert reps_baseline.get_snapshot().pinned is True
        monkeypatch.delenv("OKAREO_REPS_PINNED_TAG")
        github.tag = "v0.5.1"
        clock["now"] += 901
        snap = reps_baseline.get_snapshot()
        assert snap.pinned is False
        assert snap.tag == "v0.5.1"
        assert github.release_calls[-1] == RELEASE_URL_LATEST

    def test_bad_pin_with_cache_serves_stale(self, github, monkeypatch, clock):
        reps_baseline.get_snapshot()  # cache latest
        monkeypatch.setenv("OKAREO_REPS_PINNED_TAG", "v9.9.9")
        github.release_status = 404
        clock["now"] += 901
        snap = reps_baseline.get_snapshot()
        assert snap.tag == "v0.5.1"
        assert snap.stale is True
        assert snap.stale_reason == "pinned_tag_unavailable"

    def test_bad_pin_cold_start_names_pin(self, github, monkeypatch):
        monkeypatch.setenv("OKAREO_REPS_PINNED_TAG", "v9.9.9")
        github.release_status = 404
        with pytest.raises(reps_baseline.BaselineUnavailableError) as exc_info:
            reps_baseline.get_snapshot()
        assert "v9.9.9" in str(exc_info.value)
