"""Carry an Okareo API key through OAuth clients with short fields.

Slack's Manual OAuth form caps each field near 255 characters, and an Okareo
API key is ~850 (specs/048-api-key-shared-connections research R14). Only
the key's 342-character RS256 signature is secret, and it fits the client
secret. Everything before it — header and payload — is claims okareo-server
encoded deterministically, so it can be rebuilt byte-for-byte from the claim
values alone. Those values travel in the token URL's path, which Slack calls
server-to-server:

    /oauth/shared/token/v1/<sub>/<tenantId>/<jti>/<iat>/<email>/<name>[/<opt>…]

- UUIDs are packed to 22 base64url characters; anything else is ``~`` plus
  the percent-encoded string (okareo-server falls back to ``sub="unknown"``).
- ``email`` and ``name`` are percent-encoded; ``!`` stands for JSON null and
  ``!e`` for an empty string, so no segment is ever empty (proxies may
  collapse ``//``). ``!`` is always percent-encoded in real values.
- Claims that usually take a known value are omitted unless they differ,
  each as an optional ``key=value`` segment:
  ``r=`` roles (default ``["Admin"]``), ``t=`` tenantIds (default
  ``[tenantId]``), ``i=`` iss, ``a=`` aud, ``x=`` exp (none by default).

The layout mirrors okareo-server's ``create_okareo_api_key`` claim order
(``api_key_service.py``) and PyJWT's compact encoding. ``pack`` refuses any
key it cannot rebuild exactly, so a change in okareo-server's format shows up
at setup time, never as a silent failure in a client.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
import urllib.parse

VERSION = "v1"
DEFAULT_ROLES = ["Admin"]
DEFAULT_ISS = "https://app.okareo.com"
DEFAULT_AUD = "okareo-cloud"

# PyJWT sorts header keys, and okareo-server passes only `typ`.
_HEADER = '{"alg":"RS256","typ":"JWT"}'
_CLAIM_ORDER = (
    "sub", "tenantId", "tenantIds", "type", "roles", "email", "name",
    "iss", "aud", "iat", "jti",
)
_NULL = "!"
_EMPTY = "!e"
_RAW = "~"
# Percent-encode everything but unreserved characters and the few email
# characters that are safe in a path segment; "/" and "!" must always encode.
_SAFE = "@+.-_"


class CompactKeyError(ValueError):
    """The key or path can't be packed or rebuilt. The message is safe to
    show: it never contains claim values or key material."""


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _encode_json(value) -> str:
    # Exactly PyJWT's payload encoding.
    return _b64e(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _pack_id(value) -> str:
    if isinstance(value, str):
        try:
            parsed = uuid.UUID(value)
        except ValueError:
            parsed = None
        if parsed is not None and str(parsed) == value:
            return _b64e(parsed.bytes)
        return _RAW + (urllib.parse.quote(value, safe=_SAFE) or _EMPTY)
    raise CompactKeyError("an id claim is not a string")


def _unpack_id(segment: str) -> str:
    if segment.startswith(_RAW):
        rest = segment[1:]
        return "" if rest == _EMPTY else urllib.parse.unquote(rest)
    try:
        raw = _b64d(segment)
    except (binascii.Error, ValueError) as exc:
        raise CompactKeyError("malformed id segment") from exc
    if len(raw) != 16:
        raise CompactKeyError("malformed id segment")
    return str(uuid.UUID(bytes=raw))


def _pack_text(value) -> str:
    if value is None:
        return _NULL
    if not isinstance(value, str):
        raise CompactKeyError("email or name is not a string")
    if value == "":
        return _EMPTY
    return urllib.parse.quote(value, safe=_SAFE)


def _unpack_text(segment: str):
    if segment == _NULL:
        return None
    if segment == _EMPTY:
        return ""
    return urllib.parse.unquote(segment)


def _pack_list(values: list, item) -> str:
    return ",".join(item(v) for v in values)


def pack(header_and_payload: str) -> str:
    """``header.payload`` of an Okareo API key → the token-URL path.

    Raises ``CompactKeyError`` when the input carries a signature, isn't an
    okareo-server API key, or can't be rebuilt byte-for-byte.
    """
    parts = header_and_payload.strip().split(".")
    if len(parts) == 3:
        raise CompactKeyError(
            "send only the first two parts of the key; the third is the secret"
        )
    if len(parts) != 2:
        raise CompactKeyError("not the first two parts of an Okareo API key")
    header_b64, payload_b64 = parts
    try:
        header = _b64d(header_b64).decode("utf-8")
        claims = json.loads(_b64d(payload_b64))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise CompactKeyError("not the first two parts of an Okareo API key") from exc
    if header != _HEADER:
        raise CompactKeyError("the key's header is not okareo-server's RS256 header")
    if not isinstance(claims, dict) or claims.get("type") != "apiKey":
        raise CompactKeyError("not an Okareo API key (type is not apiKey)")
    expected = list(_CLAIM_ORDER) + (["exp"] if "exp" in claims else [])
    if list(claims) != expected:
        raise CompactKeyError(
            "the key's claims are not in okareo-server's layout; "
            "the MCP needs updating for this key format"
        )
    for name in ("iat", "exp"):
        if name in claims and (not isinstance(claims[name], int) or claims[name] < 0):
            raise CompactKeyError(f"{name} is not a whole number")
    if not isinstance(claims["roles"], list) or not isinstance(claims["tenantIds"], list):
        raise CompactKeyError("roles or tenantIds is not a list")

    segments = [
        VERSION,
        _pack_id(claims["sub"]),
        _pack_id(claims["tenantId"]),
        _pack_id(claims["jti"]),
        str(claims["iat"]),
        _pack_text(claims["email"]),
        _pack_text(claims["name"]),
    ]
    if claims["roles"] != DEFAULT_ROLES:
        if not claims["roles"] or any(not isinstance(v, str) or v == "" for v in claims["roles"]):
            raise CompactKeyError("roles must be non-empty strings")
        segments.append("r=" + _pack_list(claims["roles"], lambda v: urllib.parse.quote(v, safe="")))
    if claims["tenantIds"] != [claims["tenantId"]]:
        segments.append("t=" + _pack_list(claims["tenantIds"], _pack_id))
    if claims["iss"] != DEFAULT_ISS:
        segments.append("i=" + urllib.parse.quote(str(claims["iss"]), safe=""))
    if claims["aud"] != DEFAULT_AUD:
        segments.append("a=" + urllib.parse.quote(str(claims["aud"]), safe=""))
    if "exp" in claims:
        segments.append(f"x={claims['exp']}")
    path = "/".join(segments)

    if _rebuild_header_and_payload(path) != f"{header_b64}.{payload_b64}":
        raise CompactKeyError("this key can't be rebuilt exactly from its claims")
    return path


def unpack(path: str, signature: str) -> str:
    """Token-URL path + client secret → the full API key.

    ``path`` must be the raw (still percent-encoded) path after
    ``/oauth/shared/token/``. A tampered path rebuilds a different key, which
    okareo-server then refuses; this function only checks the shape.
    """
    if not signature or "." in signature:
        raise CompactKeyError("the client secret is not a key signature")
    return f"{_rebuild_header_and_payload(path)}.{signature}"


def _rebuild_header_and_payload(path: str) -> str:
    segments = path.split("/")
    if len(segments) < 7 or segments[0] != VERSION:
        raise CompactKeyError("malformed token path")
    _, sub, tenant, jti, iat, email, name, *optional = segments
    if not iat.isdigit():
        raise CompactKeyError("malformed token path")
    tenant_id = _unpack_id(tenant)
    extra: dict[str, str] = {}
    for segment in optional:
        key, sep, value = segment.partition("=")
        if not sep or key not in ("r", "t", "i", "a", "x") or key in extra:
            raise CompactKeyError("malformed token path")
        extra[key] = value

    claims: dict = {
        "sub": _unpack_id(sub),
        "tenantId": tenant_id,
        "tenantIds": (
            [_unpack_id(v) for v in extra["t"].split(",")] if "t" in extra else [tenant_id]
        ),
        "type": "apiKey",
        "roles": (
            [urllib.parse.unquote(v) for v in extra["r"].split(",")]
            if "r" in extra else list(DEFAULT_ROLES)
        ),
        "email": _unpack_text(email),
        "name": _unpack_text(name),
        "iss": urllib.parse.unquote(extra["i"]) if "i" in extra else DEFAULT_ISS,
        "aud": urllib.parse.unquote(extra["a"]) if "a" in extra else DEFAULT_AUD,
        "iat": int(iat),
        "jti": _unpack_id(jti),
    }
    if "x" in extra:
        if not extra["x"].isdigit():
            raise CompactKeyError("malformed token path")
        claims["exp"] = int(extra["x"])
    return f"{_b64e(_HEADER.encode('utf-8'))}.{_encode_json(claims)}"
