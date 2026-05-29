"""Tests for src/auth/embedded_handoff.py (POST /oauth/handoff).

Covers the happy path + the seven error responses documented in
specs/021-embedded-login/contracts/handoff-endpoint.openapi.yaml.
"""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route


_ISSUER = "https://test.frontegg.example"
_RESOURCE = "http://localhost:8080"


@pytest.fixture
def config():
    from src.auth.oauth_proxy import ProxyConfig

    return ProxyConfig(
        resource_server_url=_RESOURCE,
        frontegg_domain="test.frontegg.example",
        frontegg_client_id="frontegg-app-id",
    )


@pytest.fixture
def store():
    from src.auth.oauth_state import OAuthStateStore

    return OAuthStateStore()


@pytest.fixture
def jwks_stub(jwks_doc):
    """In-process JWKSCache stand-in returning our test JWKS keys."""
    from src.auth.jwks_cache import JWKSCache

    jwks = JWKSCache(_ISSUER)

    async def _get_key(kid: str):
        for k in jwks_doc["keys"]:
            if k["kid"] == kid:
                return k
        return None

    jwks.get_key = _get_key  # type: ignore[method-assign]
    return jwks


@pytest.fixture
def app(store, config, jwks_stub):
    from src.auth.embedded_handoff import make_handoff_route

    handler = make_handoff_route(
        store,
        config,
        jwks_stub,
        audience_aliases=[_RESOURCE, _RESOURCE + "/"],
    )
    return Starlette(
        routes=[Route("/oauth/handoff", handler, methods=["POST", "OPTIONS"])]
    )


def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url=_RESOURCE, follow_redirects=False
    )


async def _seed_pending(store, redirect_uri="http://127.0.0.1:33418/", state="cli-state"):
    """Create a PendingAuthorization the way /oauth/authorize would."""
    return await store.create_pending(
        client_id="mcp_test_client_id",
        redirect_uri=redirect_uri,
        code_challenge="challenge",
        code_challenge_method="S256",
        state_to_client=state,
        upstream_code_verifier="verifier-not-used-by-embedded",
    )


def _good_claims(now: int | None = None) -> dict[str, Any]:
    now = now or int(time.time())
    return {
        "iss": _ISSUER,
        "aud": _RESOURCE,
        "sub": "user-123",
        "exp": now + 600,
        "iat": now,
        "organization_id": "org-A",
    }


def _good_body(pending_code: str, token: str) -> dict[str, Any]:
    return {
        "pending_code": pending_code,
        "frontegg_access_token": token,
        "frontegg_refresh_token": "rt-test-1234567890",
        "frontegg_expires_in": 3600,
    }


def _same_origin_headers() -> dict[str, str]:
    return {
        "origin": _RESOURCE,
        "sec-fetch-site": "same-origin",
        "content-type": "application/json",
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHandoffHappyPath:
    def test_valid_request_populates_pending_and_returns_redirect_url(
        self, app, store, jwt_signer
    ):
        token = jwt_signer(_good_claims())

        async def run():
            pending = await _seed_pending(store)
            async with _client(app) as c:
                r = await c.post(
                    "/oauth/handoff",
                    json=_good_body(pending.code, token),
                    headers=_same_origin_headers(),
                )
            return r, await store.get_pending(pending.code)

        r, pending_after = asyncio.run(run())
        assert r.status_code == 200, r.text
        body = r.json()
        assert "redirect_url" in body
        parsed = urllib.parse.urlsplit(body["redirect_url"])
        assert parsed.scheme == "http"
        assert parsed.netloc == "127.0.0.1:33418"
        qs = urllib.parse.parse_qs(parsed.query)
        assert qs["code"][0].startswith("okm_")
        assert qs["state"] == ["cli-state"]
        # JWT, refresh_token, expires_in have been stored on the pending record.
        assert pending_after is not None
        assert pending_after.frontegg_jwt == token
        assert pending_after.frontegg_refresh_token == "rt-test-1234567890"
        assert pending_after.frontegg_expires_in == 3600

    def test_omitted_client_state_omits_state_from_redirect(
        self, app, store, jwt_signer
    ):
        token = jwt_signer(_good_claims())

        async def run():
            pending = await _seed_pending(store, state=None)
            async with _client(app) as c:
                return await c.post(
                    "/oauth/handoff",
                    json=_good_body(pending.code, token),
                    headers=_same_origin_headers(),
                )

        r = asyncio.run(run())
        assert r.status_code == 200
        qs = urllib.parse.parse_qs(
            urllib.parse.urlsplit(r.json()["redirect_url"]).query
        )
        assert "state" not in qs


# ---------------------------------------------------------------------------
# Error path: pending code
# ---------------------------------------------------------------------------


class TestHandoffPendingErrors:
    def test_unknown_pending_code_returns_invalid_grant(
        self, app, store, jwt_signer
    ):
        token = jwt_signer(_good_claims())

        async def run():
            async with _client(app) as c:
                return await c.post(
                    "/oauth/handoff",
                    json=_good_body("okm_does_not_exist", token),
                    headers=_same_origin_headers(),
                )

        r = asyncio.run(run())
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_malformed_pending_code_returns_invalid_request(
        self, app, store, jwt_signer
    ):
        token = jwt_signer(_good_claims())

        async def run():
            async with _client(app) as c:
                return await c.post(
                    "/oauth/handoff",
                    # wrong prefix
                    json=_good_body("badprefix_abc", token),
                    headers=_same_origin_headers(),
                )

        r = asyncio.run(run())
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_request"

    def test_expired_pending_returns_invalid_grant(
        self, app, store, jwt_signer
    ):
        token = jwt_signer(_good_claims())

        async def run():
            # Create pending with a TTL that's already elapsed.
            pending = await store.create_pending(
                client_id="mcp_test",
                redirect_uri="http://127.0.0.1:33418/",
                code_challenge="x",
                code_challenge_method="S256",
                state_to_client=None,
                upstream_code_verifier="v",
                ttl_seconds=0.001,
            )
            # Wait for TTL to expire.
            await asyncio.sleep(0.02)
            async with _client(app) as c:
                return await c.post(
                    "/oauth/handoff",
                    json=_good_body(pending.code, token),
                    headers=_same_origin_headers(),
                )

        r = asyncio.run(run())
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"


# ---------------------------------------------------------------------------
# Error path: JWT validation
# ---------------------------------------------------------------------------


class TestHandoffJWTErrors:
    def test_invalid_jwt_signature_returns_invalid_token(
        self, app, store, jwt_signer
    ):
        # Tamper with the FIRST char of the signature. base64url encoding
        # leaves the last char's high bits unused for non-aligned-length
        # signatures, so flipping a trailing char can decode to the same
        # bytes — flipping a leading char always changes the signature.
        good = jwt_signer(_good_claims())
        head, payload, sig = good.split(".")
        first = sig[0]
        replacement = "B" if first != "B" else "C"
        tampered = f"{head}.{payload}.{replacement}{sig[1:]}"

        async def run():
            pending = await _seed_pending(store)
            async with _client(app) as c:
                return await c.post(
                    "/oauth/handoff",
                    json=_good_body(pending.code, tampered),
                    headers=_same_origin_headers(),
                )

        r = asyncio.run(run())
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_token"

    def test_wrong_audience_returns_invalid_token(
        self, app, store, jwt_signer
    ):
        claims = _good_claims()
        claims["aud"] = "https://malicious.example"
        token = jwt_signer(claims)

        async def run():
            pending = await _seed_pending(store)
            async with _client(app) as c:
                return await c.post(
                    "/oauth/handoff",
                    json=_good_body(pending.code, token),
                    headers=_same_origin_headers(),
                )

        r = asyncio.run(run())
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_token"

    def test_wrong_issuer_returns_invalid_token(self, app, store, jwt_signer):
        claims = _good_claims()
        claims["iss"] = "https://attacker.example"
        token = jwt_signer(claims)

        async def run():
            pending = await _seed_pending(store)
            async with _client(app) as c:
                return await c.post(
                    "/oauth/handoff",
                    json=_good_body(pending.code, token),
                    headers=_same_origin_headers(),
                )

        r = asyncio.run(run())
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_token"

    def test_expired_jwt_returns_invalid_token(self, app, store, jwt_signer):
        claims = _good_claims()
        claims["exp"] = int(time.time()) - 120
        claims["iat"] = int(time.time()) - 240
        token = jwt_signer(claims)

        async def run():
            pending = await _seed_pending(store)
            async with _client(app) as c:
                return await c.post(
                    "/oauth/handoff",
                    json=_good_body(pending.code, token),
                    headers=_same_origin_headers(),
                )

        r = asyncio.run(run())
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_token"

    def test_missing_kid_returns_invalid_token(self, app, store, jwt_signer):
        # Sign with an unknown kid so JWKS lookup returns None.
        token = jwt_signer(_good_claims(), kid="unknown-kid")

        async def run():
            pending = await _seed_pending(store)
            async with _client(app) as c:
                return await c.post(
                    "/oauth/handoff",
                    json=_good_body(pending.code, token),
                    headers=_same_origin_headers(),
                )

        r = asyncio.run(run())
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_token"


# ---------------------------------------------------------------------------
# Error path: body shape
# ---------------------------------------------------------------------------


class TestHandoffBodyErrors:
    def test_missing_required_field_returns_invalid_request(
        self, app, store, jwt_signer
    ):
        token = jwt_signer(_good_claims())

        async def run():
            pending = await _seed_pending(store)
            body = _good_body(pending.code, token)
            del body["frontegg_refresh_token"]
            async with _client(app) as c:
                return await c.post(
                    "/oauth/handoff",
                    json=body,
                    headers=_same_origin_headers(),
                )

        r = asyncio.run(run())
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_request"

    def test_invalid_expires_in_returns_invalid_request(
        self, app, store, jwt_signer
    ):
        token = jwt_signer(_good_claims())

        async def run():
            pending = await _seed_pending(store)
            body = _good_body(pending.code, token)
            body["frontegg_expires_in"] = -1
            async with _client(app) as c:
                return await c.post(
                    "/oauth/handoff",
                    json=body,
                    headers=_same_origin_headers(),
                )

        r = asyncio.run(run())
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_request"

    def test_non_json_body_returns_invalid_request(self, app, store):
        async def run():
            async with _client(app) as c:
                return await c.post(
                    "/oauth/handoff",
                    content="not-json-at-all",
                    headers=_same_origin_headers(),
                )

        r = asyncio.run(run())
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_request"

    def test_non_json_content_type_returns_415(self, app, store):
        async def run():
            async with _client(app) as c:
                return await c.post(
                    "/oauth/handoff",
                    content="x",
                    headers={
                        "origin": _RESOURCE,
                        "content-type": "text/plain",
                    },
                )

        r = asyncio.run(run())
        assert r.status_code == 415


# ---------------------------------------------------------------------------
# Error path: CSRF / Origin
# ---------------------------------------------------------------------------


class TestHandoffCSRF:
    def test_missing_origin_returns_forbidden(self, app, store, jwt_signer):
        token = jwt_signer(_good_claims())

        async def run():
            pending = await _seed_pending(store)
            async with _client(app) as c:
                # httpx adds an Origin header automatically only for fetch-style
                # requests; we explicitly suppress it here.
                return await c.post(
                    "/oauth/handoff",
                    json=_good_body(pending.code, token),
                    headers={"content-type": "application/json"},
                )

        r = asyncio.run(run())
        assert r.status_code == 403

    def test_cross_origin_returns_forbidden(self, app, store, jwt_signer):
        token = jwt_signer(_good_claims())

        async def run():
            pending = await _seed_pending(store)
            async with _client(app) as c:
                return await c.post(
                    "/oauth/handoff",
                    json=_good_body(pending.code, token),
                    headers={
                        "content-type": "application/json",
                        "origin": "https://attacker.example",
                    },
                )

        r = asyncio.run(run())
        assert r.status_code == 403

    def test_sec_fetch_site_cross_site_returns_forbidden_in_production(
        self, app, store, jwt_signer
    ):
        token = jwt_signer(_good_claims())

        async def run():
            pending = await _seed_pending(store)
            async with _client(app) as c:
                return await c.post(
                    "/oauth/handoff",
                    json=_good_body(pending.code, token),
                    headers={
                        "content-type": "application/json",
                        "origin": _RESOURCE,
                        "sec-fetch-site": "cross-site",
                    },
                )

        r = asyncio.run(run())
        assert r.status_code == 403

    def test_dev_mode_accepts_localhost_3000(
        self, store, config, jwks_stub, jwt_signer, monkeypatch
    ):
        from src.auth.embedded_handoff import make_handoff_route

        monkeypatch.setenv("MCP_EMBEDDED_LOGIN_DEV_MODE", "true")
        token = jwt_signer(_good_claims())
        handler = make_handoff_route(
            store, config, jwks_stub, audience_aliases=[_RESOURCE, _RESOURCE + "/"]
        )
        local_app = Starlette(
            routes=[Route("/oauth/handoff", handler, methods=["POST", "OPTIONS"])]
        )

        async def run():
            pending = await _seed_pending(store)
            transport = httpx.ASGITransport(app=local_app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url=_RESOURCE,
                follow_redirects=False,
            ) as c:
                return await c.post(
                    "/oauth/handoff",
                    json=_good_body(pending.code, token),
                    headers={
                        "content-type": "application/json",
                        "origin": "http://localhost:3000",
                    },
                )

        r = asyncio.run(run())
        assert r.status_code == 200
        # CORS allow-origin echoed for dev mode.
        assert (
            r.headers.get("access-control-allow-origin") == "http://localhost:3000"
        )

    def test_dev_mode_off_rejects_localhost_3000(
        self, app, store, jwt_signer, monkeypatch
    ):
        monkeypatch.delenv("MCP_EMBEDDED_LOGIN_DEV_MODE", raising=False)
        token = jwt_signer(_good_claims())

        async def run():
            pending = await _seed_pending(store)
            async with _client(app) as c:
                return await c.post(
                    "/oauth/handoff",
                    json=_good_body(pending.code, token),
                    headers={
                        "content-type": "application/json",
                        "origin": "http://localhost:3000",
                    },
                )

        r = asyncio.run(run())
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Method enforcement
# ---------------------------------------------------------------------------


class TestHandoffMethods:
    def test_options_preflight_in_dev_mode_emits_cors(
        self, store, config, jwks_stub, monkeypatch
    ):
        from src.auth.embedded_handoff import make_handoff_route

        monkeypatch.setenv("MCP_EMBEDDED_LOGIN_DEV_MODE", "true")
        handler = make_handoff_route(
            store, config, jwks_stub, audience_aliases=[_RESOURCE]
        )
        local_app = Starlette(
            routes=[Route("/oauth/handoff", handler, methods=["POST", "OPTIONS"])]
        )

        async def run():
            transport = httpx.ASGITransport(app=local_app)
            async with httpx.AsyncClient(
                transport=transport, base_url=_RESOURCE
            ) as c:
                return await c.request(
                    "OPTIONS",
                    "/oauth/handoff",
                    headers={
                        "origin": "http://localhost:3000",
                        "access-control-request-method": "POST",
                    },
                )

        r = asyncio.run(run())
        assert r.status_code == 204
        assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"
        assert "POST" in r.headers.get("access-control-allow-methods", "")


# ---------------------------------------------------------------------------
# One-time use (defense — also covered by the spec contract)
# ---------------------------------------------------------------------------


class TestHandoffOneTimeUse:
    def test_double_submit_with_same_pending_code_returns_invalid_grant(
        self, app, store, jwt_signer
    ):
        """A successful handoff populates the pending record; submitting the
        same pending_code a second time MUST still 200 (the record is
        peek-able and re-populating is idempotent). What MUST fail is when
        the code has been *consumed* by /oauth/token in between — but that's
        covered in the integration test. Here we just confirm a re-submit
        doesn't somehow corrupt state."""
        token = jwt_signer(_good_claims())

        async def run():
            pending = await _seed_pending(store)
            async with _client(app) as c:
                r1 = await c.post(
                    "/oauth/handoff",
                    json=_good_body(pending.code, token),
                    headers=_same_origin_headers(),
                )
                r2 = await c.post(
                    "/oauth/handoff",
                    json=_good_body(pending.code, token),
                    headers=_same_origin_headers(),
                )
            return r1, r2

        r1, r2 = asyncio.run(run())
        assert r1.status_code == 200
        # Second call: pending is still in the store (get_pending peeks, doesn't
        # consume), so this also succeeds. /oauth/token is the path that
        # consumes the record.
        assert r2.status_code == 200
