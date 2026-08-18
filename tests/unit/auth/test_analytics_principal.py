"""T026 / T030 — in HTTP mode the analytics principal is ``org_id`` not the
process uuid4 and not the bearer credential. When the OAuth JWT carries a
user identity (sub + email), the principal is the user and a one-time
PostHog ``$identify`` associates the email."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from src.analytics import (
    AnalyticsClient,
    _reset_for_tests as _reset_identified,
    emit_tool_event,
)
from src.auth.context import (
    SessionCredential,
    _reset_for_tests as _reset_credential,
    set_session_credential,
)


@pytest.fixture(autouse=True)
def _isolate():
    _reset_credential()
    _reset_identified()
    yield
    _reset_credential()
    _reset_identified()


def _client(transport: str) -> tuple[AnalyticsClient, MagicMock]:
    http = MagicMock()
    return AnalyticsClient(
        http_client=http,
        distinct_id="process-uuid-xyz",
        transport_type=transport,
        server_version="0.0.test",
        enabled=True,
    ), http


def _capture_payload():
    """Return a list that ``_send_event`` will append payloads into."""
    captured: list[dict] = []

    async def _fake_send(http_client, payload):
        captured.append(payload)

    return captured, _fake_send


class TestGroupAssociation:
    def test_groups_present_when_org_id_bound(self):
        client, _ = _client("streamable-http")
        captured, fake_send = _capture_payload()

        async def run():
            set_session_credential(
                SessionCredential(
                    kind="oauth", api_key="k", org_id="org-ACME", subject="u",
                )
            )
            with patch("src.analytics._send_event", fake_send):
                emit_tool_event(client, tool_name="t", success=True)
                await asyncio.sleep(0)

        asyncio.run(run())
        props = captured[0]["properties"]
        assert props["org_id"] == "org-ACME"
        assert props["$groups"] == {"organization": "org-ACME"}

    def test_groups_absent_in_stdio(self):
        client, _ = _client("stdio")
        captured, fake_send = _capture_payload()

        async def run():
            set_session_credential(
                SessionCredential(
                    kind="oauth", api_key="k", org_id="org-ACME",
                )
            )
            with patch("src.analytics._send_event", fake_send):
                emit_tool_event(client, tool_name="t", success=True)
                await asyncio.sleep(0)

        asyncio.run(run())
        props = captured[0]["properties"]
        assert "$groups" not in props
        assert "org_id" not in props


class TestGroupIdentify:
    def test_groupidentify_once_per_org_with_name(self):
        from src.analytics import _store_org_name

        client, _ = _client("streamable-http")
        captured, fake_send = _capture_payload()
        _store_org_name("org-ACME", "Acme Corp")

        async def run():
            set_session_credential(
                SessionCredential(
                    kind="oauth",
                    api_key="k",
                    org_id="org-ACME",
                    subject="user-42",
                    email="dev@example.com",
                )
            )
            with patch("src.analytics._send_event", fake_send):
                emit_tool_event(client, tool_name="a", success=True)
                emit_tool_event(client, tool_name="b", success=True)
                await asyncio.sleep(0)

        asyncio.run(run())
        events = [p["event"] for p in captured]
        assert events.count("$groupidentify") == 1
        gi = next(p for p in captured if p["event"] == "$groupidentify")
        assert gi["properties"]["$group_type"] == "organization"
        assert gi["properties"]["$group_key"] == "org-ACME"
        assert gi["properties"]["$group_set"] == {"name": "Acme Corp"}
        assert gi["properties"]["$process_person_profile"] is False
        # Sequenced before the first tool event.
        first_tool = next(
            i for i, p in enumerate(captured) if p["event"] == "okareo_mcp_tool_call"
        )
        gi_idx = next(
            i for i, p in enumerate(captured) if p["event"] == "$groupidentify"
        )
        assert gi_idx < first_tool
        tool_event = next(
            p for p in captured if p["event"] == "okareo_mcp_tool_call"
        )
        assert tool_event["properties"]["org_name"] == "Acme Corp"

    def test_never_emitted_without_resolved_name(self):
        client, _ = _client("streamable-http")
        captured, fake_send = _capture_payload()

        async def run():
            set_session_credential(
                SessionCredential(
                    kind="oauth", api_key="k", org_id="org-ACME", subject="u",
                )
            )
            with patch("src.analytics._send_event", fake_send), \
                 patch("src.analytics._schedule_org_name_resolution"):
                emit_tool_event(client, tool_name="t", success=True)
                await asyncio.sleep(0)

        asyncio.run(run())
        assert all(p["event"] != "$groupidentify" for p in captured)
        assert "org_name" not in captured[0]["properties"]


class TestOrgNameResolutionFailure:
    def test_raising_lookup_never_blocks_event(self):
        client, _ = _client("streamable-http")
        captured, fake_send = _capture_payload()

        async def _boom(**kwargs):
            raise RuntimeError("frontegg down")

        async def run():
            set_session_credential(
                SessionCredential(
                    kind="oauth",
                    api_key="jwt-token",
                    org_id="org-ACME",
                    subject="u",
                )
            )
            with patch("src.analytics._send_event", fake_send), \
                 patch(
                     "src.auth.frontegg_user_info.get_user_tenants",
                     side_effect=_boom,
                 ):
                emit_tool_event(client, tool_name="t", success=True)
                # Let the background resolution task run and fail.
                await asyncio.sleep(0.05)

        asyncio.run(run())
        assert any(p["event"] == "okareo_mcp_tool_call" for p in captured)
        from src.analytics import _org_names
        assert _org_names.get("org-ACME") is None

        # Negative sentinel prevents a second attempt.
        captured.clear()

        async def run2():
            with patch("src.analytics._send_event", fake_send), \
                 patch(
                     "src.auth.frontegg_user_info.get_user_tenants",
                     side_effect=_boom,
                 ) as mock_tenants:
                emit_tool_event(client, tool_name="t2", success=True)
                await asyncio.sleep(0.05)
                mock_tenants.assert_not_called()

        asyncio.run(run2())

    def test_api_key_session_resolves_no_name(self):
        client, _ = _client("streamable-http")
        captured, fake_send = _capture_payload()

        async def run():
            set_session_credential(
                SessionCredential(
                    kind="api_key", api_key="okareo-key", org_id="org-KEY",
                )
            )
            with patch("src.analytics._send_event", fake_send), \
                 patch("src.analytics._schedule_org_name_resolution") as sched:
                emit_tool_event(client, tool_name="t", success=True)
                await asyncio.sleep(0)
                sched.assert_not_called()

        asyncio.run(run())
        props = captured[0]["properties"]
        assert props["org_id"] == "org-KEY"
        assert props["$groups"] == {"organization": "org-KEY"}
        assert "org_name" not in props


class TestHTTPModePrincipal:
    def test_uses_org_id_from_credential(self):
        client, _ = _client("streamable-http")
        captured, fake_send = _capture_payload()

        async def run():
            cred = SessionCredential(
                kind="oauth",
                api_key="jwt-token-value",
                org_id="org-ACME",
                subject="user-42",
            )
            set_session_credential(cred)
            with patch("src.analytics._send_event", fake_send):
                emit_tool_event(client, tool_name="list_scenarios", success=True)
                # fire-and-forget: yield to let asyncio.create_task scheduled
                # the coroutine and let it complete.
                await asyncio.sleep(0)

        asyncio.run(run())

        assert len(captured) == 1
        payload = captured[0]
        assert payload["distinct_id"] == "org-ACME", (
            f"expected org_id as principal, got {payload['distinct_id']!r}"
        )
        # JWT MUST NEVER appear in the analytics payload.
        import json
        assert "jwt-token-value" not in json.dumps(payload)

    def test_oauth_user_with_email_identifies_and_uses_subject(self):
        client, _ = _client("streamable-http")
        captured, fake_send = _capture_payload()

        async def run():
            cred = SessionCredential(
                kind="oauth",
                api_key="jwt-token-value",
                org_id="org-ACME",
                subject="user-42",
                email="dev@example.com",
            )
            set_session_credential(cred)
            with patch("src.analytics._send_event", fake_send):
                emit_tool_event(client, tool_name="list_scenarios", success=True)
                await asyncio.sleep(0)

        asyncio.run(run())

        # $identify first, then the tool event — both keyed to the user.
        assert [p["event"] for p in captured] == [
            "$identify",
            "okareo_mcp_tool_call",
        ]
        identify, event = captured
        assert identify["distinct_id"] == "user-42"
        assert identify["properties"]["$set"]["email"] == "dev@example.com"
        assert identify["properties"]["$set"]["org_id"] == "org-ACME"
        assert event["distinct_id"] == "user-42"
        assert event["properties"]["$process_person_profile"] is True
        assert event["properties"]["org_id"] == "org-ACME"
        # JWT MUST NEVER appear in any analytics payload.
        import json
        assert "jwt-token-value" not in json.dumps(captured)

    def test_identify_sent_only_once_per_user(self):
        client, _ = _client("streamable-http")
        captured, fake_send = _capture_payload()

        async def run():
            cred = SessionCredential(
                kind="oauth",
                api_key="k",
                org_id="org-ACME",
                subject="user-42",
                email="dev@example.com",
            )
            set_session_credential(cred)
            with patch("src.analytics._send_event", fake_send):
                emit_tool_event(client, tool_name="a", success=True)
                emit_tool_event(client, tool_name="b", success=True)
                await asyncio.sleep(0)

        asyncio.run(run())

        events = [p["event"] for p in captured]
        assert events.count("$identify") == 1
        assert events.count("okareo_mcp_tool_call") == 2

    def test_oauth_without_email_stays_on_org_id_and_anonymous(self):
        client, _ = _client("streamable-http")
        captured, fake_send = _capture_payload()

        async def run():
            cred = SessionCredential(
                kind="oauth",
                api_key="k",
                org_id="org-ACME",
                subject="user-42",
            )
            set_session_credential(cred)
            with patch("src.analytics._send_event", fake_send):
                emit_tool_event(client, tool_name="t", success=True)
                await asyncio.sleep(0)

        asyncio.run(run())

        assert [p["event"] for p in captured] == ["okareo_mcp_tool_call"]
        assert captured[0]["distinct_id"] == "org-ACME"
        assert captured[0]["properties"]["$process_person_profile"] is False

    def test_falls_back_to_process_uuid_when_no_credential(self):
        client, _ = _client("streamable-http")
        captured, fake_send = _capture_payload()

        async def run():
            with patch("src.analytics._send_event", fake_send):
                emit_tool_event(client, tool_name="list_scenarios", success=True)
                await asyncio.sleep(0)

        asyncio.run(run())
        assert captured[0]["distinct_id"] == "process-uuid-xyz"


class TestStdioModePrincipalUnchanged:
    def test_uses_process_uuid_even_with_credential_present(self):
        # In stdio mode there should never be a SessionCredential, but if
        # something binds one defensively the analytics path MUST still
        # use the process uuid — we don't leak HTTP-mode behavior into stdio.
        client, _ = _client("stdio")
        captured, fake_send = _capture_payload()

        async def run():
            set_session_credential(
                SessionCredential(
                    kind="oauth", api_key="k", org_id="org-SHOULD-NOT-BE-USED",
                )
            )
            with patch("src.analytics._send_event", fake_send):
                emit_tool_event(client, tool_name="t", success=True)
                await asyncio.sleep(0)

        asyncio.run(run())
        assert captured[0]["distinct_id"] == "process-uuid-xyz"
