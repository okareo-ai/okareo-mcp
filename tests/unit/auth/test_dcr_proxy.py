"""Tests for src/auth/dcr_proxy.py (RFC 7591 DCR — in-process storage).

Rewritten 2026-05-16: the DCR proxy no longer calls Frontegg. Registrations
are stored in the supplied OAuthStateStore.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest


@pytest.fixture
def store():
    from src.auth.oauth_state import OAuthStateStore

    return OAuthStateStore()


@pytest.fixture
def app(store):
    from src.auth.dcr_proxy import build_dcr_app

    return build_dcr_app(store)


def _post(app, body):
    transport = httpx.ASGITransport(app=app)

    async def _run():
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post(
                "/register",
                json=body,
                headers={"content-type": "application/json"},
            )

    return asyncio.run(_run())


class TestDCRProxy:
    def test_well_formed_register_returns_rfc7591(self, app, store):
        r = _post(
            app,
            {
                "redirect_uris": ["http://127.0.0.1:33418/oauth/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "client_name": "Claude Code",
                "scope": "okareo:use",
            },
        )
        assert r.status_code == 201
        body = r.json()
        # client_id is opaque + minted by us
        assert body["client_id"]
        assert len(body["client_id"]) >= 32
        # echo of request
        assert body["redirect_uris"] == [
            "http://127.0.0.1:33418/oauth/callback"
        ]
        assert body["token_endpoint_auth_method"] == "none"
        assert "authorization_code" in body["grant_types"]
        assert body["response_types"] == ["code"]
        assert body["scope"] == "okareo:use"
        assert body["client_name"] == "Claude Code"
        # public-client invariant: no client_secret in response
        assert "client_secret" not in body

    def test_registration_persists_in_store(self, app, store):
        r = _post(
            app,
            {
                "redirect_uris": ["http://127.0.0.1:33418/"],
                "client_name": "VS Code",
            },
        )
        client_id = r.json()["client_id"]
        record = asyncio.run(store.get_client(client_id))
        assert record is not None
        assert record.client_name == "VS Code"
        assert record.redirect_uris == ("http://127.0.0.1:33418/",)

    def test_malformed_body_returns_400(self, app):
        r = _post(app, {})  # no redirect_uris
        assert r.status_code == 400

    def test_invalid_json_returns_400(self, app):
        transport = httpx.ASGITransport(app=app)

        async def _run():
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.post(
                    "/register",
                    content=b"not valid json",
                    headers={"content-type": "application/json"},
                )

        r = asyncio.run(_run())
        assert r.status_code == 400

    def test_non_string_redirect_uri_returns_400(self, app):
        r = _post(app, {"redirect_uris": [123]})
        assert r.status_code == 400

    def test_empty_string_redirect_uri_returns_400(self, app):
        r = _post(app, {"redirect_uris": [""]})
        assert r.status_code == 400

    def test_each_registration_mints_new_client_id(self, app):
        r1 = _post(
            app,
            {
                "redirect_uris": ["http://localhost/"],
                "client_name": "same",
            },
        )
        r2 = _post(
            app,
            {
                "redirect_uris": ["http://localhost/"],
                "client_name": "same",
            },
        )
        assert r1.json()["client_id"] != r2.json()["client_id"]
