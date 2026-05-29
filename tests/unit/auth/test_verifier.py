"""Tests for src/auth/verifier.py (CombinedTokenVerifier)."""

from __future__ import annotations

import asyncio
import time

import pytest

from src.auth.context import SessionCredential, get_session_credential_optional


@pytest.fixture
def make_verifier(rsa_keypair, jwks_doc, issuer_url, resource_server_url):
    """Returns a verifier wired against the in-process test JWKS."""

    def _factory(api_key_resolver=None):
        from src.auth.jwks_cache import JWKSCache
        from src.auth.verifier import CombinedTokenVerifier

        async def _stub_get_key(kid: str):
            for k in jwks_doc["keys"]:
                if k["kid"] == kid:
                    return k
            return None

        jwks = JWKSCache(issuer_url)
        # patch the cache instance method so we don't make real network calls
        jwks.get_key = _stub_get_key  # type: ignore[method-assign]

        async def _default_api_key_resolver(api_key: str):
            return None  # by default no fallback path

        return CombinedTokenVerifier(
            issuer_url=issuer_url,
            resource_server_url=resource_server_url,
            jwks_cache=jwks,
            api_key_resolver=api_key_resolver or _default_api_key_resolver,
            required_scope="okareo:use",
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

        async def _resolver(_: str):
            return None

        verifier = CombinedTokenVerifier(
            issuer_url=issuer_url,
            resource_server_url=resource_server_url,
            jwks_cache=jwks,
            api_key_resolver=_resolver,
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


class TestAPIKeyPath:
    def test_non_jwt_bearer_falls_through_to_api_key(self, make_verifier):
        async def _resolver(api_key: str):
            if api_key == "okareo-VALIDKEY":
                return SessionCredential(
                    kind="api_key", api_key=api_key, org_id="org-from-api-key"
                )
            return None

        verifier = make_verifier(api_key_resolver=_resolver)

        async def run():
            return await verifier.verify_token("okareo-VALIDKEY"), get_session_credential_optional()

        access_token, credential = asyncio.run(run())
        assert access_token is not None
        assert credential is not None
        assert credential.kind == "api_key"
        assert credential.org_id == "org-from-api-key"

    def test_invalid_api_key_returns_none(self, make_verifier):
        async def _resolver(api_key: str):
            return None

        verifier = make_verifier(api_key_resolver=_resolver)

        async def run():
            return await verifier.verify_token("okareo-WRONGKEY")

        assert asyncio.run(run()) is None

    def test_tenant_access_token_routes_to_api_key_path(
        self, make_verifier, jwt_signer
    ):
        """Frontegg `tenantAccessToken` JWTs (Okareo's long-lived API-key
        delivery format) have no `exp` claim, so they must NOT be routed
        through `_verify_jwt`. Detect by `type` claim and hand off to the
        API-key resolver — the Okareo backend is the source of truth on
        whether the key is valid.
        """
        seen: dict[str, str] = {}

        async def _resolver(api_key: str):
            seen["token"] = api_key
            return SessionCredential(
                kind="api_key", api_key=api_key, org_id="org-from-tat"
            )

        verifier = make_verifier(api_key_resolver=_resolver)
        token = jwt_signer(
            {
                "sub": "user-x",
                "type": "tenantAccessToken",
                "tenantId": "t-123",
                "iss": "https://auth.okareo.com",
                "aud": "vendor-id",
                "iat": 0,
            }
        )

        async def run():
            return await verifier.verify_token(token), get_session_credential_optional()

        access_token, credential = asyncio.run(run())
        assert access_token is not None, "tenant access token must NOT be rejected"
        assert credential is not None
        assert credential.kind == "api_key"
        assert credential.org_id == "org-from-tat"
        assert seen["token"] == token, "resolver should receive the original token verbatim"


class TestNeverRaises:
    def test_malformed_token_returns_none_does_not_raise(self, make_verifier):
        verifier = make_verifier()

        async def run():
            return await verifier.verify_token("garbage.not.a.jwt")

        assert asyncio.run(run()) is None

    def test_resolver_raises_returns_none(self, make_verifier):
        async def _resolver(api_key: str):
            raise RuntimeError("upstream blew up")

        verifier = make_verifier(api_key_resolver=_resolver)

        async def run():
            return await verifier.verify_token("okareo-VALIDKEY")

        # Verifier must swallow upstream errors so the SDK can return a clean
        # 401 — never a 500.
        assert asyncio.run(run()) is None
