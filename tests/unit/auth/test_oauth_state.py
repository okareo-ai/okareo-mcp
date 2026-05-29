"""Tests for src/auth/oauth_state.py (PendingAuthorization + RegisteredMcpClient)."""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def store():
    from src.auth.oauth_state import OAuthStateStore

    return OAuthStateStore()


class TestRegisteredMcpClient:
    def test_register_returns_record_with_minted_client_id(self, store):
        async def run():
            return await store.register_client(
                client_name="VS Code",
                redirect_uris=("http://127.0.0.1:33418/",),
            )

        rec = asyncio.run(run())
        assert rec.client_id
        # opaque, sufficiently long
        assert len(rec.client_id) >= 32
        assert rec.client_name == "VS Code"
        assert rec.redirect_uris == ("http://127.0.0.1:33418/",)

    def test_get_client_returns_registered(self, store):
        """With stateless DCR (2026-05-18), get_client() decodes the signed
        client_id and returns a freshly-constructed record — not the same
        instance, but equivalent content. Equality is by value, not identity.
        """
        async def run():
            rec = await store.register_client(
                client_name="VS Code",
                redirect_uris=("http://127.0.0.1:33418/",),
            )
            return rec, await store.get_client(rec.client_id)

        original, looked_up = asyncio.run(run())
        assert looked_up is not None
        assert looked_up.client_id == original.client_id
        assert looked_up.client_name == original.client_name
        assert looked_up.redirect_uris == original.redirect_uris
        assert looked_up.token_endpoint_auth_method == original.token_endpoint_auth_method
        assert looked_up.scope == original.scope

    def test_get_client_unknown_returns_none(self, store):
        async def run():
            return await store.get_client("not-a-real-client-id")

        assert asyncio.run(run()) is None

    def test_each_registration_mints_a_new_client_id(self, store):
        """Even with identical inputs, each /register call gets a fresh
        client_id (a random ``jti`` is part of the signed payload). Matches
        RFC 7591 convention.
        """
        async def run():
            a = await store.register_client(
                client_name="A", redirect_uris=("http://localhost/",)
            )
            b = await store.register_client(
                client_name="A", redirect_uris=("http://localhost/",)
            )
            return a, b

        a, b = asyncio.run(run())
        assert a.client_id != b.client_id

    def test_get_client_with_stale_signing_key_returns_none(self):
        """If the server's signing key rotates (or differs across instances),
        previously-issued client_ids fail HMAC verification and get_client
        returns None — the path that surfaces as 401 invalid_client on
        /oauth/authorize.
        """
        from src.auth.oauth_state import OAuthStateStore

        store_a = OAuthStateStore(dcr_signing_key="key-A-some-entropy-here")
        store_b = OAuthStateStore(dcr_signing_key="key-B-different-entropy")

        async def run():
            rec = await store_a.register_client(
                client_name="x", redirect_uris=("http://localhost/",)
            )
            # rec.client_id was signed with key A; store B can't verify it.
            return await store_b.get_client(rec.client_id)

        assert asyncio.run(run()) is None

    def test_get_client_with_garbage_returns_none(self, store):
        """Garbage / tampered / forged client_ids get None, not exception."""
        async def run():
            return (
                await store.get_client("not-prefixed"),
                await store.get_client("mcp_garbage_no_dot"),
                await store.get_client("mcp_payload.tamperedmac"),
                await store.get_client(""),
            )

        results = asyncio.run(run())
        assert all(r is None for r in results)

    def test_redirect_uris_frozen_tuple(self, store):
        async def run():
            return await store.register_client(
                client_name="Test", redirect_uris=("http://localhost/",)
            )

        rec = asyncio.run(run())
        assert isinstance(rec.redirect_uris, tuple)


class TestPendingAuthorization:
    def _register(self, store, redirect_uri="http://127.0.0.1:33418/"):
        async def run():
            return await store.register_client(
                client_name="test-client",
                redirect_uris=(redirect_uri,),
            )

        return asyncio.run(run())

    def test_create_pending_returns_record_with_code(self, store):
        client = self._register(store)

        async def run():
            return await store.create_pending(
                client_id=client.client_id,
                redirect_uri="http://127.0.0.1:33418/",
                code_challenge="abc-challenge",
                code_challenge_method="S256",
                state_to_client="vs-state",
            )

        rec = asyncio.run(run())
        assert rec.code
        assert len(rec.code) >= 32
        assert rec.frontegg_jwt is None
        assert rec.frontegg_refresh_token is None
        assert rec.code_challenge == "abc-challenge"

    def test_populate_pending_fills_frontegg_fields(self, store):
        client = self._register(store)

        async def run():
            pending = await store.create_pending(
                client_id=client.client_id,
                redirect_uri="http://127.0.0.1:33418/",
                code_challenge="abc",
                code_challenge_method="S256",
                state_to_client=None,
            )
            ok = await store.populate_pending(
                pending.code,
                frontegg_jwt="fake-jwt",
                frontegg_refresh_token="fake-refresh",
            )
            return ok, await store.get_pending(pending.code)

        ok, populated = asyncio.run(run())
        assert ok is True
        assert populated is not None
        assert populated.frontegg_jwt == "fake-jwt"
        assert populated.frontegg_refresh_token == "fake-refresh"

    def test_populate_unknown_code_returns_false(self, store):
        async def run():
            return await store.populate_pending(
                "not-a-real-code", frontegg_jwt="x", frontegg_refresh_token="y"
            )

        assert asyncio.run(run()) is False

    def test_consume_returns_record_and_deletes_it(self, store):
        client = self._register(store)

        async def run():
            pending = await store.create_pending(
                client_id=client.client_id,
                redirect_uri="http://127.0.0.1:33418/",
                code_challenge="x",
                code_challenge_method="S256",
                state_to_client=None,
            )
            await store.populate_pending(
                pending.code, frontegg_jwt="j", frontegg_refresh_token="r"
            )
            first = await store.consume_pending(pending.code)
            second = await store.consume_pending(pending.code)
            return first, second

        first, second = asyncio.run(run())
        assert first is not None
        assert first.frontegg_jwt == "j"
        # one-time use: second consume returns None
        assert second is None

    def test_consume_unknown_code_returns_none(self, store):
        async def run():
            return await store.consume_pending("nope")

        assert asyncio.run(run()) is None

    def test_consume_deletes_even_on_unpopulated_record(self, store):
        """Edge case: caller consumes a pending auth before /oauth/callback
        populated it. Should still delete and return the (unpopulated) record
        so the caller can decide it's not redeemable yet."""
        client = self._register(store)

        async def run():
            pending = await store.create_pending(
                client_id=client.client_id,
                redirect_uri="http://127.0.0.1:33418/",
                code_challenge="x",
                code_challenge_method="S256",
                state_to_client=None,
            )
            first = await store.consume_pending(pending.code)
            second = await store.consume_pending(pending.code)
            return first, second

        first, second = asyncio.run(run())
        assert first is not None
        assert first.frontegg_jwt is None  # not yet populated
        assert second is None  # one-time use enforced


class TestTTLExpiry:
    def test_expired_pending_gc_on_access(self, store):
        from src.auth.oauth_state import OAuthStateStore

        # short TTL store
        store = OAuthStateStore(default_ttl_seconds=0.05)

        async def run():
            client = await store.register_client(
                client_name="t", redirect_uris=("http://localhost/",)
            )
            pending = await store.create_pending(
                client_id=client.client_id,
                redirect_uri="http://localhost/",
                code_challenge="x",
                code_challenge_method="S256",
                state_to_client=None,
            )
            await asyncio.sleep(0.1)  # exceed TTL
            return await store.consume_pending(pending.code)

        assert asyncio.run(run()) is None


class TestConcurrency:
    def test_concurrent_pending_creates_are_independent(self, store):
        """Two concurrent /oauth/authorize requests get distinct codes that
        don't collide."""
        client = self._register(store)
        captured: list[str] = []

        async def one(state_label):
            pending = await store.create_pending(
                client_id=client.client_id,
                redirect_uri="http://127.0.0.1:33418/",
                code_challenge="x",
                code_challenge_method="S256",
                state_to_client=state_label,
            )
            captured.append(pending.code)

        async def run():
            await asyncio.gather(*[one(f"s{i}") for i in range(10)])

        asyncio.run(run())
        assert len(captured) == 10
        assert len(set(captured)) == 10  # all distinct

    def _register(self, store, redirect_uri="http://127.0.0.1:33418/"):
        async def run():
            return await store.register_client(
                client_name="test-client",
                redirect_uris=(redirect_uri,),
            )

        return asyncio.run(run())


class TestSecurityHygiene:
    def test_client_id_is_opaque_random(self, store):
        async def run():
            return await store.register_client(
                client_name="<script>alert(1)</script>",
                redirect_uris=("http://localhost/",),
            )

        rec = asyncio.run(run())
        # opaque ⇒ unrelated to client_name (no injection-by-mangling)
        assert "<" not in rec.client_id
        assert "alert" not in rec.client_id

    def test_pending_code_is_opaque_random(self, store):
        client = asyncio.run(
            store.register_client(
                client_name="t", redirect_uris=("http://localhost/",)
            )
        )

        async def run():
            return await store.create_pending(
                client_id=client.client_id,
                redirect_uri="http://localhost/",
                code_challenge="my-challenge",
                code_challenge_method="S256",
                state_to_client="my-state",
            )

        rec = asyncio.run(run())
        assert "my-challenge" not in rec.code
        assert "my-state" not in rec.code
