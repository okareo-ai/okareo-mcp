"""In-process JWKS cache for Frontegg-issued JWT signing keys.

Fetches the JWKS document from ``{issuer}/.well-known/jwks.json`` and caches
it for ``ttl`` seconds. On a cache miss for a specific ``kid`` (typically the
result of a key rotation), forces a refresh before giving up.

Resilience: if a refresh fails (network error, upstream 5xx), the cache
keeps serving the last-known-good document and logs a warning. This avoids
turning a transient Frontegg outage into a full authentication outage.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx


_logger = logging.getLogger(__name__)


class JWKSCache:
    """TTL-cached fetcher for a single issuer's JWKS document.

    Public API:
        - ``get_key(kid)`` — returns the JWK dict for ``kid``, refreshing
          on miss; returns ``None`` if the kid is genuinely unknown.
        - ``refresh()`` — force an HTTP fetch.
    """

    def __init__(self, issuer_url: str, ttl: float = 600.0) -> None:
        self._issuer_url = issuer_url.rstrip("/")
        self._ttl = ttl
        self._keys_by_kid: dict[str, dict[str, Any]] = {}
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_key(self, kid: str) -> dict[str, Any] | None:
        """Return the JWK with the given ``kid`` or None if not found.

        Treats a fresh cache that does not contain ``kid`` as a probable key
        rotation: force a refresh once and try again before giving up. This
        keeps the cache responsive to Frontegg's key-rotation cadence without
        polling on every request.
        """
        if kid in self._keys_by_kid and self._is_fresh():
            return self._keys_by_kid[kid]

        # Refresh once — covers both "cache expired" and "kid unknown,
        # possibly because keys rotated since the last fetch".
        try:
            await self.refresh()
        except Exception:
            # If the refresh failed and we have no prior data, fall through
            # to the lookup which will return None.
            pass
        return self._keys_by_kid.get(kid)

    async def refresh(self) -> None:
        """Force-refresh the JWKS document."""
        async with self._lock:
            await self._fetch()

    def _is_fresh(self) -> bool:
        return (time.monotonic() - self._fetched_at) < self._ttl

    async def _refresh_locked(self) -> None:
        # Re-check freshness inside the lock — another coroutine may have
        # already refreshed while we were waiting to acquire it.
        async with self._lock:
            if self._is_fresh():
                return
            await self._fetch()

    async def _fetch(self) -> None:
        url = f"{self._issuer_url}/.well-known/jwks.json"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                doc = response.json()
            self._keys_by_kid = {
                k["kid"]: k for k in doc.get("keys", []) if "kid" in k
            }
            self._fetched_at = time.monotonic()
        except Exception as exc:
            if self._keys_by_kid:
                _logger.warning(
                    "JWKS refresh failed; serving stale keys (kids=%s): %s",
                    list(self._keys_by_kid.keys()),
                    type(exc).__name__,
                )
                # Keep the existing cache; advance fetched_at slightly so we
                # don't hammer the upstream on every request during outage.
                self._fetched_at = time.monotonic()
            else:
                # No prior keys to fall back on — let the caller see None.
                _logger.error("JWKS initial fetch failed: %s", exc)
                raise
