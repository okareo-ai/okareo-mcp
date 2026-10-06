"""Tests for src/auth/compact_key.py (048 research R14).

Keys are signed here exactly the way okareo-server's
``create_okareo_api_key`` + ``sign_jwt`` build them: the same claim order,
``exp`` last when set, PyJWT with ``headers={"typ": "JWT"}``. A real key is
never committed.
"""

from __future__ import annotations

import uuid

import jwt as pyjwt
import pytest

from src.auth.compact_key import CompactKeyError, pack, unpack


def _mint(rsa_keypair, **overrides) -> str:
    tenant = overrides.pop("tenantId", str(uuid.uuid4()))
    claims = {
        "sub": str(uuid.uuid4()),
        "tenantId": tenant,
        "tenantIds": [tenant],
        "type": "apiKey",
        "roles": ["Admin"],
        "email": "svc+slack@customer.example",
        "name": "Slack Service",
        "iss": "https://app.okareo.com",
        "aud": "okareo-cloud",
        "iat": 1791249491,
        "jti": str(uuid.uuid4()),
    }
    exp = overrides.pop("exp", None)
    claims.update(overrides)
    if exp is not None:
        claims["exp"] = exp
    return pyjwt.encode(claims, rsa_keypair["private"], algorithm="RS256", headers={"typ": "JWT"})


def _split(key: str) -> tuple[str, str]:
    header, payload, signature = key.split(".")
    return f"{header}.{payload}", signature


def _round_trip(key: str) -> str:
    claims, signature = _split(key)
    path = pack(claims)
    assert unpack(path, signature) == key
    return path


class TestRoundTrip:
    def test_typical_one_org_admin_key_is_short(self, rsa_keypair):
        path = _round_trip(_mint(rsa_keypair))
        assert path.startswith("v1/")
        assert path.count("/") == 6  # no optional segments
        assert len("https://tools.okareo.com/oauth/shared/token/" + path) < 200

    @pytest.mark.parametrize(
        "overrides, segment",
        [
            ({"roles": ["Member"]}, "r="),
            ({"roles": ["Admin", "Read Only"]}, "r="),
            ({"iss": "https://dev.okareo.com"}, "i="),
            ({"aud": "okareo-standalone"}, "a="),
            ({"exp": 1893456000}, "x="),
        ],
    )
    def test_differences_from_the_defaults_travel_as_segments(self, rsa_keypair, overrides, segment):
        assert f"/{segment}" in _round_trip(_mint(rsa_keypair, **overrides))

    def test_several_organizations(self, rsa_keypair):
        tenant = str(uuid.uuid4())
        key = _mint(rsa_keypair, tenantId=tenant, tenantIds=[str(uuid.uuid4()), tenant])
        assert "/t=" in _round_trip(key)

    @pytest.mark.parametrize(
        "email, name",
        [
            (None, None),
            ("a/b%c@x.example", "Ünïcødé 名前 😀"),
            ("plain@x.example", "Slash / Percent % Bang ! Tilde ~"),
            ("", ""),
        ],
    )
    def test_awkward_email_and_name(self, rsa_keypair, email, name):
        _round_trip(_mint(rsa_keypair, email=email, name=name))

    def test_non_uuid_sub(self, rsa_keypair):
        # okareo-server falls back to "unknown" when the session has no sub.
        path = _round_trip(_mint(rsa_keypair, sub="unknown"))
        assert path.split("/")[1] == "~unknown"

    def test_uppercase_uuid_is_kept_verbatim(self, rsa_keypair):
        _round_trip(_mint(rsa_keypair, jti=str(uuid.uuid4()).upper()))

    def test_no_value_from_the_signature_is_in_the_path(self, rsa_keypair):
        key = _mint(rsa_keypair)
        claims, signature = _split(key)
        path = pack(claims)
        assert signature not in path
        assert signature[:20] not in path


class TestPackRefuses:
    def test_a_whole_key(self, rsa_keypair):
        with pytest.raises(CompactKeyError, match="only the first two parts"):
            pack(_mint(rsa_keypair))

    def test_a_different_header(self, rsa_keypair):
        key = pyjwt.encode(
            {"sub": "x", "type": "apiKey"}, rsa_keypair["private"], algorithm="RS256",
            headers={"kid": "k1"},
        )
        with pytest.raises(CompactKeyError, match="header"):
            pack(_split(key)[0])

    def test_a_frontegg_key(self, rsa_keypair):
        with pytest.raises(CompactKeyError, match="type is not apiKey"):
            pack(_split(_mint(rsa_keypair, type="tenantAccessToken"))[0])

    def test_an_unknown_claim_layout(self, rsa_keypair):
        with pytest.raises(CompactKeyError, match="layout"):
            pack(_split(_mint(rsa_keypair, newClaim=1))[0])

    @pytest.mark.parametrize("text", ["", "a", "a.b.c.d", "!!!.???"])
    def test_garbage(self, text):
        with pytest.raises(CompactKeyError):
            pack(text)


class TestUnpackRefuses:
    @pytest.mark.parametrize(
        "path",
        [
            "",
            "v2/a/b/c/1/e/n",
            "v1/a/b/c/1/e",
            "v1/AAAAAAAAAAAAAAAAAAAAAA/AAAAAAAAAAAAAAAAAAAAAA/AAAAAAAAAAAAAAAAAAAAAA/notanumber/e/n",
            "v1/short/AAAAAAAAAAAAAAAAAAAAAA/AAAAAAAAAAAAAAAAAAAAAA/1/e/n",
            "v1/AAAAAAAAAAAAAAAAAAAAAA/AAAAAAAAAAAAAAAAAAAAAA/AAAAAAAAAAAAAAAAAAAAAA/1/e/n/z=1",
            "v1/AAAAAAAAAAAAAAAAAAAAAA/AAAAAAAAAAAAAAAAAAAAAA/AAAAAAAAAAAAAAAAAAAAAA/1/e/n/r=a/r=b",
            "v1/AAAAAAAAAAAAAAAAAAAAAA/AAAAAAAAAAAAAAAAAAAAAA/AAAAAAAAAAAAAAAAAAAAAA/1/e/n/x=soon",
        ],
    )
    def test_malformed_paths(self, path):
        with pytest.raises(CompactKeyError):
            unpack(path, "sig")

    @pytest.mark.parametrize("secret", ["", "a.b"])
    def test_secret_that_is_not_a_signature(self, rsa_keypair, secret):
        path = pack(_split(_mint(rsa_keypair))[0])
        with pytest.raises(CompactKeyError):
            unpack(path, secret)

    def test_tampered_path_rebuilds_a_different_key(self, rsa_keypair):
        """unpack only checks shape; okareo-server's signature check is what
        refuses a key whose claims were altered."""
        key = _mint(rsa_keypair)
        claims, signature = _split(key)
        path = pack(claims).replace("Slack%20Service", "Someone%20Else")
        rebuilt = unpack(path, signature)
        assert rebuilt != key
        with pytest.raises(pyjwt.InvalidSignatureError):
            pyjwt.decode(
                rebuilt, rsa_keypair["public"], algorithms=["RS256"], audience="okareo-cloud"
            )
