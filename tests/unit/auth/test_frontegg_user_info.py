"""Unit tests for src/auth/frontegg_user_info.py — the Frontegg user-info wrapper.

The wrapper sources the user's tenant list (id + name) from Frontegg
(`GET /identity/resources/users/v3/me`) using the calling user's own JWT —
never an admin / M2M credential (FR-029). Per-session TTL cache (60s default)
avoids redundant calls when `list_tenants` is invoked repeatedly.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.auth import frontegg_user_info


@pytest.fixture(autouse=True)
def _isolate_cache():
    frontegg_user_info._reset_for_tests()
    yield
    frontegg_user_info._reset_for_tests()


def _mock_response(status: int, json_data: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json = MagicMock(return_value=json_data or {})
    resp.is_success = 200 <= status < 300
    return resp


def _run(coro_factory):
    """Run an async coroutine in a fresh event loop. Mirrors the project's
    existing test pattern (see test_oauth_proxy.py, test_jwks_cache.py)."""
    return asyncio.run(coro_factory())


class TestHappyPath:
    def test_parses_tenants(self):
        body = {
            "tenants": [
                {"tenantId": "t-1", "name": "Acme Corp"},
                {"tenantId": "t-2", "name": "Globex"},
            ],
            "tenantId": "t-1",
        }
        with patch(
            "src.auth.frontegg_user_info._get",
            AsyncMock(return_value=_mock_response(200, body)),
        ):
            tenants = _run(lambda: frontegg_user_info.get_user_tenants(
                jwt="dummy.jwt.token",
                session_id="sess-A",
                frontegg_domain="test.frontegg.example",
            ))

        assert len(tenants) == 2
        assert tenants[0].id == "t-1"
        assert tenants[0].name == "Acme Corp"
        assert tenants[1].id == "t-2"
        assert tenants[1].name == "Globex"


class TestErrorMapping:
    def test_401_raises_auth_error(self):
        with patch(
            "src.auth.frontegg_user_info._get",
            AsyncMock(return_value=_mock_response(401)),
        ):
            with pytest.raises(frontegg_user_info.TenantLookupAuthError):
                _run(lambda: frontegg_user_info.get_user_tenants(
                    jwt="dummy", session_id="sess", frontegg_domain="x.test",
                ))

    def test_5xx_raises_lookup_failed(self):
        with patch(
            "src.auth.frontegg_user_info._get",
            AsyncMock(return_value=_mock_response(503)),
        ):
            with pytest.raises(frontegg_user_info.TenantLookupFailed):
                _run(lambda: frontegg_user_info.get_user_tenants(
                    jwt="dummy", session_id="sess", frontegg_domain="x.test",
                ))

    def test_network_error_raises_lookup_failed(self):
        with patch(
            "src.auth.frontegg_user_info._get",
            AsyncMock(side_effect=httpx.ConnectError("network down")),
        ):
            with pytest.raises(frontegg_user_info.TenantLookupFailed):
                _run(lambda: frontegg_user_info.get_user_tenants(
                    jwt="dummy", session_id="sess", frontegg_domain="x.test",
                ))


class TestCache:
    def test_second_call_hits_cache(self):
        body = {"tenants": [{"tenantId": "t-1", "name": "Acme"}]}
        mock_get = AsyncMock(return_value=_mock_response(200, body))

        async def go():
            await frontegg_user_info.get_user_tenants(
                jwt="dummy", session_id="sess-A", frontegg_domain="x.test",
            )
            await frontegg_user_info.get_user_tenants(
                jwt="dummy", session_id="sess-A", frontegg_domain="x.test",
            )

        with patch("src.auth.frontegg_user_info._get", mock_get):
            asyncio.run(go())

        assert mock_get.await_count == 1, "second call should have hit the cache"

    def test_ttl_expiry_refetches(self, monkeypatch):
        body = {"tenants": [{"tenantId": "t-1", "name": "Acme"}]}
        mock_get = AsyncMock(return_value=_mock_response(200, body))
        clock = {"now": 1000.0}
        monkeypatch.setattr(frontegg_user_info, "_monotonic", lambda: clock["now"])
        monkeypatch.setattr(frontegg_user_info, "_CACHE_TTL_SECONDS", 60.0)

        async def go():
            await frontegg_user_info.get_user_tenants(
                jwt="dummy", session_id="sess-A", frontegg_domain="x.test",
            )
            clock["now"] = 1100.0  # +100s, past TTL
            await frontegg_user_info.get_user_tenants(
                jwt="dummy", session_id="sess-A", frontegg_domain="x.test",
            )

        with patch("src.auth.frontegg_user_info._get", mock_get):
            asyncio.run(go())

        assert mock_get.await_count == 2

    def test_invalidate_cache_targets_session(self):
        body = {"tenants": [{"tenantId": "t-1", "name": "Acme"}]}
        mock_get = AsyncMock(return_value=_mock_response(200, body))

        async def go():
            await frontegg_user_info.get_user_tenants(
                jwt="dummy", session_id="sess-A", frontegg_domain="x.test",
            )
            await frontegg_user_info.get_user_tenants(
                jwt="dummy", session_id="sess-B", frontegg_domain="x.test",
            )
            # Invalidate sess-A only.
            frontegg_user_info.invalidate_cache("sess-A")
            await frontegg_user_info.get_user_tenants(
                jwt="dummy", session_id="sess-A", frontegg_domain="x.test",
            )
            await frontegg_user_info.get_user_tenants(
                jwt="dummy", session_id="sess-B", frontegg_domain="x.test",
            )

        with patch("src.auth.frontegg_user_info._get", mock_get):
            asyncio.run(go())

        # Calls: A miss, B miss, A miss (cache cleared), B hit (still cached)
        assert mock_get.await_count == 3


class TestSecurity:
    def test_jwt_never_in_logs(self, caplog):
        """The user's bearer token MUST NEVER appear in any log line."""
        sentinel = "okareo-sentinel-jwt-do-not-leak.aaa.bbb"
        body = {"tenants": [{"tenantId": "t-1", "name": "Acme"}]}

        with patch(
            "src.auth.frontegg_user_info._get",
            AsyncMock(return_value=_mock_response(200, body)),
        ):
            _run(lambda: frontegg_user_info.get_user_tenants(
                jwt=sentinel, session_id="sess", frontegg_domain="x.test",
            ))

        for record in caplog.records:
            assert sentinel not in record.getMessage(), (
                f"JWT leaked in log: {record.getMessage()!r}"
            )

    def test_jwt_never_in_error_message(self):
        sentinel = "okareo-sentinel-jwt-do-not-leak.aaa.bbb"
        with patch(
            "src.auth.frontegg_user_info._get",
            AsyncMock(return_value=_mock_response(401)),
        ):
            try:
                _run(lambda: frontegg_user_info.get_user_tenants(
                    jwt=sentinel, session_id="sess", frontegg_domain="x.test",
                ))
            except frontegg_user_info.TenantLookupAuthError as e:
                assert sentinel not in str(e), "JWT leaked in error message"


class TestMeTenantsEndpointShape:
    """Frontegg's /identity/resources/users/v3/me/tenants returns a top-level
    JSON array of full tenant objects (with names). This is the preferred
    path and what we want for `list_tenants` UX."""

    def test_top_level_array_with_names(self):
        body = [
            {"tenantId": "t-1", "name": "Acme Corp"},
            {"tenantId": "t-2", "name": "Globex"},
        ]
        with patch(
            "src.auth.frontegg_user_info._get",
            AsyncMock(return_value=_mock_response(200, body)),
        ):
            tenants = _run(lambda: frontegg_user_info.get_user_tenants(
                jwt="dummy", session_id="sess", frontegg_domain="x.test",
            ))
        assert tenants == [
            frontegg_user_info.Tenant(id="t-1", name="Acme Corp"),
            frontegg_user_info.Tenant(id="t-2", name="Globex"),
        ]

    def test_alternative_name_field_displayName(self):
        body = [{"tenantId": "t-1", "displayName": "Acme"}]
        with patch(
            "src.auth.frontegg_user_info._get",
            AsyncMock(return_value=_mock_response(200, body)),
        ):
            tenants = _run(lambda: frontegg_user_info.get_user_tenants(
                jwt="dummy", session_id="sess", frontegg_domain="x.test",
            ))
        assert tenants[0].name == "Acme"

    def test_falls_back_to_me_on_unexpected_shape(self, caplog):
        """/me/tenants returns a weird shape — wrapper falls back to /me,
        and warns when names are still missing."""
        import logging
        responses = iter([
            _mock_response(200, {"unexpected": "shape"}),
            _mock_response(
                200,
                {"tenants": [{"tenantId": "t-1"}, {"tenantId": "t-2"}]},
            ),
        ])

        async def _spy(url, headers):
            return next(responses)

        with patch("src.auth.frontegg_user_info._get", _spy):
            with caplog.at_level(logging.WARNING, logger="src.auth.frontegg_user_info"):
                tenants = _run(lambda: frontegg_user_info.get_user_tenants(
                    jwt="dummy", session_id="sess", frontegg_domain="x.test",
                ))

        assert [t.id for t in tenants] == ["t-1", "t-2"]
        assert all(t.name == "" for t in tenants)
        warned = [
            r.getMessage() for r in caplog.records
            if "no name field" in r.getMessage()
        ]
        assert warned, f"expected diagnostic, got records: {[r.getMessage() for r in caplog.records]}"


# ---------------------------------------------------------------------------
# refresh_with_tenant (2026-05-18 pivot — tenant switching via Frontegg)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# refresh_with_tenant — two-step /me/tenant + /oauth/token flow
# (2026-05-18, third iteration — final design)
# ---------------------------------------------------------------------------


class TestSwitchViaMeTenant:
    """The new flow: PUT /me/tenant with Bearer auth to switch active tenant,
    then refresh via /oauth/token to get a tenant-scoped access token."""

    def _patch_two_calls(self, me_tenant_response, oauth_response):
        """Patch httpx.AsyncClient so the first call returns ``me_tenant_response``
        and the second returns ``oauth_response``."""
        responses = iter([me_tenant_response, oauth_response])

        async def _post(self, url, **kwargs):
            return next(responses)

        async def _put(self, url, **kwargs):
            return next(responses)

        client_mock = AsyncMock()
        client_mock.put = _put.__get__(client_mock)
        client_mock.post = _post.__get__(client_mock)
        ctx_mgr = MagicMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=client_mock)
        ctx_mgr.__aexit__ = AsyncMock(return_value=None)
        return patch(
            "src.auth.frontegg_user_info.httpx.AsyncClient",
            return_value=ctx_mgr,
        )

    def test_me_tenant_returns_tokens_directly(self):
        """Some Frontegg configs return new tokens in the /me/tenant response.
        When they do, we use them directly (no follow-up refresh needed)."""
        me_resp = _mock_response(200, {
            "accessToken": "new-tenant-bound-jwt",
            "refreshToken": "rotated-rt",
            "expiresIn": 1800,
        })
        # Should NOT be consumed (we return after step 1):
        oauth_resp = _mock_response(500)

        with self._patch_two_calls(me_resp, oauth_resp):
            result = _run(lambda: frontegg_user_info.refresh_with_tenant(
                refresh_token="rt",
                tenant_id="t-target",
                frontegg_domain="x.test",
                frontegg_client_id="c",
                current_access_token="access-jwt",
            ))

        assert result["access_token"] == "new-tenant-bound-jwt"
        assert result["refresh_token"] == "rotated-rt"

    def test_me_tenant_empty_response_falls_back_to_oauth_refresh(self):
        """When /me/tenant returns 200 with empty body, we follow up with
        /oauth/token refresh to pick up the new tenant context."""
        me_resp = _mock_response(200, {})
        oauth_resp = _mock_response(200, {
            "access_token": "jwt-from-refresh",
            "refresh_token": "rt-from-refresh",
            "expires_in": 1800,
        })

        with self._patch_two_calls(me_resp, oauth_resp):
            result = _run(lambda: frontegg_user_info.refresh_with_tenant(
                refresh_token="rt-original",
                tenant_id="t-target",
                frontegg_domain="x.test",
                frontegg_client_id="c",
                current_access_token="access-jwt",
            ))

        assert result["access_token"] == "jwt-from-refresh"
        assert result["refresh_token"] == "rt-from-refresh"

    def test_me_tenant_401_raises_auth_error(self):
        me_resp = _mock_response(401)
        oauth_resp = _mock_response(200, {})

        with self._patch_two_calls(me_resp, oauth_resp):
            with pytest.raises(frontegg_user_info.TenantSwitchAuthError):
                _run(lambda: frontegg_user_info.refresh_with_tenant(
                    refresh_token="rt",
                    tenant_id="t",
                    frontegg_domain="x.test",
                    frontegg_client_id="c",
                    current_access_token="stale-access-jwt",
                ))

    def test_me_tenant_403_raises_auth_error(self):
        """403 means user is not authorized for the target tenant. Treated
        the same as 401 — surfaces 'need re-sign-in' to the user (the
        FR-025 pre-validation should have caught this earlier, so a 403
        here suggests something inconsistent and re-authing is the right
        response)."""
        me_resp = _mock_response(403)
        oauth_resp = _mock_response(200, {})

        with self._patch_two_calls(me_resp, oauth_resp):
            with pytest.raises(frontegg_user_info.TenantSwitchAuthError):
                _run(lambda: frontegg_user_info.refresh_with_tenant(
                    refresh_token="rt",
                    tenant_id="t",
                    frontegg_domain="x.test",
                    frontegg_client_id="c",
                    current_access_token="access-jwt",
                ))

    def test_me_tenant_5xx_raises_generic_failure(self):
        me_resp = _mock_response(503)
        oauth_resp = _mock_response(200, {})

        with self._patch_two_calls(me_resp, oauth_resp):
            with pytest.raises(frontegg_user_info.TenantSwitchFailed):
                _run(lambda: frontegg_user_info.refresh_with_tenant(
                    refresh_token="rt",
                    tenant_id="t",
                    frontegg_domain="x.test",
                    frontegg_client_id="c",
                    current_access_token="access-jwt",
                ))

    def test_missing_current_access_token_fails_locally(self):
        """The new flow REQUIRES the current access token for /me/tenant
        Bearer auth. Empty value fails before any network call."""
        with pytest.raises(frontegg_user_info.TenantSwitchFailed):
            _run(lambda: frontegg_user_info.refresh_with_tenant(
                refresh_token="rt",
                tenant_id="t",
                frontegg_domain="x.test",
                frontegg_client_id="c",
                current_access_token="",
            ))

    def test_me_tenant_request_shape(self):
        """The PUT request to /me/tenant must include the current access
        token as Bearer auth and the tenantId in the JSON body."""
        captured: dict = {}

        async def _capture_put(self, url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers", {})
            captured["json"] = kwargs.get("json", {})
            return _mock_response(200, {
                "accessToken": "x",
                "refreshToken": "y",
                "expiresIn": 0,
            })

        client_mock = AsyncMock()
        client_mock.put = _capture_put.__get__(client_mock)
        ctx_mgr = MagicMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=client_mock)
        ctx_mgr.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "src.auth.frontegg_user_info.httpx.AsyncClient",
            return_value=ctx_mgr,
        ):
            _run(lambda: frontegg_user_info.refresh_with_tenant(
                refresh_token="rt",
                tenant_id="t-target",
                frontegg_domain="test.frontegg.example",
                frontegg_client_id="c",
                current_access_token="user-access-jwt",
            ))

        assert captured["url"].endswith("/identity/resources/users/v1/tenant")
        assert captured["headers"]["Authorization"] == "Bearer user-access-jwt"
        assert captured["json"] == {"tenantId": "t-target"}
