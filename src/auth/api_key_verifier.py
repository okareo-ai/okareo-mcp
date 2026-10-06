"""Validates Okareo API keys against okareo-server for the bearer path.

okareo-server is the only authority on whether a key is valid: it checks the
signature, expiry and revocation on every request. This module asks it with
one ``GET /v0/projects`` and maps the answer to three outcomes, so an outage
is never reported as a bad key (specs/048-api-key-shared-connections
research R2):

- 2xx → ``valid``
- 401 / 403 → ``invalid``
- anything else, a timeout, or a transport error → ``unavailable``

Two shortcuts avoid the call entirely:

- A bearer that is not JWT-shaped is ``invalid`` without asking. okareo-server
  only accepts JWTs (its own and Frontegg's), so an opaque string can never be
  valid, and the call would only let any caller make us call okareo-server.
- ``valid`` answers are cached for 30 s, keyed by the key's SHA-256, so a
  revoked key stops working within 30 s (FR-007). Failures are never cached.

The organization comes from the key's own ``tenantId`` claim (else
``organization_id``), read without checking the signature. That is safe only
because okareo-server has just accepted this exact token: both of its JWT
paths verify the signature, so a tampered ``tenantId`` would have been a 401
(research R3). It is the same identifier the sign-in path uses.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Literal

import httpx
import jwt as pyjwt

_logger = logging.getLogger(__name__)

Outcome = Literal["valid", "invalid", "unavailable"]

_CACHE_TTL_SECONDS = 30.0
_CACHE_MAX_ENTRIES = 1024
# Both under the 10 s an MCP client typically waits for a response.
_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


def looks_like_jwt(token: str) -> bool:
    """Cheap shape check: three base64url segments separated by dots."""
    parts = token.split(".")
    if len(parts) != 3:
        return False
    return all(p and all(c.isalnum() or c in "-_" for c in p) for p in parts)


@dataclass(frozen=True)
class KeyValidation:
    outcome: Outcome
    tenant_id: str | None = None
    subject: str | None = None
    expires_at: int | None = None


_INVALID = KeyValidation(outcome="invalid")
_UNAVAILABLE = KeyValidation(outcome="unavailable")


class OkareoAPIKeyVerifier:
    """Asks okareo-server whether an API key is valid, with a short cache."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        cache_ttl: float = _CACHE_TTL_SECONDS,
        cache_max: int = _CACHE_MAX_ENTRIES,
    ) -> None:
        self._projects_url = base_url.rstrip("/") + "/v0/projects"
        self._transport = transport
        self._clock = clock
        self._cache_ttl = cache_ttl
        self._cache_max = cache_max
        self._cache: OrderedDict[str, tuple[float, KeyValidation]] = OrderedDict()

    async def validate(self, key: str, *, use_cache: bool = True) -> KeyValidation:
        if not looks_like_jwt(key):
            _logger.info("API-key validation: bearer is not JWT-shaped; refused without a call")
            return _INVALID

        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        if use_cache:
            cached = self._cache_get(digest)
            if cached is not None:
                return cached

        outcome = await self._ask_okareo(key)
        if outcome != "valid":
            return _INVALID if outcome == "invalid" else _UNAVAILABLE

        result = _identity_from_claims(key)
        if result.outcome == "valid":
            self._cache_put(digest, result)
        return result

    async def _ask_okareo(self, key: str) -> Outcome:
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=_TIMEOUT
            ) as client:
                response = await client.get(
                    self._projects_url, headers={"api-key": key}
                )
        except httpx.HTTPError as exc:
            _logger.warning(
                "API-key validation: okareo-server unreachable (%s)",
                type(exc).__name__,
            )
            return "unavailable"

        status = response.status_code
        if status in (401, 403):
            _logger.info("API-key validation: okareo-server refused the key (%d)", status)
            return "invalid"
        if not 200 <= status < 300:
            _logger.warning("API-key validation: okareo-server returned %d", status)
            return "unavailable"
        try:
            response.json()
        except ValueError:
            # A 2xx that isn't the API's JSON is a proxy or error page, not
            # okareo-server vouching for the key.
            _logger.warning("API-key validation: okareo-server 2xx was not JSON")
            return "unavailable"
        return "valid"

    def _cache_get(self, digest: str) -> KeyValidation | None:
        entry = self._cache.get(digest)
        if entry is None:
            return None
        stored_at, result = entry
        if self._clock() - stored_at >= self._cache_ttl:
            del self._cache[digest]
            return None
        return result

    def _cache_put(self, digest: str, result: KeyValidation) -> None:
        self._cache[digest] = (self._clock(), result)
        self._cache.move_to_end(digest)
        while len(self._cache) > self._cache_max:
            self._cache.popitem(last=False)


def _identity_from_claims(key: str) -> KeyValidation:
    try:
        claims = pyjwt.decode(key, options={"verify_signature": False})
    except pyjwt.InvalidTokenError:
        _logger.warning("API-key validation: accepted key has an unreadable payload")
        return _INVALID
    tenant_id = claims.get("tenantId") or claims.get("organization_id")
    if not tenant_id:
        _logger.warning(
            "API-key validation: accepted key carries no tenantId/organization_id; refused"
        )
        return _INVALID
    subject = claims.get("sub")
    exp = claims.get("exp")
    _logger.info("API-key validation: valid for tenant %s", tenant_id)
    return KeyValidation(
        outcome="valid",
        tenant_id=str(tenant_id),
        subject=str(subject) if subject else None,
        expires_at=int(exp) if isinstance(exp, (int, float)) else None,
    )
