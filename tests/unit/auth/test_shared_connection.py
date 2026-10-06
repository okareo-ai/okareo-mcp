"""Tests for src/auth/shared_connection.py (048 US2).

Crypto primitives (research R8, data-model.md) and the two Manual OAuth
routes (contracts/shared-oauth.md). okareo-server is mocked through a stub
validator; ``tests/integration/test_shared_connection_flow.py`` drives the
real verifier end to end.
"""

from __future__ import annotations

import base64
import hashlib
import urllib.parse

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.auth.api_key_verifier import KeyValidation
from src.auth.shared_connection import (
    ACCESS_PREFIX,
    ACCESS_TTL,
    CLIENT_ID,
    CODE_PREFIX,
    CODE_TTL,
    REFRESH_PREFIX,
    REFRESH_TTL,
    InvalidSharedCodeError,
    InvalidSharedTokenError,
    SharedConnectionKeys,
    decode_code,
    encode_code,
    is_acceptable_redirect_uri,
    issue_tokens,
    make_shared_authorize_route,
    make_shared_token_route,
    open_token,
)

SIGNING_KEY = "a-stable-dcr-signing-key-of-at-least-32-bytes"
CALLBACK = "https://tools.customer.example/oauth/callback"
API_KEY = "eyJhbGciOiJIUzI1NiJ9.eyJ0eXBlIjoiYXBpS2V5IiwidGVuYW50SWQiOiJ0LTEifQ.c2lnbmF0dXJlLXNlZ21lbnQtZm9yLXRlc3Rz"


@pytest.fixture
def keys() -> SharedConnectionKeys:
    return SharedConnectionKeys.from_signing_key(SIGNING_KEY)


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


class TestKeys:
    def test_derivation_is_deterministic(self):
        a = SharedConnectionKeys.from_signing_key(SIGNING_KEY)
        b = SharedConnectionKeys.from_signing_key(SIGNING_KEY)
        assert a.code_key == b.code_key
        token = issue_tokens(a, API_KEY, "t-1")["access_token"]
        assert open_token(token, b, "at")["k"] == API_KEY

    def test_code_and_token_keys_are_distinct_and_not_the_signing_key(self, keys):
        assert keys.code_key != keys.token_key
        assert SIGNING_KEY.encode() not in (keys.code_key, keys.token_key)

    def test_different_signing_key_cannot_open_tokens(self, keys):
        token = issue_tokens(keys, API_KEY, "t-1")["access_token"]
        other = SharedConnectionKeys.from_signing_key("another-signing-key-32-bytes-long!!")
        with pytest.raises(InvalidSharedTokenError):
            open_token(token, other, "at")


class TestCode:
    def test_round_trip(self, keys):
        code = encode_code(keys, redirect_uri=CALLBACK, code_challenge="abc", now=1000)
        assert code.startswith(CODE_PREFIX)
        payload = decode_code(code, keys, now=1000 + CODE_TTL - 1)
        assert payload["cid"] == CLIENT_ID
        assert payload["ru"] == CALLBACK
        assert payload["cc"] == "abc"
        assert payload["exp"] == 1000 + CODE_TTL

    def test_expired(self, keys):
        code = encode_code(keys, redirect_uri=CALLBACK, code_challenge=None, now=1000)
        with pytest.raises(InvalidSharedCodeError):
            decode_code(code, keys, now=1000 + CODE_TTL + 1)

    @pytest.mark.parametrize("where", ["payload", "mac"])
    def test_tampered(self, keys, where):
        code = encode_code(keys, redirect_uri=CALLBACK, code_challenge=None, now=1000)
        body = code[len(CODE_PREFIX):]
        payload, mac = body.rsplit(".", 1)
        if where == "payload":
            payload = payload[:-1] + ("A" if payload[-1] != "A" else "B")
        else:
            mac = mac[:-1] + ("A" if mac[-1] != "A" else "B")
        with pytest.raises(InvalidSharedCodeError):
            decode_code(f"{CODE_PREFIX}{payload}.{mac}", keys, now=1000)

    @pytest.mark.parametrize("code", ["", "okshc_", "okshc_abc", "mcp_abc.def", "garbage"])
    def test_malformed(self, keys, code):
        with pytest.raises(InvalidSharedCodeError):
            decode_code(code, keys, now=1000)

    def test_codes_never_repeat(self, keys):
        a = encode_code(keys, redirect_uri=CALLBACK, code_challenge="x", now=1000)
        b = encode_code(keys, redirect_uri=CALLBACK, code_challenge="x", now=1000)
        assert a != b


class TestTokens:
    def test_access_token_round_trip_hides_the_key(self, keys):
        body = issue_tokens(keys, API_KEY, "t-1", now=1000)
        at = body["access_token"]
        assert at.startswith(ACCESS_PREFIX)
        assert body["refresh_token"].startswith(REFRESH_PREFIX)
        assert API_KEY not in at and API_KEY not in body["refresh_token"]
        # Not even the key's signature segment leaks.
        assert API_KEY.rsplit(".", 1)[1][:20] not in at
        payload = open_token(at, keys, "at", now=1000 + ACCESS_TTL - 1)
        assert payload["k"] == API_KEY
        assert payload["t"] == "t-1"
        assert payload["cid"] == CLIENT_ID

    def test_response_body_shape(self, keys):
        body = issue_tokens(keys, API_KEY, "t-1", now=1000)
        assert set(body) == {"access_token", "token_type", "expires_in", "refresh_token", "scope"}
        assert body["token_type"] == "Bearer"
        assert body["expires_in"] == ACCESS_TTL
        assert body["scope"] == "okareo:use"

    def test_access_token_expires(self, keys):
        at = issue_tokens(keys, API_KEY, "t-1", now=1000)["access_token"]
        with pytest.raises(InvalidSharedTokenError):
            open_token(at, keys, "at", now=1000 + ACCESS_TTL + 1)

    def test_refresh_token_lifetime(self, keys):
        rt = issue_tokens(keys, API_KEY, "t-1", now=1000)["refresh_token"]
        day = 24 * 3600
        assert open_token(rt, keys, "rt", now=1000 + 89 * day)["k"] == API_KEY
        with pytest.raises(InvalidSharedTokenError):
            open_token(rt, keys, "rt", now=1000 + REFRESH_TTL + 1)

    def test_types_are_not_interchangeable(self, keys):
        body = issue_tokens(keys, API_KEY, "t-1", now=1000)
        with pytest.raises(InvalidSharedTokenError):
            open_token(body["refresh_token"], keys, "at", now=1000)
        with pytest.raises(InvalidSharedTokenError):
            open_token(body["access_token"], keys, "rt", now=1000)

    @pytest.mark.parametrize("token", ["", "okmcp_at_", "okmcp_at_notfernet", API_KEY])
    def test_garbage(self, keys, token):
        with pytest.raises(InvalidSharedTokenError):
            open_token(token, keys, "at")


class TestRedirectUriRule:
    """No tool is special-cased: any https callback works (research R10)."""

    @pytest.mark.parametrize(
        "uri",
        [
            "https://oauth2.slack.com/external/auth/callback",
            "https://tools.customer.example/oauth/callback?tenant=1",
            "http://localhost:8765/cb",
            "http://127.0.0.1/cb",
            "http://[::1]:9000/cb",
        ],
    )
    def test_accepted(self, uri):
        assert is_acceptable_redirect_uri(uri)

    @pytest.mark.parametrize(
        "uri",
        [
            None,
            "",
            "http://tools.customer.example/cb",
            "https://tools.customer.example/cb#frag",
            "javascript:alert(1)",
            "/relative/cb",
            "https:///no-host",
            "ftp://files.example/cb",
        ],
    )
    def test_refused(self, uri):
        assert not is_acceptable_redirect_uri(uri)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class _Validator:
    """Stub okareo-server: answers with ``self.outcome``; records calls."""

    def __init__(self, outcome: str = "valid") -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, bool]] = []

    async def __call__(self, key: str, *, use_cache: bool = True) -> KeyValidation:
        self.calls.append((key, use_cache))
        if self.outcome == "valid" and key == API_KEY:
            return KeyValidation(outcome="valid", tenant_id="t-1", subject="u-1")
        if self.outcome == "unavailable":
            return KeyValidation(outcome="unavailable")
        return KeyValidation(outcome="invalid")


def _client(keys, validator=None) -> TestClient:
    validator = validator or _Validator()
    app = Starlette(
        routes=[
            Route(
                "/oauth/shared/authorize",
                make_shared_authorize_route(keys),
                methods=["GET"],
            ),
            Route(
                "/oauth/shared/token",
                make_shared_token_route(keys, validator),
                methods=["POST"],
            ),
            Route(
                "/oauth/shared/token/{compact:path}",
                make_shared_token_route(keys, validator),
                methods=["POST"],
            ),
        ]
    )
    return TestClient(app, follow_redirects=False)


def _pkce() -> tuple[str, str]:
    verifier = "v" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _authorize(client, **overrides):
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": CALLBACK,
        "state": "xyz",
    }
    params.update(overrides)
    params = {k: v for k, v in params.items() if v is not None}
    return client.get("/oauth/shared/authorize", params=params)


def _code(client, challenge: str | None = None) -> str:
    extra = {"code_challenge": challenge, "code_challenge_method": "S256"} if challenge else {}
    r = _authorize(client, **extra)
    assert r.status_code == 302, r.text
    return urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)["code"][0]


def _basic(client_id: str = CLIENT_ID, secret: str = API_KEY) -> dict:
    raw = f"{urllib.parse.quote(client_id, safe='')}:{urllib.parse.quote(secret, safe='')}"
    return {"Authorization": "Basic " + base64.b64encode(raw.encode()).decode()}


class TestAuthorize:
    def test_redirects_straight_back_with_code_and_state(self, keys):
        r = _authorize(_client(keys))
        assert r.status_code == 302
        loc = urllib.parse.urlparse(r.headers["location"])
        assert f"{loc.scheme}://{loc.netloc}{loc.path}" == CALLBACK
        q = urllib.parse.parse_qs(loc.query)
        assert q["state"] == ["xyz"]
        assert q["code"][0].startswith(CODE_PREFIX)

    @pytest.mark.parametrize("uri", [None, "http://tools.customer.example/cb", CALLBACK + "#x"])
    def test_unusable_redirect_uri_is_400_without_redirect(self, keys, uri):
        r = _authorize(_client(keys), redirect_uri=uri)
        assert r.status_code == 400
        assert "location" not in r.headers

    def test_wrong_client_id_redirects_with_error(self, keys):
        r = _authorize(_client(keys), client_id="someone-else")
        assert r.status_code == 302
        q = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
        assert q["error"] == ["unauthorized_client"]
        assert q["state"] == ["xyz"]
        assert "code" not in q

    def test_unsupported_response_type(self, keys):
        r = _authorize(_client(keys), response_type="token")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
        assert q["error"] == ["unsupported_response_type"]

    @pytest.mark.parametrize("method", ["plain", None])
    def test_pkce_must_be_s256(self, keys, method):
        r = _authorize(_client(keys), code_challenge="abc", code_challenge_method=method)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
        assert q["error"] == ["invalid_request"]

    def test_callback_with_its_own_query_keeps_it(self, keys):
        r = _authorize(_client(keys), redirect_uri="https://cb.example/x?tenant=1")
        loc = urllib.parse.urlparse(r.headers["location"])
        q = urllib.parse.parse_qs(loc.query)
        assert q["tenant"] == ["1"]
        assert q["code"][0].startswith(CODE_PREFIX)

    def test_disabled_without_stable_signing_key(self):
        r = _authorize(_client(None))
        assert r.status_code == 503
        assert r.json()["error"] == "temporarily_unavailable"


class TestTokenAuthorizationCode:
    def _exchange(self, client, code, *, headers=None, **form):
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK}
        data.update(form)
        return client.post("/oauth/shared/token", data=data, headers=headers or {})

    def test_basic_auth(self, keys):
        client = _client(keys)
        r = self._exchange(client, _code(client), headers=_basic())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["access_token"].startswith(ACCESS_PREFIX)
        assert body["refresh_token"].startswith(REFRESH_PREFIX)
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["pragma"] == "no-cache"

    def test_post_body_auth(self, keys):
        client = _client(keys)
        r = self._exchange(client, _code(client), client_id=CLIENT_ID, client_secret=API_KEY)
        assert r.status_code == 200, r.text

    def test_both_auth_methods_is_invalid_client(self, keys):
        client = _client(keys)
        r = self._exchange(
            client, _code(client), headers=_basic(), client_id=CLIENT_ID, client_secret=API_KEY
        )
        assert r.status_code == 401
        assert r.json()["error"] == "invalid_client"
        assert r.headers["www-authenticate"].startswith("Basic")

    @pytest.mark.parametrize("auth", ["none", "wrong-id"])
    def test_missing_or_wrong_client(self, keys, auth):
        client = _client(keys)
        headers = _basic(client_id="someone-else") if auth == "wrong-id" else {}
        r = self._exchange(client, _code(client), headers=headers)
        assert r.status_code == 401
        assert r.json()["error"] == "invalid_client"

    def test_code_is_single_use_on_an_instance(self, keys):
        client = _client(keys)
        code = _code(client)
        assert self._exchange(client, code, headers=_basic()).status_code == 200
        r = self._exchange(client, code, headers=_basic())
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_redirect_uri_must_match(self, keys):
        client = _client(keys)
        r = self._exchange(
            client, _code(client), headers=_basic(), redirect_uri="https://other.example/cb"
        )
        assert r.json()["error"] == "invalid_grant"

    def test_garbage_code(self, keys):
        r = self._exchange(_client(keys), "okshc_nope.nope", headers=_basic())
        assert r.json()["error"] == "invalid_grant"

    def test_pkce_required_when_challenged(self, keys):
        client = _client(keys)
        verifier, challenge = _pkce()
        assert self._exchange(client, _code(client, challenge), headers=_basic()).json()[
            "error"
        ] == "invalid_grant"
        assert self._exchange(
            client, _code(client, challenge), headers=_basic(), code_verifier="w" * 64
        ).json()["error"] == "invalid_grant"
        assert self._exchange(
            client, _code(client, challenge), headers=_basic(), code_verifier=verifier
        ).status_code == 200

    def test_invalid_key_is_invalid_client_naming_the_key(self, keys):
        client = _client(keys, _Validator("invalid"))
        r = self._exchange(client, _code(client), headers=_basic())
        assert r.status_code == 401
        assert r.json()["error"] == "invalid_client"
        assert "API key is not valid" in r.json()["error_description"]

    def test_okareo_unreachable_is_503(self, keys):
        client = _client(keys, _Validator("unavailable"))
        r = self._exchange(client, _code(client), headers=_basic())
        assert r.status_code == 503
        assert r.json()["error"] == "temporarily_unavailable"
        assert r.headers["retry-after"] == "5"

    def test_key_is_checked_without_the_cache(self, keys):
        validator = _Validator()
        client = _client(keys, validator)
        self._exchange(client, _code(client), headers=_basic())
        assert validator.calls == [(API_KEY, False)]

    def test_secret_with_reserved_characters_survives_basic_encoding(self, keys):
        # RFC 6749 §2.3.1: Basic credentials are form-urlencoded first.
        validator = _Validator()
        client = _client(keys, validator)
        self._exchange(client, _code(client), headers=_basic(secret="a:b+c/d"))
        assert validator.calls[0][0] == "a:b+c/d"


class TestTokenRefresh:
    def _tokens(self, keys):
        return issue_tokens(keys, API_KEY, "t-1")

    def _refresh(self, client, rt, headers=None):
        return client.post(
            "/oauth/shared/token",
            data={"grant_type": "refresh_token", "refresh_token": rt},
            headers=headers if headers is not None else _basic(),
        )

    def test_issues_a_new_pair(self, keys):
        rt = self._tokens(keys)["refresh_token"]
        r = self._refresh(_client(keys), rt)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["refresh_token"] != rt
        assert open_token(body["access_token"], keys, "at")["k"] == API_KEY
        assert r.headers["cache-control"] == "no-store"

    def test_secret_must_still_match_the_connection(self, keys):
        rt = self._tokens(keys)["refresh_token"]
        r = self._refresh(_client(keys), rt, headers=_basic(secret="a-different-key"))
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"
        assert "no longer matches" in r.json()["error_description"]

    def test_revoked_key(self, keys):
        rt = self._tokens(keys)["refresh_token"]
        r = self._refresh(_client(keys, _Validator("invalid")), rt)
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_okareo_unreachable_is_503(self, keys):
        rt = self._tokens(keys)["refresh_token"]
        r = self._refresh(_client(keys, _Validator("unavailable")), rt)
        assert r.status_code == 503

    def test_access_token_is_not_a_refresh_token(self, keys):
        at = self._tokens(keys)["access_token"]
        assert self._refresh(_client(keys), at).json()["error"] == "invalid_grant"

    def test_missing_client_auth(self, keys):
        rt = self._tokens(keys)["refresh_token"]
        assert self._refresh(_client(keys), rt, headers={}).status_code == 401


class TestTokenOther:
    def test_unsupported_grant_type(self, keys):
        r = _client(keys).post(
            "/oauth/shared/token", data={"grant_type": "client_credentials"}, headers=_basic()
        )
        assert r.status_code == 400
        assert r.json()["error"] == "unsupported_grant_type"

    def test_disabled_without_stable_signing_key(self):
        r = _client(None).post(
            "/oauth/shared/token", data={"grant_type": "authorization_code"}, headers=_basic()
        )
        assert r.status_code == 503
        assert r.json()["error"] == "temporarily_unavailable"


# ---------------------------------------------------------------------------
# Diagnostics (research R13)
# ---------------------------------------------------------------------------


def _diag_lines(capsys) -> list[str]:
    return [
        line for line in capsys.readouterr().err.splitlines()
        if line.startswith("[shared-oauth]")
    ]


def _fields(line: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in line.split()[1:])


class TestDiagnostics:
    """One line per request describing what the client sent, so the first
    real connection from a new tool shows exactly where it diverges."""

    def test_authorize_success(self, keys, capsys):
        verifier, challenge = _pkce()
        _authorize(_client(keys), code_challenge=challenge, code_challenge_method="S256")
        (line,) = _diag_lines(capsys)
        f = _fields(line)
        assert f["route"] == "authorize"
        assert f["status"] == "302"
        assert f["outcome"] == "ok"
        assert f["pkce"] == "yes"
        assert f["callback_host"] == "tools.customer.example"
        assert f["params"] == (
            "client_id,code_challenge,code_challenge_method,redirect_uri,response_type,state"
        )

    def test_token_success(self, keys, capsys):
        client = _client(keys)
        code = _code(client)
        capsys.readouterr()
        client.post(
            "/oauth/shared/token",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK},
            headers={**_basic(), "User-Agent": "Slackbot 1.0 (+https://api.slack.com/robots)"},
        )
        (line,) = _diag_lines(capsys)
        f = _fields(line)
        assert f["route"] == "token"
        assert f["status"] == "200"
        assert f["outcome"] == "ok"
        assert f["grant"] == "authorization_code"
        assert f["client_auth"] == "basic"
        assert f["pkce"] == "no"
        assert f["params"] == "code,grant_type,redirect_uri"
        assert f["tenant"] == "t-1"
        assert f["ua"].startswith("Slackbot_1.0_(+https://api.slack.com/robots)")

    @pytest.mark.parametrize(
        "case, reason, error",
        [
            ("both_auth", "client_auth", "invalid_client"),
            ("wrong_client", "wrong_client_id", "invalid_client"),
            ("no_redirect", "redirect_uri_missing", "invalid_grant"),
            ("other_redirect", "redirect_uri_mismatch", "invalid_grant"),
            ("bad_code", "bad_code", "invalid_grant"),
            ("no_verifier", "pkce_missing", "invalid_grant"),
            ("bad_verifier", "pkce_mismatch", "invalid_grant"),
            ("key_invalid", "key_invalid", "invalid_client"),
            ("key_unavailable", "key_unavailable", "temporarily_unavailable"),
            ("grant", "unsupported_grant_type", "unsupported_grant_type"),
        ],
    )
    def test_each_token_failure_names_its_reason(self, keys, capsys, case, reason, error):
        outcome = {"key_invalid": "invalid", "key_unavailable": "unavailable"}.get(case, "valid")
        client = _client(keys, _Validator(outcome))
        verifier, challenge = _pkce()
        code = _code(client, challenge if case in ("no_verifier", "bad_verifier") else None)
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK}
        headers = _basic()
        if case == "both_auth":
            data.update(client_id=CLIENT_ID, client_secret=API_KEY)
        elif case == "wrong_client":
            headers = _basic(client_id="someone-else")
        elif case == "no_redirect":
            del data["redirect_uri"]
        elif case == "other_redirect":
            data["redirect_uri"] = "https://other.example/cb"
        elif case == "bad_code":
            data["code"] = "okshc_x.y"
        elif case == "bad_verifier":
            data["code_verifier"] = "w" * 64
        elif case == "grant":
            data["grant_type"] = "password"
        capsys.readouterr()
        client.post("/oauth/shared/token", data=data, headers=headers)
        (line,) = _diag_lines(capsys)
        f = _fields(line)
        assert f["outcome"] == "error"
        assert f["reason"] == reason
        assert f["error"] == error

    @pytest.mark.parametrize(
        "case, reason, error",
        [
            ("rt", "bad_refresh_token", "invalid_grant"),
            ("secret", "secret_changed", "invalid_grant"),
            ("revoked", "key_invalid", "invalid_grant"),
        ],
    )
    def test_each_refresh_failure_names_its_reason(self, keys, capsys, case, reason, error):
        rt = issue_tokens(keys, API_KEY, "t-1")["refresh_token"]
        client = _client(keys, _Validator("invalid" if case == "revoked" else "valid"))
        client.post(
            "/oauth/shared/token",
            data={"grant_type": "refresh_token", "refresh_token": "junk" if case == "rt" else rt},
            headers=_basic(secret="other-key") if case == "secret" else _basic(),
        )
        (line,) = _diag_lines(capsys)
        f = _fields(line)
        assert (f["grant"], f["reason"], f["error"]) == ("refresh_token", reason, error)

    @pytest.mark.parametrize(
        "overrides, reason",
        [
            ({"redirect_uri": "http://insecure.example/cb"}, "bad_redirect_uri"),
            ({"client_id": "nope"}, "wrong_client_id"),
            ({"response_type": "token"}, "wrong_response_type"),
            ({"code_challenge": "x", "code_challenge_method": "plain"}, "bad_pkce_method"),
        ],
    )
    def test_each_authorize_failure_names_its_reason(self, keys, capsys, overrides, reason):
        _authorize(_client(keys), **overrides)
        (line,) = _diag_lines(capsys)
        assert _fields(line)["reason"] == reason

    def test_disabled_is_logged(self, capsys):
        _authorize(_client(None))
        (line,) = _diag_lines(capsys)
        assert _fields(line)["reason"] == "disabled"

    def test_no_secret_or_credential_value_is_logged(self, keys, capsys):
        client = _client(keys)
        verifier, challenge = _pkce()
        r = _authorize(
            client,
            redirect_uri="https://tools.customer.example/private/path?acct=secret-acct",
            state="state-value-xyz",
            code_challenge=challenge,
            code_challenge_method="S256",
        )
        code = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)["code"][0]
        tokens = client.post(
            "/oauth/shared/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://tools.customer.example/private/path?acct=secret-acct",
                "code_verifier": verifier,
            },
            headers=_basic(),
        ).json()
        client.post(
            "/oauth/shared/token",
            data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
            headers=_basic(),
        )
        out = capsys.readouterr()
        logged = out.err + out.out
        assert logged.count("[shared-oauth]") == 3
        for value in (
            API_KEY,
            API_KEY.rsplit(".", 1)[1][:20],
            code,
            tokens["access_token"],
            tokens["refresh_token"],
            "state-value-xyz",
            verifier,
            challenge,
            "private/path",
            "secret-acct",
        ):
            assert value not in logged, value

    def test_caller_cannot_forge_a_second_line(self, keys, capsys):
        client = _client(keys)
        client.get(
            "/oauth/shared/authorize",
            params={
                "response_type": "code",
                "client_id": CLIENT_ID,
                "redirect_uri": CALLBACK,
                "evil\n[shared-oauth] route=token outcome=ok": "1",
            },
            headers={"User-Agent": "x [shared-oauth] fake=1"},
        )
        (line,) = _diag_lines(capsys)
        assert "\n" not in line
        assert line.count("[shared-oauth]") == 1
        assert " fake=1" not in line


# ---------------------------------------------------------------------------
# Token URL carrying the key's claims (research R14)
# ---------------------------------------------------------------------------


class _KeyValidator:
    """Stub okareo-server that accepts exactly one full key."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.calls: list[str] = []

    async def __call__(self, key: str, *, use_cache: bool = True) -> KeyValidation:
        self.calls.append(key)
        if key == self.key:
            return KeyValidation(outcome="valid", tenant_id="t-compact", subject="u")
        return KeyValidation(outcome="invalid")


@pytest.fixture
def minted(rsa_keypair):
    import uuid

    import jwt as pyjwt

    from src.auth.compact_key import pack

    tenant = str(uuid.uuid4())
    key = pyjwt.encode(
        {
            "sub": str(uuid.uuid4()), "tenantId": tenant, "tenantIds": [tenant],
            "type": "apiKey", "roles": ["Admin"], "email": "svc+slack@customer.example",
            "name": "Slack / Service", "iss": "https://app.okareo.com", "aud": "okareo-cloud",
            "iat": 1791249491, "jti": str(uuid.uuid4()),
        },
        rsa_keypair["private"], algorithm="RS256", headers={"typ": "JWT"},
    )
    header, payload, signature = key.split(".")
    return {"key": key, "path": pack(f"{header}.{payload}"), "signature": signature}


def _basic_sig(signature: str) -> dict:
    return _basic(secret=signature)


class TestCompactTokenPath:
    def test_code_exchange_rebuilds_the_key(self, keys, minted, capsys):
        validator = _KeyValidator(minted["key"])
        client = _client(keys, validator)
        code = _code(client)
        r = client.post(
            f"/oauth/shared/token/{minted['path']}",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK},
            headers=_basic_sig(minted["signature"]),
        )
        assert r.status_code == 200, r.text
        assert validator.calls == [minted["key"]]
        # The envelope holds the whole key, so the bearer path needs nothing else.
        assert open_token(r.json()["access_token"], keys, "at")["k"] == minted["key"]
        line = _diag_lines(capsys)[-1]
        assert "key_source=path" in line and "outcome=ok" in line

    def test_post_body_credentials(self, keys, minted):
        client = _client(keys, _KeyValidator(minted["key"]))
        code = _code(client)
        r = client.post(
            f"/oauth/shared/token/{minted['path']}",
            data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
                "client_id": CLIENT_ID, "client_secret": minted["signature"],
            },
        )
        assert r.status_code == 200, r.text

    def test_refresh_through_the_same_url(self, keys, minted):
        client = _client(keys, _KeyValidator(minted["key"]))
        rt = issue_tokens(keys, minted["key"], "t-compact")["refresh_token"]
        r = client.post(
            f"/oauth/shared/token/{minted['path']}",
            data={"grant_type": "refresh_token", "refresh_token": rt},
            headers=_basic_sig(minted["signature"]),
        )
        assert r.status_code == 200, r.text

    def test_whole_key_as_secret_against_the_path_is_refused(self, keys, minted, capsys):
        client = _client(keys, _KeyValidator(minted["key"]))
        code = _code(client)
        r = client.post(
            f"/oauth/shared/token/{minted['path']}",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK},
            headers=_basic_sig(minted["key"]),
        )
        assert r.status_code == 401
        assert r.json()["error"] == "invalid_client"
        assert "setup page" in r.json()["error_description"]
        assert "reason=bad_claims_path" in _diag_lines(capsys)[-1]

    def test_wrong_signature_is_refused_by_okareo(self, keys, minted, capsys):
        client = _client(keys, _KeyValidator(minted["key"]))
        code = _code(client)
        r = client.post(
            f"/oauth/shared/token/{minted['path']}",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK},
            headers=_basic_sig("A" * 342),
        )
        assert r.status_code == 401
        assert "reason=key_invalid" in _diag_lines(capsys)[-1]

    def test_malformed_path(self, keys, minted, capsys):
        client = _client(keys, _KeyValidator(minted["key"]))
        code = _code(client)
        r = client.post(
            "/oauth/shared/token/v1/not/enough",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK},
            headers=_basic_sig(minted["signature"]),
        )
        assert r.status_code == 401
        assert "reason=bad_claims_path" in _diag_lines(capsys)[-1]

    def test_claims_are_not_logged(self, keys, minted, capsys):
        client = _client(keys, _KeyValidator(minted["key"]))
        code = _code(client)
        client.post(
            f"/oauth/shared/token/{minted['path']}",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK},
            headers=_basic_sig(minted["signature"]),
        )
        logged = capsys.readouterr().err
        for segment in minted["path"].split("/")[1:]:
            assert segment not in logged
        assert minted["signature"][:20] not in logged


class TestSetup:
    def _app(self):
        from src.auth.shared_setup import make_pack_route, make_setup_page_route

        return TestClient(Starlette(routes=[
            Route("/oauth/shared/setup", make_setup_page_route("https://tools.okareo.com"), methods=["GET"]),
            Route("/oauth/shared/setup/pack", make_pack_route("https://tools.okareo.com"), methods=["POST"]),
        ]))

    def test_page(self):
        r = self._app().get("/oauth/shared/setup")
        assert r.status_code == 200
        assert "okareo-shared-connection" in r.text
        assert "https://tools.okareo.com/oauth/shared/authorize" in r.text
        assert r.headers["cache-control"] == "no-store"
        assert "connect-src 'self'" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY"

    def test_pack_returns_the_token_url(self, minted):
        header, payload, _ = minted["key"].split(".")
        r = self._app().post("/oauth/shared/setup/pack", json={"claims": f"{header}.{payload}"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["token_url"] == "https://tools.okareo.com/oauth/shared/token/" + minted["path"]
        assert body["token_url_length"] == len(body["token_url"])
        assert "warning" not in body

    def test_pack_refuses_a_whole_key(self, minted):
        r = self._app().post("/oauth/shared/setup/pack", json={"claims": minted["key"]})
        assert r.status_code == 400
        assert "first two parts" in r.json()["error_description"]

    def test_pack_warns_about_long_urls(self, rsa_keypair):
        import uuid

        import jwt as pyjwt

        tenants = [str(uuid.uuid4()) for _ in range(5)]
        key = pyjwt.encode(
            {
                "sub": str(uuid.uuid4()), "tenantId": tenants[0], "tenantIds": tenants,
                "type": "apiKey", "roles": ["Admin"], "email": "a@b.example", "name": "A",
                "iss": "https://app.okareo.com", "aud": "okareo-cloud", "iat": 1, "jti": str(uuid.uuid4()),
            },
            rsa_keypair["private"], algorithm="RS256", headers={"typ": "JWT"},
        )
        header, payload, _ = key.split(".")
        r = self._app().post("/oauth/shared/setup/pack", json={"claims": f"{header}.{payload}"})
        assert r.status_code == 200
        assert "only to this one" in r.json()["warning"]

    @pytest.mark.parametrize("body", [b"not json", b"{}", b'{"claims": 1}'])
    def test_pack_bad_requests(self, body):
        r = self._app().post("/oauth/shared/setup/pack", content=body)
        assert r.status_code == 400
