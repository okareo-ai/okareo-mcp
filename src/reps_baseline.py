"""Fetch and cache the REPS baseline from tagged okareo-tools GitHub Releases.

The REPS agent-evaluation baseline (the ``reps/`` tree of the
okareo-ai/okareo-tools repo) is published via tag-driven GitHub Releases.
This module resolves the release to serve (latest, or the operator-pinned
tag), downloads its source tarball, extracts the ``reps/`` tree into an
in-memory snapshot, and refreshes that snapshot on a TTL. Only tagged
release tarballs are ever fetched — there is no code path that reads a
branch.

All state is per-process. The snapshot's file contents are immutable once
built; refresh swaps in a fully-built replacement. Freshness metadata
(``fetched_at``/``stale``) is updated in place under the module lock.
"""

import io
import logging
import os
import tarfile
import threading
import time
from dataclasses import dataclass, field

import httpx

_logger = logging.getLogger(__name__)

DEFAULT_REPO = "okareo-ai/okareo-tools"
GITHUB_API_BASE = "https://api.github.com"

DEFAULT_REFRESH_SECONDS = 900
MIN_REFRESH_SECONDS = 60
# The documented "new release picked up within" bound (FR-005).
MAX_REFRESH_SECONDS = 3600

PER_FILE_CAP_BYTES = 5 * 1024 * 1024
TREE_CAP_BYTES = 50 * 1024 * 1024

RECOGNIZED_PILLARS = [
    "R-reasoning",
    "E-execution",
    "P-performance",
    "S-security",
    "explore",
    "profile",
]


class BaselineFetchError(Exception):
    """A single resolve/download/extract attempt against GitHub failed."""


class BaselineUnavailableError(Exception):
    """No cached snapshot exists and the baseline cannot be fetched."""


@dataclass
class FileRecord:
    path: str
    size: int
    pillar: str | None
    content: bytes | None
    oversize: bool = False


@dataclass
class BaselineSnapshot:
    tag: str
    fetched_at: float
    files: dict[str, FileRecord]
    pinned: bool = False
    stale: bool = False
    stale_reason: str | None = None
    # Last refresh *attempt* (success or failure). TTL checks key off this,
    # not fetched_at, so a failing GitHub doesn't get hammered once per
    # request — at most one retry per TTL window. fetched_at only advances
    # on successful confirmation and is what responses report.
    checked_at: float = field(default=0.0)


_lock = threading.Lock()
_snapshot: BaselineSnapshot | None = None


def _now() -> float:
    """Seam for tests to control the clock."""
    return time.time()


def _refresh_seconds() -> int:
    raw = os.environ.get("OKAREO_REPS_REFRESH_SECONDS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_REFRESH_SECONDS
    except ValueError:
        return DEFAULT_REFRESH_SECONDS
    return max(MIN_REFRESH_SECONDS, min(MAX_REFRESH_SECONDS, value))


def _github_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _resolve_release(pinned_tag: str | None) -> tuple[str, str]:
    """Return (tag, tarball_url) for the release to serve."""
    repo = os.environ.get("OKAREO_REPS_REPO", "").strip() or DEFAULT_REPO
    if pinned_tag:
        url = f"{GITHUB_API_BASE}/repos/{repo}/releases/tags/{pinned_tag}"
    else:
        url = f"{GITHUB_API_BASE}/repos/{repo}/releases/latest"
    try:
        response = httpx.get(
            url,
            headers=_github_headers(),
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=True,
        )
        response.raise_for_status()
        data = response.json()
        return data["tag_name"], data["tarball_url"]
    except Exception as e:
        raise BaselineFetchError(f"Could not resolve release from {url}: {e}") from e


def _pillar_for(tree_rel_parts: list[str]) -> str | None:
    if len(tree_rel_parts) >= 2 and tree_rel_parts[0] in RECOGNIZED_PILLARS:
        return tree_rel_parts[0]
    return None


def _download_and_extract(tarball_url: str) -> dict[str, FileRecord]:
    """Download the source tarball and extract the ``reps/`` tree.

    GitHub source tarballs wrap the repo in a variable top-level directory
    (``<owner>-<repo>-<sha>/``), which is stripped here. Only regular files
    under ``reps/`` are kept; ``__pycache__`` components, dotfiles, and
    non-regular members (symlinks, dirs) are excluded. Paths are stored
    relative to ``reps/`` with forward slashes.
    """
    try:
        response = httpx.get(
            tarball_url,
            headers=_github_headers(),
            timeout=httpx.Timeout(60.0, connect=5.0),
            follow_redirects=True,
        )
        response.raise_for_status()
        raw = response.content
    except Exception as e:
        raise BaselineFetchError(f"Could not download tarball {tarball_url}: {e}") from e

    files: dict[str, FileRecord] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            for member in tar:
                if not member.isreg():
                    continue
                parts = member.name.split("/")
                # parts[0] is the variable top-level dir; the repo tree starts
                # at parts[1]. Keep only reps/<something...>.
                rel = parts[1:]
                if len(rel) < 2 or rel[0] != "reps":
                    continue
                tree_rel_parts = rel[1:]
                if any(p == "__pycache__" for p in tree_rel_parts):
                    continue
                if tree_rel_parts[-1].startswith("."):
                    continue
                tree_rel = "/".join(tree_rel_parts)
                pillar = _pillar_for(tree_rel_parts)
                if member.size > PER_FILE_CAP_BYTES:
                    files[tree_rel] = FileRecord(
                        path=tree_rel,
                        size=member.size,
                        pillar=pillar,
                        content=None,
                        oversize=True,
                    )
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:  # pragma: no cover — defensive
                    continue
                content = extracted.read()
                total += len(content)
                if total > TREE_CAP_BYTES:
                    raise BaselineFetchError(
                        f"Baseline tree exceeds {TREE_CAP_BYTES} bytes; refusing to cache."
                    )
                files[tree_rel] = FileRecord(
                    path=tree_rel,
                    size=len(content),
                    pillar=pillar,
                    content=content,
                )
    except BaselineFetchError:
        raise
    except Exception as e:
        raise BaselineFetchError(f"Could not extract tarball: {e}") from e

    if not files:
        raise BaselineFetchError("Release tarball contains no reps/ tree.")
    return files


def _refresh_locked() -> None:
    """Attempt a refresh. Caller holds ``_lock``.

    On failure with an existing snapshot: keep serving it, marked stale.
    On failure with no snapshot: raise BaselineUnavailableError.
    """
    global _snapshot

    pinned_tag = os.environ.get("OKAREO_REPS_PINNED_TAG", "").strip() or None
    now = _now()

    def _fail(exc: BaselineFetchError) -> None:
        if _snapshot is not None:
            _snapshot.stale = True
            _snapshot.stale_reason = (
                "pinned_tag_unavailable" if pinned_tag else "github_unreachable"
            )
            _snapshot.checked_at = now
            _logger.warning(
                "REPS baseline refresh failed (%s); serving cached %s as stale: %s",
                _snapshot.stale_reason,
                _snapshot.tag,
                exc,
            )
            return
        if pinned_tag:
            raise BaselineUnavailableError(
                f"REPS baseline is unavailable: pinned tag '{pinned_tag}' "
                f"(OKAREO_REPS_PINNED_TAG) could not be retrieved and no "
                f"cached copy exists. {exc}"
            ) from exc
        raise BaselineUnavailableError(
            "REPS baseline is temporarily unavailable: the okareo-tools "
            f"GitHub release could not be reached and no cached copy exists. {exc}"
        ) from exc

    try:
        tag, tarball_url = _resolve_release(pinned_tag)
    except BaselineFetchError as e:
        _fail(e)
        return

    if _snapshot is not None and _snapshot.tag == tag:
        # Same release — confirm freshness without re-downloading.
        _snapshot.fetched_at = now
        _snapshot.checked_at = now
        _snapshot.stale = False
        _snapshot.stale_reason = None
        _snapshot.pinned = bool(pinned_tag)
        return

    try:
        files = _download_and_extract(tarball_url)
    except BaselineFetchError as e:
        _fail(e)
        return

    _snapshot = BaselineSnapshot(
        tag=tag,
        fetched_at=now,
        checked_at=now,
        files=files,
        pinned=bool(pinned_tag),
    )
    _logger.info(
        "REPS baseline snapshot loaded: %s (%d files%s)",
        tag,
        len(files),
        ", pinned" if pinned_tag else "",
    )


def get_snapshot() -> BaselineSnapshot:
    """Return the current snapshot, refreshing on first use / TTL expiry.

    - First call (no snapshot): blocks on the fetch; raises
      ``BaselineUnavailableError`` if it fails.
    - TTL expired with a snapshot present: at most one caller performs the
      refresh; concurrent callers are served the existing snapshot without
      blocking (single-flight).
    """
    snap = _snapshot
    if snap is not None:
        if _now() - snap.checked_at < _refresh_seconds():
            return snap
        if _lock.acquire(blocking=False):
            try:
                # Re-check under the lock — another thread may have refreshed.
                current = _snapshot
                if current is None or _now() - current.checked_at >= _refresh_seconds():
                    _refresh_locked()
            finally:
                _lock.release()
        result = _snapshot
        assert result is not None  # never reset to None once populated
        return result

    with _lock:
        if _snapshot is None:
            _refresh_locked()
        result = _snapshot
        assert result is not None  # _refresh_locked raises rather than yield None
        return result


def _reset_for_tests() -> None:
    """Clear module state. Test hook only."""
    global _snapshot
    with _lock:
        _snapshot = None
