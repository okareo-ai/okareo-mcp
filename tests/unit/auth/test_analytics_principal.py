"""T026 / T030 — in HTTP mode the analytics principal is ``org_id`` not the
process uuid4 and not the bearer credential."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from src.analytics import AnalyticsClient, emit_tool_event
from src.auth.context import (
    SessionCredential,
    _reset_for_tests as _reset_credential,
    set_session_credential,
)


@pytest.fixture(autouse=True)
def _isolate():
    _reset_credential()
    yield
    _reset_credential()


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
