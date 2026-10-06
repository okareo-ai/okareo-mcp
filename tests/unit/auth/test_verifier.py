"""Tests for src/auth/verifier.py (CombinedTokenVerifier)."""

from __future__ import annotations

import asyncio
import time

import pytest

from src.auth.api_key_verifier import KeyValidation
from src.auth.context import get_session_credential_optional
from src.auth.verifier import CredentialUnavailableError, InvalidAPIKeyError


@pytest.fixture
def make_verifier(rsa_keypair, jwks_doc, issuer_url, resource_server_url):
    """Returns a verifier wired against the in-process test JWKS."""

    def _factory(api_key_validator=None, jwks_get_key=None, shared_keys=None):
        from src.auth.jwks_cache import JWKSCache
        from src.auth.verifier import CombinedTokenVerifier

        async def _stub_get_key(kid: str):
            for k in jwks_doc["keys"]:
                if k["kid"] == kid:
                    return k
            return None

        jwks = JWKSCache(issuer_url)
        # patch the cache instance method so we don't make real network calls
        jwks.get_key = jwks_get_key or _stub_get_key  # type: ignore[method-assign]

        async def _default_api_key_validator(api_key: str):
            return KeyValidation(outcome="invalid")

        return CombinedTokenVerifier(
            issuer_url=issuer_url,
            resource_server_url=resource_server_url,
            jwks_cache=jwks,
            api_key_validator=api_key_validator or _default_api_key_validator,
            required_scope="okareo:use",
            shared_keys=shared_keys,
        )

    return _factory


class TestJWTPath:
    def test_valid_jwt_returns_access_token_and_sets_credential(
        self, make_verifier, jwt_signer, default_claims
    ):
        verifier = make_verifier()
        token = jwt_signer(default_claims)

        async def run():
            return await verifier.verify_token(token), get_session_credential_optional()

        access_token, credential = asyncio.run(run())
        assert access_token is not None
        assert access_token.client_id == "user-123"
        assert credential is not None
        assert credential.kind == "oauth"
        assert credential.org_id == "org-A"
        assert credential.subject == "user-123"

    def test_email_claim_propagates_to_credential(
        self, make_verifier, jwt_signer, default_claims
    ):
        default_claims["email"] = "dev@example.com"
        verifier = make_verifier()
        token = jwt_signer(default_claims)

        async def run():
            await verifier.verify_token(token)
            return get_session_credential_optional()

        credential = asyncio.run(run())
        assert credential is not None
        assert credential.email == "dev@example.com"

    def test_missing_email_claim_yields_none(
        self, make_verifier, jwt_signer, default_claims
    ):
        default_claims.pop("email", None)
        verifier = make_verifier()
        token = jwt_signer(default_claims)

        async def run():
            await verifier.verify_token(token)
            return get_session_credential_optional()

        credential = asyncio.run(run())
        assert credential is not None
        assert credential.email is None

    def test_wrong_aud_returns_none(self, make_verifier, jwt_signer, default_claims):
        default_claims["aud"] = "https://malicious.example"
        verifier = make_verifier()
        token = jwt_signer(default_claims)

        async def run():
            return await verifier.verify_token(token)

        assert asyncio.run(run()) is None

    def test_expired_jwt_returns_none(
        self, make_verifier, jwt_signer, default_claims
    ):
        default_claims["exp"] = int(time.time()) - 100
        default_claims["iat"] = int(time.time()) - 200
        verifier = make_verifier()
        token = jwt_signer(default_claims)

        async def run():
            return await verifier.verify_token(token)

        assert asyncio.run(run()) is None

    def test_missing_organization_id_returns_none(
        self, make_verifier, jwt_signer, default_claims
    ):
        default_claims.pop("organization_id")
        verifier = make_verifier()
        token = jwt_signer(default_claims)

        async def run():
            return await verifier.verify_token(token)

        assert asyncio.run(run()) is None

    def test_missing_required_scope_returns_none(
        self, make_verifier, jwt_signer, default_claims
    ):
        default_claims["scope"] = "some:other:scope"
        verifier = make_verifier()
        token = jwt_signer(default_claims)

        async def run():
            return await verifier.verify_token(token)

        assert asyncio.run(run()) is None

    def test_required_scope_empty_accepts_any_scope_set(
        self, rsa_keypair, jwks_doc, issuer_url, resource_server_url,
        jwt_signer, default_claims,
    ):
        """When required_scope is empty (the v1 default), the verifier
        accepts the JWT regardless of what scopes it carries — including
        no scope at all. Used so Frontegg's default token templates
        (which don't issue MCP-specific scopes) still pass."""
        from src.auth.jwks_cache import JWKSCache
        from src.auth.verifier import CombinedTokenVerifier

        async def _stub_get_key(kid: str):
            for k in jwks_doc["keys"]:
                if k["kid"] == kid:
                    return k
            return None

        jwks = JWKSCache(issuer_url)
        jwks.get_key = _stub_get_key  # type: ignore[method-assign]

        async def _validator(_: str):
            return KeyValidation(outcome="invalid")

        verifier = CombinedTokenVerifier(
            issuer_url=issuer_url,
            resource_server_url=resource_server_url,
            jwks_cache=jwks,
            api_key_validator=_validator,
            required_scope="",  # opt out of scope enforcement
        )
        # Token has no `scope` claim at all
        default_claims.pop("scope", None)
        token = jwt_signer(default_claims)

        async def run():
            return await verifier.verify_token(token)

        assert asyncio.run(run()) is not None

    def test_trailing_slash_in_aud_normalized(
        self, make_verifier, jwt_signer, default_claims
    ):
        # The PRM doc may render aud as "http://localhost:8080/" (trailing
        # slash) even when AuthSettings was given the no-slash form. The
        # verifier must accept either form on the inbound token.
        default_claims["aud"] = default_claims["aud"] + "/"
        verifier = make_verifier()
        token = jwt_signer(default_claims)

        async def run():
            return await verifier.verify_token(token)

        assert asyncio.run(run()) is not None


class TestAllowedTenantsFromJWT:
    """T053 / T060 — `tenantIds[]` claim is surfaced on `SessionCredential`
    for `switch_tenant`'s FR-025 fast-path validation. Absent claim is OK
    (the tools layer falls back to a Frontegg user-info call)."""

    def test_tenantIds_claim_populates_allowed_tenants(
        self, make_verifier, jwt_signer, default_claims
    ):
        claims = {**default_claims, "tenantIds": ["t-1", "t-2", "t-3"]}
        verifier = make_verifier()
        token = jwt_signer(claims)

        async def run():
            return await verifier.verify_token(token), get_session_credential_optional()

        access_token, credential = asyncio.run(run())
        assert access_token is not None
        assert credential is not None
        assert credential.allowed_tenants == ("t-1", "t-2", "t-3")

    def test_missing_tenantIds_claim_yields_empty_tuple(
        self, make_verifier, jwt_signer, default_claims
    ):
        verifier = make_verifier()
        token = jwt_signer(default_claims)  # no tenantIds

        async def run():
            return await verifier.verify_token(token), get_session_credential_optional()

        _, credential = asyncio.run(run())
        assert credential is not None
        assert credential.allowed_tenants == ()

    def test_malformed_tenantIds_claim_yields_empty_tuple(
        self, make_verifier, jwt_signer, default_claims
    ):
        """A non-list value for `tenantIds` is treated as absent — fall back
        to Frontegg user-info instead of crashing."""
        claims = {**default_claims, "tenantIds": "not-a-list"}
        verifier = make_verifier()
        token = jwt_signer(claims)

        async def run():
            return await verifier.verify_token(token), get_session_credential_optional()

        _, credential = asyncio.run(run())
        assert credential is not None
        assert credential.allowed_tenants == ()


def _key_jwt(jwt_signer, **claims) -> str:
    payload = {"type": "apiKey", "tenantId": "tenant-K", "sub": "creator-1"}
    payload.update(claims)
    return jwt_signer(payload)


async def _jwks_must_not_be_called(kid: str):
    raise AssertionError("an API key must never reach the Frontegg JWKS lookup")


class TestAPIKeyPath:
    @pytest.mark.parametrize("token_type", ["apiKey", "tenantAccessToken"])
    def test_key_jwt_routes_to_validator_never_jwks(
        self, make_verifier, jwt_signer, token_type
    ):
        """048 R1: okareo-server mints `type: apiKey` keys; Frontegg issued
        `tenantAccessToken` ones. Both are validated by okareo-server, and
        neither has a `kid` Frontegg's JWKS would know."""
        seen: dict[str, str] = {}

        async def _validator(api_key: str):
            seen["token"] = api_key
            return KeyValidation(
                outcome="valid", tenant_id="tenant-K", subject="creator-1"
            )

        verifier = make_verifier(
            api_key_validator=_validator, jwks_get_key=_jwks_must_not_be_called
        )
        token = _key_jwt(jwt_signer, type=token_type)

        async def run():
            return await verifier.verify_token(token), get_session_credential_optional()

        access_token, credential = asyncio.run(run())
        assert seen["token"] == token
        assert access_token is not None
        assert access_token.client_id == "tenant-K"
        assert credential is not None
        assert credential.kind == "api_key"
        assert credential.api_key == token
        assert credential.org_id == "tenant-K"
        assert credential.subject == "creator-1"

    def test_key_expiry_is_carried_to_the_access_token(self, make_verifier, jwt_signer):
        async def _validator(api_key: str):
            return KeyValidation(
                outcome="valid", tenant_id="tenant-K", subject="creator-1",
                expires_at=4102444800,
            )

        verifier = make_verifier(api_key_validator=_validator)

        async def run():
            return await verifier.verify_token(_key_jwt(jwt_signer)), get_session_credential_optional()

        access_token, credential = asyncio.run(run())
        assert access_token.expires_at == 4102444800
        assert credential.expires_at is not None
        assert int(credential.expires_at.timestamp()) == 4102444800

    def test_frontegg_user_token_still_takes_the_jwt_path(
        self, make_verifier, jwt_signer, default_claims
    ):
        async def _validator(api_key: str):
            raise AssertionError("a sign-in JWT must not reach the key validator")

        verifier = make_verifier(api_key_validator=_validator)
        default_claims["type"] = "userToken"

        async def run():
            return await verifier.verify_token(jwt_signer(default_claims)), get_session_credential_optional()

        access_token, credential = asyncio.run(run())
        assert access_token is not None
        assert credential.kind == "oauth"

    def test_invalid_key_raises_invalid_api_key_error(self, make_verifier, jwt_signer):
        async def _validator(api_key: str):
            return KeyValidation(outcome="invalid")

        verifier = make_verifier(api_key_validator=_validator)
        with pytest.raises(InvalidAPIKeyError) as exc:
            asyncio.run(verifier.verify_token(_key_jwt(jwt_signer)))
        assert "not valid" in exc.value.description

    def test_unavailable_raises_credential_unavailable_error(self, make_verifier, jwt_signer):
        async def _validator(api_key: str):
            return KeyValidation(outcome="unavailable")

        verifier = make_verifier(api_key_validator=_validator)
        with pytest.raises(CredentialUnavailableError):
            asyncio.run(verifier.verify_token(_key_jwt(jwt_signer)))

    def test_validator_that_raises_fails_closed_as_unavailable(
        self, make_verifier, jwt_signer
    ):
        """A bug in the validator must not let the request through, and must
        not tell the user their key is wrong."""
        async def _validator(api_key: str):
            raise RuntimeError("upstream blew up")

        verifier = make_verifier(api_key_validator=_validator)
        with pytest.raises(CredentialUnavailableError):
            asyncio.run(verifier.verify_token(_key_jwt(jwt_signer)))

    @pytest.mark.parametrize("token", ["okareo-OPAQUE-KEY", "sk-live-123"])
    def test_non_jwt_bearer_is_invalid_without_validation(self, make_verifier, token):
        """048 R4: okareo-server only accepts JWTs, so an opaque string can
        never be a valid key; no backend call is made for it."""
        async def _validator(api_key: str):
            raise AssertionError("an opaque string must not reach the validator")

        verifier = make_verifier(api_key_validator=_validator)
        with pytest.raises(InvalidAPIKeyError):
            asyncio.run(verifier.verify_token(token))


class TestNeverRaises:
    """The sign-in path keeps returning None so the SDK's own 401 is
    unchanged (FR-015). Only key-shaped bearers raise."""

    def test_malformed_jwt_shaped_token_returns_none_does_not_raise(self, make_verifier):
        verifier = make_verifier()

        async def run():
            return await verifier.verify_token("garbage.notajwt.atall")

        assert asyncio.run(run()) is None

    def test_four_segment_string_is_refused_as_a_key(self, make_verifier):
        """Not JWT-shaped, so it can only have been meant as an API key."""
        verifier = make_verifier()
        with pytest.raises(InvalidAPIKeyError):
            asyncio.run(verifier.verify_token("garbage.not.a.jwt"))

    def test_bad_signature_sign_in_jwt_returns_none(
        self, make_verifier, rsa_keypair, default_claims
    ):
        import jwt as pyjwt
        from cryptography.hazmat.primitives.asymmetric import rsa

        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = pyjwt.encode(
            default_claims, other, algorithm="RS256", headers={"kid": "test-key-1"}
        )
        verifier = make_verifier()
        assert asyncio.run(verifier.verify_token(token)) is None


class TestSharedConnectionPath:
    """048 US2: `okmcp_at_` tokens carry an API key encrypted by this server
    (contracts/bearer-verification.md)."""

    SIGNING_KEY = "a-stable-dcr-signing-key-of-at-least-32-bytes"
    INNER_KEY = "eyJhbGciOiJIUzI1NiJ9.eyJ0eXBlIjoiYXBpS2V5In0.c2lnbmF0dXJl"

    @pytest.fixture
    def keys(self):
        from src.auth.shared_connection import SharedConnectionKeys

        return SharedConnectionKeys.from_signing_key(self.SIGNING_KEY)

    def _access_token(self, keys, **kw):
        from src.auth.shared_connection import issue_tokens

        return issue_tokens(keys, self.INNER_KEY, "tenant-S", **kw)["access_token"]

    def _validator(self, outcome="valid", seen=None):
        async def _v(api_key: str):
            if seen is not None:
                seen.append(api_key)
            if outcome == "valid":
                return KeyValidation(outcome="valid", tenant_id="tenant-S", subject="creator-S")
            return KeyValidation(outcome=outcome)

        return _v

    def test_valid_envelope_sets_shared_session_with_inner_key(self, make_verifier, keys):
        seen: list[str] = []
        verifier = make_verifier(
            api_key_validator=self._validator(seen=seen),
            jwks_get_key=_jwks_must_not_be_called,
            shared_keys=keys,
        )
        token = self._access_token(keys)

        async def run():
            return await verifier.verify_token(token), get_session_credential_optional()

        access_token, credential = asyncio.run(run())
        assert seen == [self.INNER_KEY]
        assert access_token.token == token
        assert access_token.client_id == "tenant-S"
        assert credential.kind == "shared_api_key"
        assert credential.api_key == self.INNER_KEY
        assert credential.org_id == "tenant-S"

    @pytest.mark.parametrize("variant", ["garbage", "expired", "refresh", "other-key"])
    def test_unreadable_envelope_asks_to_reconnect(self, make_verifier, keys, variant):
        from src.auth.shared_connection import (
            ACCESS_TTL,
            SharedConnectionKeys,
            issue_tokens,
        )

        if variant == "garbage":
            token = "okmcp_at_not-a-real-token"
        elif variant == "expired":
            token = self._access_token(keys, now=int(time.time()) - ACCESS_TTL - 10)
        elif variant == "refresh":
            token = issue_tokens(keys, self.INNER_KEY, "tenant-S")["refresh_token"]
            token = "okmcp_at_" + token[len("okmcp_rt_"):]
        else:
            other = SharedConnectionKeys.from_signing_key("another-signing-key-32-bytes-long!!")
            token = self._access_token(other)

        verifier = make_verifier(
            api_key_validator=self._validator(), shared_keys=keys
        )
        with pytest.raises(InvalidAPIKeyError) as exc:
            asyncio.run(verifier.verify_token(token))
        assert "reconnect" in exc.value.description

    def test_without_shared_keys_envelope_is_refused(self, make_verifier, keys):
        verifier = make_verifier(api_key_validator=self._validator(), shared_keys=None)
        with pytest.raises(InvalidAPIKeyError) as exc:
            asyncio.run(verifier.verify_token(self._access_token(keys)))
        assert "reconnect" in exc.value.description

    def test_revoked_inner_key_names_the_key(self, make_verifier, keys):
        verifier = make_verifier(api_key_validator=self._validator("invalid"), shared_keys=keys)
        with pytest.raises(InvalidAPIKeyError) as exc:
            asyncio.run(verifier.verify_token(self._access_token(keys)))
        assert "API key is not valid" in exc.value.description

    def test_okareo_unreachable(self, make_verifier, keys):
        verifier = make_verifier(api_key_validator=self._validator("unavailable"), shared_keys=keys)
        with pytest.raises(CredentialUnavailableError):
            asyncio.run(verifier.verify_token(self._access_token(keys)))
