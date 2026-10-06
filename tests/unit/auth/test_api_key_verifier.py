"""Tests for src/auth/api_key_verifier.py (048 research R2–R5)."""

from __future__ import annotations

import asyncio

import httpx
import jwt as pyjwt
import pytest

from src.auth.api_key_verifier import KeyValidation, OkareoAPIKeyVerifier

BASE = "https://api.okareo.example/"


def _key(**claims) -> str:
    # The verifier never checks the signature itself (okareo-server does), so
    # any signing key will do.
    payload = {"type": "apiKey", "tenantId": "tenant-A", "sub": "user-1"}
    payload.update(claims)
    payload = {k: v for k, v in payload.items() if v is not None}
    return pyjwt.encode(payload, "test-secret-at-least-32-bytes-long!!", algorithm="HS256")


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Upstream:
    """Mock okareo-server: records requests, answers with ``self.respond``."""

    def __init__(self, respond) -> None:
        self.respond = respond
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self.respond(request)
        if isinstance(result, Exception):
            raise result
        return result


def _verifier(upstream: _Upstream, clock: _Clock | None = None, **kw) -> OkareoAPIKeyVerifier:
    return OkareoAPIKeyVerifier(
        base_url=BASE,
        transport=httpx.MockTransport(upstream.handler),
        clock=clock or _Clock(),
        **kw,
    )


def _run(coro):
    return asyncio.run(coro)


def _ok(projects=None):
    return lambda _r: httpx.Response(200, json=projects if projects is not None else [{"id": "p1"}])


class TestOutcomes:
    def test_2xx_is_valid_with_identity_from_claims(self):
        up = _Upstream(_ok())
        key = _key(exp=4102444800)
        result = _run(_verifier(up).validate(key))
        assert result == KeyValidation(
            outcome="valid", tenant_id="tenant-A", subject="user-1", expires_at=4102444800
        )

    def test_empty_project_list_is_still_valid(self):
        # An organization with no projects must be able to connect (FR-005).
        up = _Upstream(_ok([]))
        assert _run(_verifier(up).validate(_key())).outcome == "valid"

    def test_organization_id_claim_is_the_fallback(self):
        up = _Upstream(_ok())
        key = _key(tenantId=None, organization_id="org-B")
        assert _run(_verifier(up).validate(key)).tenant_id == "org-B"

    def test_tenant_access_token_is_validated_the_same_way(self):
        up = _Upstream(_ok())
        key = _key(type="tenantAccessToken")
        assert _run(_verifier(up).validate(key)).outcome == "valid"

    @pytest.mark.parametrize("status", [401, 403])
    def test_rejected_is_invalid(self, status):
        up = _Upstream(lambda _r: httpx.Response(status, json={"detail": "no"}))
        assert _run(_verifier(up).validate(_key())) == KeyValidation(outcome="invalid")

    @pytest.mark.parametrize(
        "respond",
        [
            lambda _r: httpx.Response(500),
            lambda _r: httpx.Response(502),
            lambda _r: httpx.Response(429),
            lambda _r: httpx.ConnectError("down"),
            lambda _r: httpx.ReadTimeout("slow"),
            lambda _r: httpx.Response(200, text="<html>not json</html>"),
        ],
        ids=["500", "502", "429", "connect", "timeout", "non-json-2xx"],
    )
    def test_upstream_trouble_is_unavailable(self, respond):
        up = _Upstream(respond)
        assert _run(_verifier(up).validate(_key())) == KeyValidation(outcome="unavailable")

    def test_one_get_projects_with_api_key_header_only(self):
        up = _Upstream(_ok())
        key = _key()
        _run(_verifier(up).validate(key))
        assert len(up.requests) == 1
        req = up.requests[0]
        assert req.method == "GET"
        assert str(req.url) == "https://api.okareo.example/v0/projects"
        assert req.headers["api-key"] == key
        assert "authorization" not in req.headers

    def test_no_organization_claim_is_invalid(self):
        up = _Upstream(_ok())
        key = _key(tenantId=None)
        assert _run(_verifier(up).validate(key)) == KeyValidation(outcome="invalid")

    @pytest.mark.parametrize("token", ["okareo-OPAQUE-KEY", "", "a.b", "not a jwt.at.all!"])
    def test_non_jwt_is_invalid_without_a_request(self, token):
        up = _Upstream(_ok())
        assert _run(_verifier(up).validate(token)) == KeyValidation(outcome="invalid")
        assert up.requests == []


class TestCache:
    def test_valid_result_is_reused_within_ttl(self):
        up = _Upstream(_ok())
        clock = _Clock()
        v = _verifier(up, clock)
        key = _key()

        async def run():
            await v.validate(key)
            clock.now += 29
            await v.validate(key)

        _run(run())
        assert len(up.requests) == 1

    def test_valid_result_expires_after_ttl(self):
        up = _Upstream(_ok())
        clock = _Clock()
        v = _verifier(up, clock)
        key = _key()

        async def run():
            await v.validate(key)
            clock.now += 31
            await v.validate(key)

        _run(run())
        assert len(up.requests) == 2

    def test_revocation_is_seen_after_ttl(self):
        up = _Upstream(_ok())
        clock = _Clock()
        v = _verifier(up, clock)
        key = _key()

        async def run():
            first = await v.validate(key)
            up.respond = lambda _r: httpx.Response(401)
            clock.now += 31
            return first, await v.validate(key)

        first, second = _run(run())
        assert first.outcome == "valid"
        assert second.outcome == "invalid"

    @pytest.mark.parametrize(
        "respond",
        [lambda _r: httpx.Response(401), lambda _r: httpx.ConnectError("down")],
        ids=["invalid", "unavailable"],
    )
    def test_failures_are_never_cached(self, respond):
        up = _Upstream(respond)
        v = _verifier(up)
        key = _key()

        async def run():
            await v.validate(key)
            await v.validate(key)

        _run(run())
        assert len(up.requests) == 2

    def test_cache_is_bounded_and_evicts_oldest(self):
        up = _Upstream(_ok())
        v = _verifier(up, cache_max=3)
        keys = [_key(sub=f"user-{i}") for i in range(4)]

        async def run():
            for k in keys:
                await v.validate(k)
            before = len(up.requests)
            await v.validate(keys[0])  # evicted → fetched again
            after_oldest = len(up.requests)
            await v.validate(keys[3])  # newest → still cached
            return before, after_oldest, len(up.requests)

        before, after_oldest, after_newest = _run(run())
        assert before == 4
        assert after_oldest == 5
        assert after_newest == 5

    def test_use_cache_false_always_asks_upstream(self):
        up = _Upstream(_ok())
        v = _verifier(up)
        key = _key()

        async def run():
            await v.validate(key)
            await v.validate(key, use_cache=False)

        _run(run())
        assert len(up.requests) == 2

    def test_cache_is_keyed_by_digest_not_the_key(self):
        up = _Upstream(_ok())
        v = _verifier(up)
        key = _key()
        _run(v.validate(key))
        assert key not in repr(v.__dict__)
