"""T035 — p95 verifier overhead per request against a warm JWKS cache.

This is a soft-budget check: the verifier path should add <50ms p95 to a
``tools/list``-shaped request when the JWKS is already cached. We measure
the verifier-only path (no real network) by exercising
``CombinedTokenVerifier.verify_token`` 200 times against a fixture-signed
JWT and asserting the p95 wall-clock.

The test is intentionally lightweight — it runs in CI but its purpose is to
catch order-of-magnitude regressions (e.g., someone re-fetching JWKS per
call), not to micro-benchmark.
"""

from __future__ import annotations

import asyncio
import statistics
import time

import pytest

from src.auth.context import _reset_for_tests as _reset_credential


@pytest.fixture(autouse=True)
def _isolate():
    _reset_credential()
    yield
    _reset_credential()


def test_verifier_p95_under_50ms(
    rsa_keypair, jwks_doc, issuer_url, resource_server_url, jwt_signer, default_claims,
):
    from src.auth.jwks_cache import JWKSCache
    from src.auth.verifier import CombinedTokenVerifier

    async def _stub_get_key(kid: str):
        for k in jwks_doc["keys"]:
            if k["kid"] == kid:
                return k
        return None

    jwks = JWKSCache(issuer_url)
    jwks.get_key = _stub_get_key  # type: ignore[method-assign]

    async def _resolver(_):  # noqa: ANN001
        return None

    verifier = CombinedTokenVerifier(
        issuer_url=issuer_url,
        resource_server_url=resource_server_url,
        jwks_cache=jwks,
        api_key_resolver=_resolver,
        required_scope="okareo:use",
    )
    token = jwt_signer(default_claims)

    # Warm the cache and JIT once.
    asyncio.run(verifier.verify_token(token))

    durations: list[float] = []
    for _ in range(200):
        start = time.perf_counter()
        asyncio.run(verifier.verify_token(token))
        durations.append((time.perf_counter() - start) * 1000)  # ms

    durations.sort()
    p95 = durations[int(0.95 * len(durations)) - 1]
    median = statistics.median(durations)

    # Generous budget — 50ms p95 is well above what's plausible in-process.
    # The point is to catch a real regression (e.g., 500ms because of a
    # synchronous network call), not to micro-bench.
    assert p95 < 50.0, (
        f"verifier p95 = {p95:.1f}ms (median {median:.1f}ms) exceeds 50ms budget"
    )
