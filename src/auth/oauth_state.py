"""State for the OAuth Proxy.

Two record types are managed here:

- ``RegisteredMcpClient`` — created by ``/register`` (RFC 7591 DCR). Since
  the 2026-05-18 stateless-DCR clarification, these are **NOT stored** —
  the returned ``client_id`` is a self-describing, HMAC-signed string of
  the form ``mcp_<b64-payload>.<b64-hmac>``. Every ``/oauth/authorize``
  call HMAC-verifies and decodes it. Survives any restart / instance
  topology with zero new deps. Stdlib only (``hmac`` + ``base64`` + ``json``).

- ``PendingAuthorization`` — created by ``/oauth/authorize``, populated by
  ``/oauth/callback`` once the Frontegg side returns, consumed (deleted) by
  ``/oauth/token``. One-time use, 5-minute TTL. Lives in an in-process
  dict because OAuth flows are too short-lived for restart loss to matter
  (user retries on ``invalid_grant``).

Concurrency: a single ``asyncio.Lock`` serializes mutations on the pending
dict. The store is expected to see modest contention (one mutation per
OAuth flow step, not per-MCP-request), so a single lock is fine.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


CodeChallengeMethod = Literal["S256"]


# ---------------------------------------------------------------------------
# Stateless DCR (2026-05-18 clarification)
# ---------------------------------------------------------------------------

_CLIENT_ID_PREFIX = "mcp_"


class InvalidSignedClientIdError(ValueError):
    """Raised when a signed client_id fails HMAC verification, is malformed,
    or has a payload that doesn't deserialize. Callers MUST translate this
    into a `401 invalid_client` response."""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padded = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(padded)


def encode_signed_client_id(payload: dict, signing_key: str) -> str:
    """Encode a DCR registration payload as a signed client_id.

    Returns ``mcp_<b64url(json(payload))>.<b64url(hmac-sha256)>``.

    Args:
        payload: the registration record (will be JSON-serialized with sorted
            keys + compact separators for a stable signature).
        signing_key: HMAC key (FR-022, ≥256 bits in production).
    """
    payload_bytes = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    payload_b64 = _b64url_encode(payload_bytes)
    mac = hmac.new(
        signing_key.encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    mac_b64 = _b64url_encode(mac)
    return f"{_CLIENT_ID_PREFIX}{payload_b64}.{mac_b64}"


def decode_signed_client_id(client_id: str, signing_key: str) -> dict:
    """Verify HMAC (constant-time) and decode the payload.

    Raises ``InvalidSignedClientIdError`` on any failure:
    - Missing or wrong prefix
    - Missing or malformed payload/mac segments
    - HMAC mismatch
    - Payload not valid JSON / not a dict
    """
    if not isinstance(client_id, str) or not client_id.startswith(_CLIENT_ID_PREFIX):
        raise InvalidSignedClientIdError("client_id missing required prefix")

    body = client_id[len(_CLIENT_ID_PREFIX) :]
    if "." not in body:
        raise InvalidSignedClientIdError("client_id missing signature segment")

    payload_b64, mac_b64 = body.rsplit(".", 1)
    if not payload_b64 or not mac_b64:
        raise InvalidSignedClientIdError("client_id has empty payload or mac")

    expected_mac = hmac.new(
        signing_key.encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    expected_mac_b64 = _b64url_encode(expected_mac)

    # Constant-time comparison to avoid timing-side-channel disclosure
    # (FR-021 security invariant).
    if not hmac.compare_digest(mac_b64, expected_mac_b64):
        raise InvalidSignedClientIdError("client_id signature mismatch")

    try:
        payload_bytes = _b64url_decode(payload_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidSignedClientIdError(f"client_id payload malformed: {exc}") from exc

    if not isinstance(payload, dict):
        raise InvalidSignedClientIdError("client_id payload is not a JSON object")

    return payload


@dataclass(frozen=True)
class RegisteredMcpClient:
    """A DCR-registered MCP client (stateless — see module docstring)."""

    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    token_endpoint_auth_method: str = "none"
    scope: str = "okareo:use"
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass
class PendingAuthorization:
    """A one-time OAuth code flow record. Lookup key: ``code``.

    NOT frozen — ``frontegg_jwt`` and ``frontegg_refresh_token`` are filled
    in during ``/oauth/callback`` after creation by ``/oauth/authorize``.
    """

    code: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: CodeChallengeMethod
    state_to_client: str | None
    created_at: float
    ttl_seconds: float
    # Upstream-side PKCE verifier (2026-05-18 PKCE-upstream clarification).
    # Generated server-side by /oauth/authorize and sent to Frontegg's
    # /oauth/token at /oauth/callback time. Independent of the MCP-client's
    # code_challenge above.
    upstream_code_verifier: str = ""
    frontegg_jwt: str | None = None
    frontegg_refresh_token: str | None = None
    frontegg_expires_in: int | None = None

    def is_expired(self, now: float | None = None) -> bool:
        if now is None:
            now = time.monotonic()
        return (now - self.created_at) > self.ttl_seconds


class OAuthStateStore:
    """In-process state for one MCP server instance.

    Public API:
        - ``register_client(...)``
        - ``get_client(client_id)``
        - ``create_pending(...)``
        - ``populate_pending(code, frontegg_jwt, frontegg_refresh_token, ...)``
        - ``get_pending(code)``  # peek, does NOT consume
        - ``consume_pending(code)``  # returns record and deletes it
    """

    def __init__(
        self,
        default_ttl_seconds: float = 300.0,
        dcr_signing_key: str | None = None,
    ) -> None:
        """
        Args:
            default_ttl_seconds: TTL for PendingAuthorization records.
            dcr_signing_key: HMAC key for signed client_ids (FR-022). If None,
                a per-instance ephemeral key is generated and DCR registrations
                will be invalidated on restart (intended for tests; production
                MUST pass an explicit, durable key).
        """
        self._pending: dict[str, PendingAuthorization] = {}
        self._lock = asyncio.Lock()
        self._default_ttl = default_ttl_seconds
        self._dcr_signing_key = (
            dcr_signing_key
            if dcr_signing_key is not None
            else secrets.token_urlsafe(43)
        )

    # -----------------------------------------------------------------
    # Registered clients (RFC 7591 DCR) — STATELESS via signed client_id
    # -----------------------------------------------------------------

    async def register_client(
        self,
        *,
        client_name: str,
        redirect_uris: tuple[str, ...] | list[str],
        token_endpoint_auth_method: str = "none",
        scope: str = "okareo:use",
    ) -> RegisteredMcpClient:
        """Mint a stateless signed client_id encoding the registration.

        NO server-side storage. The returned record's ``client_id`` IS the
        signed encoded form; subsequent ``get_client(...)`` calls decode it.

        Each call generates a unique ``jti`` (random nonce in the payload)
        so that two registrations with otherwise identical inputs still
        produce distinct client_ids. This matches RFC 7591 convention —
        each `POST /register` returns a fresh identity.
        """
        payload = {
            "client_name": client_name,
            "redirect_uris": list(redirect_uris),
            "token_endpoint_auth_method": token_endpoint_auth_method,
            "scope": scope,
            "iat": int(time.time()),
            "jti": secrets.token_urlsafe(16),
        }
        client_id = encode_signed_client_id(payload, self._dcr_signing_key)
        return RegisteredMcpClient(
            client_id=client_id,
            client_name=client_name,
            redirect_uris=tuple(redirect_uris),
            token_endpoint_auth_method=token_endpoint_auth_method,
            scope=scope,
        )

    async def get_client(self, client_id: str) -> RegisteredMcpClient | None:
        """Verify + decode the signed client_id. Returns None on any failure.

        Failure cases (caller surfaces 401 invalid_client):
        - Missing/wrong prefix
        - Malformed payload or mac segment
        - HMAC mismatch (forged, tampered, or signed with a different key)
        - Payload not valid JSON / missing required fields
        """
        try:
            payload = decode_signed_client_id(client_id, self._dcr_signing_key)
        except InvalidSignedClientIdError as exc:
            # Diagnostic: log enough detail to tell apart "client sent a
            # never-registered random string" vs. "client's old client_id
            # is signed with a different MCP_DCR_SIGNING_KEY" vs.
            # "tampered/forged client_id". The client_id is opaque-but-public
            # so it's safe to log a prefix — but truncate so logs don't
            # blow up with full payloads.
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "Rejecting client_id (caller will return 401 invalid_client; "
                "spec-compliant clients should re-register via POST /register). "
                "client_id_prefix=%s, reason=%s",
                client_id[:32] if isinstance(client_id, str) else repr(client_id)[:32],
                exc,
            )
            return None

        # Required fields — if any are missing, the client_id was minted by
        # an older or incompatible code path. Treat as unknown.
        try:
            client_name = str(payload["client_name"])
            redirect_uris = tuple(str(u) for u in payload["redirect_uris"])
        except (KeyError, TypeError, ValueError):
            return None

        return RegisteredMcpClient(
            client_id=client_id,
            client_name=client_name,
            redirect_uris=redirect_uris,
            token_endpoint_auth_method=str(
                payload.get("token_endpoint_auth_method", "none")
            ),
            scope=str(payload.get("scope", "okareo:use")),
        )

    # -----------------------------------------------------------------
    # Pending authorizations (OAuth code flow state)
    # -----------------------------------------------------------------

    async def create_pending(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: CodeChallengeMethod,
        state_to_client: str | None,
        upstream_code_verifier: str = "",
        ttl_seconds: float | None = None,
    ) -> PendingAuthorization:
        code = "okm_" + secrets.token_urlsafe(32)
        record = PendingAuthorization(
            code=code,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            state_to_client=state_to_client,
            created_at=time.monotonic(),
            ttl_seconds=ttl_seconds if ttl_seconds is not None else self._default_ttl,
            upstream_code_verifier=upstream_code_verifier,
        )
        async with self._lock:
            self._pending[code] = record
            self._gc_expired_locked()
        return record

    async def populate_pending(
        self,
        code: str,
        *,
        frontegg_jwt: str,
        frontegg_refresh_token: str | None,
        frontegg_expires_in: int | None = None,
    ) -> bool:
        """Attach the Frontegg-side tokens to a pending record.

        Returns True on success, False if the code is unknown or expired.
        """
        async with self._lock:
            record = self._pending.get(code)
            if record is None or record.is_expired():
                if record is not None:
                    # expired — GC it
                    del self._pending[code]
                return False
            record.frontegg_jwt = frontegg_jwt
            record.frontegg_refresh_token = frontegg_refresh_token
            record.frontegg_expires_in = frontegg_expires_in
            return True

    async def get_pending(self, code: str) -> PendingAuthorization | None:
        """Peek at a pending record without consuming it. None if unknown/expired."""
        async with self._lock:
            record = self._pending.get(code)
            if record is None:
                return None
            if record.is_expired():
                del self._pending[code]
                return None
            return record

    async def consume_pending(self, code: str) -> PendingAuthorization | None:
        """Return the pending record and DELETE it (one-time use).

        Returns None if unknown or expired. The record is deleted whether
        or not the caller's PKCE check ultimately succeeds — callers MUST
        treat consumption as final.
        """
        async with self._lock:
            record = self._pending.pop(code, None)
            if record is None:
                return None
            if record.is_expired():
                return None
            return record

    def _gc_expired_locked(self) -> None:
        """Drop expired pending records. Caller holds _lock."""
        now = time.monotonic()
        expired = [c for c, r in self._pending.items() if r.is_expired(now)]
        for c in expired:
            del self._pending[c]
