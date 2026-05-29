"""Tests for src/auth/jwks_cache.py."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


@pytest.fixture
def jwks_cache_cls():
    """Late import so the test file remains collectible before the impl lands."""
    from src.auth.jwks_cache import JWKSCache

    return JWKSCache


def _mock_response(jwks_doc):
    r = MagicMock(spec=httpx.Response)
    r.json.return_value = jwks_doc
    r.raise_for_status.return_value = None
    return r


class TestJWKSCache:
    def test_first_call_fetches_jwks(self, jwks_cache_cls, jwks_doc):
        mock_get = AsyncMock(return_value=_mock_response(jwks_doc))

        async def run():
            cache = jwks_cache_cls("https://test.frontegg.example", ttl=10.0)
            with patch("httpx.AsyncClient.get", mock_get):
                key = await cache.get_key("test-key-1")
            return key

        key = asyncio.run(run())
        assert key is not None
        assert key["kid"] == "test-key-1"
        assert mock_get.await_count == 1

    def test_second_call_within_ttl_uses_cache(self, jwks_cache_cls, jwks_doc):
        mock_get = AsyncMock(return_value=_mock_response(jwks_doc))

        async def run():
            cache = jwks_cache_cls("https://test.frontegg.example", ttl=60.0)
            with patch("httpx.AsyncClient.get", mock_get):
                await cache.get_key("test-key-1")
                await cache.get_key("test-key-1")

        asyncio.run(run())
        assert mock_get.await_count == 1  # cached on second call

    def test_unknown_kid_triggers_refresh(self, jwks_cache_cls, jwks_doc):
        rotated = {
            "keys": jwks_doc["keys"]
            + [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": "rotated-key",
                    "n": jwks_doc["keys"][0]["n"],
                    "e": jwks_doc["keys"][0]["e"],
                }
            ]
        }
        responses = [_mock_response(jwks_doc), _mock_response(rotated)]
        idx = {"n": 0}

        async def _mock_get(*_args, **_kwargs):
            r = responses[idx["n"]]
            idx["n"] += 1
            return r

        async def run():
            cache = jwks_cache_cls("https://test.frontegg.example", ttl=60.0)
            with patch("httpx.AsyncClient.get", _mock_get):
                first = await cache.get_key("test-key-1")
                second = await cache.get_key("rotated-key")
            return first, second

        first, second = asyncio.run(run())
        assert first is not None
        assert second is not None
        assert second["kid"] == "rotated-key"
        assert idx["n"] == 2

    def test_network_failure_returns_last_known_good(
        self, jwks_cache_cls, jwks_doc
    ):
        success = _mock_response(jwks_doc)
        responses_iter = iter([success, httpx.ConnectError("boom")])

        async def _mock_get(*_args, **_kwargs):
            r = next(responses_iter)
            if isinstance(r, Exception):
                raise r
            return r

        async def run():
            cache = jwks_cache_cls("https://test.frontegg.example", ttl=0.001)
            with patch("httpx.AsyncClient.get", _mock_get):
                first = await cache.get_key("test-key-1")
                # Wait past TTL so the next get triggers a refresh that fails.
                await asyncio.sleep(0.005)
                second = await cache.get_key("test-key-1")
            return first, second

        first, second = asyncio.run(run())
        assert first is not None
        assert second is not None
        assert second["kid"] == "test-key-1"
