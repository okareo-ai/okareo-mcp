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
