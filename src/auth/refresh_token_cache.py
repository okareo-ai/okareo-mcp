"""Per-user refresh-token cache (Frontegg refresh tokens, indexed by JWT sub).

In-process for hot reads, persisted to disk lazily on every write so the
cache survives container restarts during local development.

**Why disk persistence:** every container rebuild was wiping the cache,
forcing the user to re-run the OAuth flow before each bug-fix iteration.
The cache now survives rebuilds when ``MCP_REFRESH_TOKEN_CACHE_PATH`` is
backed by a Docker volume mount (see docker-compose.yml).

**Production (Cloud Run):** instances have ephemeral filesystems. The
cache file path resolves to a writable directory but cache contents are
lost on instance recycle. The MCP server captures fresh refresh tokens on
every `/oauth/callback`, so users only re-OAuth as their existing access
tokens expire (~30 min default). This is acceptable for v1 — distributed
caches (Redis) are explicitly out of scope per spec.md.

Security:
- The file is written with mode 0o600 (owner read/write only).
- No encryption at rest in v1. Acceptable for local dev (host is trusted)
  and for Cloud Run (ephemeral disk, no cross-instance access). If the
  threat model later requires it, encrypt with HKDF-derived key from
  MCP_DCR_SIGNING_KEY.
- File operations are best-effort: any failure (permission denied, disk
  full, JSON corruption) logs a warning and falls back to in-memory only.
  The cache continues to function for the lifetime of the process.

Concurrency: dict assignment is atomic under the GIL. Disk writes use
write-temp + rename for atomicity. Two concurrent writes for the same
user resolve to whichever wrote last; both values are valid.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


_DEFAULT_CACHE_PATH = "/data/okareo-mcp/refresh_tokens.json"

# In-process state.
_cache: dict[str, str] = {}
_loaded_from_disk: bool = False


def _cache_path() -> Path | None:
    """Resolve the on-disk cache path from the environment.

    Returns ``None`` if persistence has been explicitly disabled
    (``MCP_REFRESH_TOKEN_CACHE_PATH`` set to empty), in which case the
    cache is purely in-memory.
    """
    raw = os.environ.get("MCP_REFRESH_TOKEN_CACHE_PATH")
    if raw is None:
        # Env var unset → use default path. (Setting it to empty string
        # disables persistence entirely; that's a deliberate test/dev knob.)
        return Path(_DEFAULT_CACHE_PATH)
    raw = raw.strip()
    if not raw:
        return None
    return Path(raw)


def _ensure_loaded() -> None:
    """Load cache from disk on first access. Safe to call repeatedly."""
    global _loaded_from_disk
    if _loaded_from_disk:
        return
    # Mark loaded first so failures don't retry forever.
    _loaded_from_disk = True

    path = _cache_path()
    if path is None or not path.exists():
        return

    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as exc:
        print(
            f"[refresh-token-cache] Failed to read cache file "
            f"({type(exc).__name__}); starting empty.",
            file=sys.stderr, flush=True,
        )
        return

    if not isinstance(data, dict):
        return

    loaded = 0
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str) and key and value:
            _cache[key] = value
            loaded += 1
    if loaded:
        print(
            f"[refresh-token-cache] Loaded {loaded} entries from {path}",
            file=sys.stderr, flush=True,
        )


def _save_to_disk() -> None:
    """Persist the cache to disk. Best-effort; logs but never raises."""
    path = _cache_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(_cache, f)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception as exc:
        print(
            f"[refresh-token-cache] Persist failed "
            f"({type(exc).__name__}); cache stays in-memory.",
            file=sys.stderr, flush=True,
        )


def set_token(user_sub: str, refresh_token: str) -> None:
    """Store the refresh token for ``user_sub``. Replaces any prior value.

    Persists to disk on every write. Empty inputs are silently ignored.
    """
    if not user_sub or not refresh_token:
        return
    _ensure_loaded()
    _cache[user_sub] = refresh_token
    _save_to_disk()


def get_token(user_sub: str) -> str | None:
    """Return the cached refresh token for ``user_sub``, or ``None``."""
    if not user_sub:
        return None
    _ensure_loaded()
    return _cache.get(user_sub)


def forget_user(user_sub: str) -> None:
    """Drop the cached refresh token for ``user_sub``. Safe if absent.

    Persists the resulting state to disk.
    """
    _ensure_loaded()
    if _cache.pop(user_sub, None) is not None:
        _save_to_disk()


# ---------------------------------------------------------------------------
# Test helpers (production code MUST NOT call these)
# ---------------------------------------------------------------------------


def _reset_for_tests() -> None:
    global _loaded_from_disk
    _cache.clear()
    _loaded_from_disk = False


def _size_for_tests() -> int:
    return len(_cache)
