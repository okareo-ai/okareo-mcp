"""Shared connections: Manual OAuth for a client whose secret is an API key.

Any OAuth client a customer configures by hand (a Slack app is the first)
uses client ID ``okareo-shared-connection`` and an Okareo API key as its
client secret. Every member it connects acts as that key. Routes:

- ``GET /oauth/shared/authorize`` — shows nothing; 302s straight back to the
  callback the client sent, with a code. The client secret is not sent on
  this leg, so there is nothing to check yet and no one to ask. Any https
  callback is accepted (http only on loopback), so no tool is special-cased
  and no server configuration is needed; the cost, accepted by the owner, is
  that this route can bounce a browser to any https site. The code it hands
  out is worthless without the key (research R10).
- ``POST /oauth/shared/token`` — authenticates the client by its secret,
  asks okareo-server whether that key is valid, and returns an access and
  refresh token that carry the key encrypted.

Nothing is stored. The token call comes from the client's servers, not the
member's browser, so it can reach a different instance than the authorize
call did; codes and tokens are therefore self-contained (research R8):

- Code: ``okshc_<b64 json>.<b64 hmac>``, 5 minutes. It carries no authority
  — the key only arrives with the token request — so single use is enforced
  per instance only.
- Tokens: ``okmcp_at_`` / ``okmcp_rt_`` + Fernet (AES-CBC + HMAC). The key
  inside is unreadable to the client, to logs and to okareo-server, which is
  what keeps the token from working anywhere but here (FR-013, FR-014).

Both keys are derived with HKDF from ``MCP_DCR_SIGNING_KEY``, so rotating
that secret disconnects every shared connection. These routes are not in the
authorization server document: Manual OAuth clients are given the URLs, and
DCR clients must keep seeing exactly what they saw before (FR-015).

See specs/048-api-key-shared-connections/contracts/shared-oauth.md.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import sys
import time
import urllib.parse
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from src.auth.api_key_verifier import KeyValidation
from src.auth.compact_key import CompactKeyError, unpack
from src.auth.oauth_proxy import _error_response, _pkce_verify
from src.auth.oauth_state import _b64url_decode, _b64url_encode

CLIENT_ID = "okareo-shared-connection"
CODE_PREFIX = "okshc_"
ACCESS_PREFIX = "okmcp_at_"
REFRESH_PREFIX = "okmcp_rt_"
CODE_TTL = 300
ACCESS_TTL = 3600
# Refreshing issues a new refresh token, so a connection in use never
# reaches this; one left idle this long must reconnect.
REFRESH_TTL = 90 * 24 * 3600
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
SCOPE = "okareo:use"

_INVALID_KEY = "This Okareo API key is not valid (revoked, expired, or unknown)."
_UNAVAILABLE = "Okareo could not be reached to check this API key. Retry shortly."
_DISABLED = (
    "Shared connections are not available on this server: "
    "MCP_DCR_SIGNING_KEY is not configured."
)

TokenType = Literal["at", "rt"]
KeyValidator = Callable[..., Awaitable[KeyValidation]]


class InvalidSharedCodeError(ValueError):
    """The code is malformed, forged, expired, or for another client."""


class InvalidSharedTokenError(ValueError):
    """The token is malformed, forged, expired, or of the wrong type.

    One error for every cause: telling a caller which check failed would
    help only someone probing tokens.
    """


@dataclass(frozen=True)
class SharedConnectionKeys:
    code_key: bytes
    token_key: bytes

    @classmethod
    def from_signing_key(cls, signing_key: str) -> "SharedConnectionKeys":
        ikm = signing_key.encode("utf-8")
        return cls(
            code_key=_hkdf(ikm, b"okareo-mcp/shared-code/v1"),
            token_key=_hkdf(ikm, b"okareo-mcp/shared-token/v1"),
        )

    @property
    def fernet(self) -> Fernet:
        return Fernet(base64.urlsafe_b64encode(self.token_key))


def _hkdf(ikm: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(ikm)


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else int(now)


# ---------------------------------------------------------------------------
# Codes
# ---------------------------------------------------------------------------


def encode_code(
    keys: SharedConnectionKeys,
    *,
    redirect_uri: str,
    code_challenge: str | None,
    now: int | None = None,
) -> str:
    payload: dict = {
        "cid": CLIENT_ID,
        "ru": redirect_uri,
        "exp": _now(now) + CODE_TTL,
        "n": secrets.token_urlsafe(16),
    }
    if code_challenge:
        payload["cc"] = code_challenge
        payload["ccm"] = "S256"
    payload_b64 = _b64url_encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    mac = hmac.new(keys.code_key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{CODE_PREFIX}{payload_b64}.{_b64url_encode(mac)}"


def decode_code(code: str, keys: SharedConnectionKeys, *, now: int | None = None) -> dict:
    if not isinstance(code, str) or not code.startswith(CODE_PREFIX):
        raise InvalidSharedCodeError("wrong prefix")
    payload_b64, sep, mac_b64 = code[len(CODE_PREFIX):].rpartition(".")
    if not sep or not payload_b64 or not mac_b64:
        raise InvalidSharedCodeError("malformed")
    expected = _b64url_encode(
        hmac.new(keys.code_key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(mac_b64, expected):
        raise InvalidSharedCodeError("bad signature")
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise InvalidSharedCodeError("unreadable") from exc
    if not isinstance(payload, dict) or payload.get("cid") != CLIENT_ID:
        raise InvalidSharedCodeError("wrong client")
    exp = payload.get("exp")
    if not isinstance(exp, int) or _now(now) > exp:
        raise InvalidSharedCodeError("expired")
    return payload


class UsedCodes:
    """Nonces of codes already redeemed on this instance, kept until expiry."""

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def claim(self, nonce: str, exp: int, now: int | None = None) -> bool:
        current = _now(now)
        self._seen = {n: e for n, e in self._seen.items() if e >= current}
        if nonce in self._seen:
            return False
        self._seen[nonce] = exp
        return True


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def _seal(keys: SharedConnectionKeys, typ: TokenType, api_key: str, tenant_id: str, now: int) -> str:
    body = json.dumps(
        {"v": 1, "typ": typ, "k": api_key, "t": tenant_id, "cid": CLIENT_ID},
        separators=(",", ":"),
    ).encode("utf-8")
    prefix = ACCESS_PREFIX if typ == "at" else REFRESH_PREFIX
    return prefix + keys.fernet.encrypt_at_time(body, now).decode("ascii")


def issue_tokens(
    keys: SharedConnectionKeys, api_key: str, tenant_id: str, *, now: int | None = None
) -> dict:
    current = _now(now)
    return {
        "access_token": _seal(keys, "at", api_key, tenant_id, current),
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL,
        "refresh_token": _seal(keys, "rt", api_key, tenant_id, current),
        "scope": SCOPE,
    }


def open_token(
    token: str,
    keys: SharedConnectionKeys,
    expected_typ: TokenType,
    *,
    now: int | None = None,
) -> dict:
    prefix = ACCESS_PREFIX if expected_typ == "at" else REFRESH_PREFIX
    ttl = ACCESS_TTL if expected_typ == "at" else REFRESH_TTL
    if not isinstance(token, str) or not token.startswith(prefix):
        raise InvalidSharedTokenError()
    try:
        body = keys.fernet.decrypt_at_time(
            token[len(prefix):].encode("ascii"), ttl, _now(now)
        )
        payload = json.loads(body.decode("utf-8"))
    except (InvalidToken, ValueError, UnicodeError):
        raise InvalidSharedTokenError() from None
    if (
        not isinstance(payload, dict)
        or payload.get("typ") != expected_typ
        or payload.get("cid") != CLIENT_ID
        or not isinstance(payload.get("k"), str)
    ):
        raise InvalidSharedTokenError()
    return payload


def is_acceptable_redirect_uri(uri: str | None) -> bool:
    """An absolute https URI with no fragment, or http on a loopback host.

    RFC 6749 §3.1.2 forbids a fragment. Plain http is allowed only where it
    can't cross a network, for testing a client locally.
    """
    if not uri:
        return False
    try:
        parsed = urllib.parse.urlsplit(uri)
    except ValueError:
        return False
    if parsed.fragment or not parsed.hostname:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _with_query(uri: str, params: dict[str, str | None]) -> str:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return f"{uri}{'&' if '?' in uri else '?'}{query}"


def _disabled() -> JSONResponse:
    return JSONResponse(
        {"error": "temporarily_unavailable", "error_description": _DISABLED},
        status_code=503,
    )


def make_shared_authorize_route(keys: SharedConnectionKeys | None):
    async def _route(request: Request) -> Response:
        q = request.query_params
        diag = _Diag("authorize")
        diag.params(q.keys())
        diag.set(pkce="yes" if q.get("code_challenge") else "no")
        response = _authorize(keys, q, diag)
        diag.emit(response)
        return response

    return _route


def _authorize(keys, q, diag: "_Diag") -> Response:
    if keys is None:
        return diag.fail("disabled", _disabled())
    redirect_uri = q.get("redirect_uri")
    # An unusable callback gets an error page, not a redirect: there is
    # nowhere safe to send the browser.
    if not is_acceptable_redirect_uri(redirect_uri):
        return diag.fail(
            "bad_redirect_uri",
            PlainTextResponse(
                "redirect_uri must be an absolute https URL without a fragment "
                "(http is accepted only for localhost).",
                status_code=400,
            ),
        )
    # The host alone identifies which tool is calling; the path and query
    # can carry the client's own identifiers, so they are not logged.
    diag.set(callback_host=urllib.parse.urlsplit(redirect_uri).hostname)
    state = q.get("state")

    def _fail(reason: str, error: str, description: str) -> Response:
        diag.set(error=error)
        return diag.fail(
            reason,
            RedirectResponse(
                _with_query(
                    redirect_uri,
                    {"error": error, "error_description": description, "state": state},
                ),
                status_code=302,
            ),
        )

    if q.get("client_id") != CLIENT_ID:
        return _fail("wrong_client_id", "unauthorized_client", f"client_id must be {CLIENT_ID}")
    if q.get("response_type") != "code":
        return _fail(
            "wrong_response_type",
            "unsupported_response_type",
            "Only response_type=code is supported",
        )
    challenge = q.get("code_challenge")
    if challenge and q.get("code_challenge_method") != "S256":
        return _fail(
            "bad_pkce_method", "invalid_request", "Only code_challenge_method=S256 is supported"
        )

    code = encode_code(keys, redirect_uri=redirect_uri, code_challenge=challenge)
    return RedirectResponse(
        _with_query(redirect_uri, {"code": code, "state": state}), status_code=302
    )


def _invalid_client(description: str) -> JSONResponse:
    response = _error_response(401, "invalid_client", description)
    # The client authenticates with HTTP Basic or form credentials here, not
    # a bearer, so challenge for that (RFC 6749 §5.2).
    response.headers["WWW-Authenticate"] = f'Basic realm="{CLIENT_ID}"'
    return response


def _unavailable() -> JSONResponse:
    response = _error_response(503, "temporarily_unavailable", _UNAVAILABLE)
    response.headers["Retry-After"] = "5"
    return response


def _no_store(body: dict) -> JSONResponse:
    return JSONResponse(body, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _client_credentials(request: Request, form) -> tuple[str, str] | None:
    """(client_id, client_secret) from exactly one of Basic or the form body."""
    header = request.headers.get("authorization", "")
    form_secret = form.get("client_secret")
    if header[:6].lower() == "basic ":
        if form_secret is not None:
            return None
        try:
            decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
        raw_id, sep, raw_secret = decoded.partition(":")
        if not sep:
            return None
        # RFC 6749 §2.3.1: both halves are form-urlencoded before encoding.
        client_id = urllib.parse.unquote_plus(raw_id)
        secret = urllib.parse.unquote_plus(raw_secret)
        form_id = form.get("client_id")
        if form_id is not None and form_id != client_id:
            return None
        return client_id, secret
    if form_secret is None:
        return None
    return form.get("client_id") or "", form_secret


def make_shared_token_route(keys: SharedConnectionKeys | None, validator: KeyValidator):
    used_codes = UsedCodes()

    async def _route(request: Request) -> Response:
        diag = _Diag("token")
        diag.set(ua=request.headers.get("user-agent"))
        if keys is None:
            response = diag.fail("disabled", _disabled())
            diag.emit(response)
            return response
        form = await request.form()
        diag.params(form.keys())
        has_basic = request.headers.get("authorization", "")[:6].lower() == "basic "
        has_post = form.get("client_secret") is not None
        diag.set(
            grant=form.get("grant_type"),
            client_auth={
                (True, True): "both",
                (True, False): "basic",
                (False, True): "post",
                (False, False): "none",
            }[(has_basic, has_post)],
            pkce="yes" if form.get("code_verifier") else "no",
        )
        compact = _compact_path(request)
        diag.set(key_source="path" if compact else "secret")
        response = await _token(request, form, keys, validator, used_codes, diag, compact)
        diag.emit(response)
        return response

    return _route


_TOKEN_PATH_PREFIX = "/oauth/shared/token/"


def _compact_path(request: Request) -> str | None:
    """The packed key claims after ``/oauth/shared/token/``, still encoded.

    Read from ``raw_path`` because Starlette's ``path`` is percent-decoded,
    which would turn an encoded ``/`` in a name into a segment separator.
    """
    raw = request.scope.get("raw_path") or request.url.path.encode("utf-8")
    path = raw.decode("latin-1")
    if not path.startswith(_TOKEN_PATH_PREFIX):
        return None
    return path[len(_TOKEN_PATH_PREFIX):] or None


async def _token(
    request, form, keys, validator, used_codes, diag: "_Diag", compact: str | None
) -> Response:
    grant_type = form.get("grant_type")
    if grant_type not in ("authorization_code", "refresh_token"):
        return diag.fail(
            "unsupported_grant_type",
            _error_response(
                400,
                "unsupported_grant_type",
                "Supported grant_types: authorization_code, refresh_token",
            ),
        )
    credentials = _client_credentials(request, form)
    if credentials is None:
        return diag.fail(
            "client_auth",
            _invalid_client(
                "Authenticate with exactly one of HTTP Basic or client_id/client_secret."
            ),
        )
    client_id, secret = credentials
    if client_id != CLIENT_ID:
        return diag.fail("wrong_client_id", _invalid_client(f"client_id must be {CLIENT_ID}"))
    if compact:
        # The client secret is only the key's signature; the rest of the key
        # travels in the token URL (research R14).
        try:
            secret = unpack(compact, secret)
        except CompactKeyError:
            return diag.fail(
                "bad_claims_path",
                _invalid_client(
                    "The token URL and client secret don't form an Okareo API key. "
                    "Generate both again on the shared-connection setup page."
                ),
            )

    if grant_type == "authorization_code":
        return await _redeem_code(form, keys, secret, validator, used_codes, diag)
    return await _refresh(form, keys, secret, validator, diag)


async def _redeem_code(form, keys, secret, validator, used_codes, diag: "_Diag") -> Response:
    try:
        code = decode_code(form.get("code") or "", keys)
    except InvalidSharedCodeError:
        return diag.fail(
            "bad_code", _error_response(400, "invalid_grant", "Unknown, expired, or forged code")
        )
    if form.get("redirect_uri") != code["ru"]:
        return diag.fail(
            "redirect_uri_mismatch" if form.get("redirect_uri") else "redirect_uri_missing",
            _error_response(
                400, "invalid_grant", "redirect_uri does not match the authorization request"
            ),
        )
    if not used_codes.claim(code["n"], code["exp"]):
        return diag.fail(
            "code_reused", _error_response(400, "invalid_grant", "Code already used")
        )
    challenge = code.get("cc")
    if challenge:
        verifier = form.get("code_verifier")
        if not verifier or not _pkce_verify(verifier, challenge):
            return diag.fail(
                "pkce_mismatch" if verifier else "pkce_missing",
                _error_response(
                    400, "invalid_grant", "PKCE verifier does not match the challenge"
                ),
            )

    # Issuing tokens is rare, and a just-revoked key must not slip through
    # on a cached answer, so this always asks okareo-server.
    result = await validator(secret, use_cache=False)
    if result.outcome == "unavailable":
        return diag.fail("key_unavailable", _unavailable())
    if result.outcome != "valid" or not result.tenant_id:
        return diag.fail("key_invalid", _invalid_client(_INVALID_KEY))
    diag.set(tenant=result.tenant_id)
    return _no_store(issue_tokens(keys, secret, result.tenant_id))


async def _refresh(form, keys, secret, validator, diag: "_Diag") -> Response:
    try:
        payload = open_token(form.get("refresh_token") or "", keys, "rt")
    except InvalidSharedTokenError:
        return diag.fail(
            "bad_refresh_token",
            _error_response(400, "invalid_grant", "Unknown or expired refresh token"),
        )
    presented = hashlib.sha256(secret.encode("utf-8")).digest()
    bound = hashlib.sha256(payload["k"].encode("utf-8")).digest()
    if not hmac.compare_digest(presented, bound):
        # The administrator put a different key into the client.
        return diag.fail(
            "secret_changed",
            _error_response(
                400,
                "invalid_grant",
                "The client secret no longer matches this connection; reconnect.",
            ),
        )
    result = await validator(secret, use_cache=False)
    if result.outcome == "unavailable":
        return diag.fail("key_unavailable", _unavailable())
    if result.outcome != "valid" or not result.tenant_id:
        return diag.fail("key_invalid", _error_response(400, "invalid_grant", _INVALID_KEY))
    diag.set(tenant=result.tenant_id)
    return _no_store(issue_tokens(keys, secret, result.tenant_id))


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

# Everything logged comes from the caller, so anything outside this set is
# replaced: no spaces, quotes, brackets or newlines, so one request is one
# parseable line, and a caller can't forge another or the line's marker.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_.:/()+\-]")
_MAX_PARAM_NAMES = 20
_FIELD_ORDER = (
    "route", "status", "outcome", "error", "reason", "grant", "client_auth",
    "pkce", "key_source", "params", "callback_host", "tenant", "ua",
)


class _Diag:
    """One ``[shared-oauth]`` line per request: the shape of what the client
    sent and how we answered, never a value that grants anything.

    Written to stderr like the verifier's diagnostics, so it appears in
    Cloud Run logs although production runs at WARNING (research R13).
    Logged: parameter names, grant type, client-auth method, whether PKCE was
    used, the callback's host, the error and an internal reason, the tenant
    once the key is accepted, and the client's User-Agent. Never logged: the
    client secret or API key, codes, tokens, ``state``, PKCE values, or the
    callback's path and query.
    """

    def __init__(self, route: str) -> None:
        self._fields: dict[str, str] = {"route": route}
        self._reason: str | None = None

    def set(self, **fields: str | None) -> None:
        for name, value in fields.items():
            if value:
                self._fields[name] = _safe(value)

    def params(self, names) -> None:
        cleaned = sorted({_safe(n, 40) for n in names})
        self._fields["params"] = ",".join(cleaned[:_MAX_PARAM_NAMES]) or "-"

    def fail(self, reason: str, response: Response) -> Response:
        self._reason = reason
        return response

    def emit(self, response: Response) -> None:
        fields = dict(self._fields)
        fields["status"] = str(response.status_code)
        fields["outcome"] = "error" if self._reason else "ok"
        if self._reason:
            fields["reason"] = self._reason
            fields.setdefault("error", _error_code(response))
        line = " ".join(f"{k}={fields[k]}" for k in _FIELD_ORDER if fields.get(k))
        print(f"[shared-oauth] {line}", file=sys.stderr, flush=True)


def _safe(value: str, limit: int = 60) -> str:
    return _UNSAFE_CHARS.sub("_", str(value)[:limit]) or "-"


def _error_code(response: Response) -> str | None:
    if not isinstance(response, JSONResponse):
        return None
    try:
        return json.loads(response.body).get("error")
    except (ValueError, AttributeError):
        return None


def register_shared_connection_routes(
    mcp,
    *,
    keys: SharedConnectionKeys | None,
    validator: KeyValidator,
    base_url: str,
) -> None:
    """Mount the shared-connection routes. ``keys=None`` → OAuth routes 503."""
    from src.auth.shared_setup import make_pack_route, make_setup_page_route

    mcp.custom_route("/oauth/shared/authorize", methods=["GET"])(
        make_shared_authorize_route(keys)
    )
    token_route = make_shared_token_route(keys, validator)
    mcp.custom_route("/oauth/shared/token", methods=["POST"])(token_route)
    # Same handler; the path carries the key's claims for clients whose
    # secret field can't hold a whole key (research R14).
    mcp.custom_route("/oauth/shared/token/{compact:path}", methods=["POST"])(token_route)
    mcp.custom_route("/oauth/shared/setup", methods=["GET"])(make_setup_page_route(base_url))
    mcp.custom_route("/oauth/shared/setup/pack", methods=["POST"])(make_pack_route(base_url))
