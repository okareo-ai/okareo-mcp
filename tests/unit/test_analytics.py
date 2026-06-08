"""Unit tests for the analytics module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.analytics import (
    AnalyticsClient,
    emit_tool_event,
    init_analytics,
    is_truthy,
    shutdown_analytics,
)


# ---------------------------------------------------------------------------
# is_truthy tests
# ---------------------------------------------------------------------------


class TestIsTruthy:
    def test_true_values(self):
        assert is_truthy("true") is True
        assert is_truthy("True") is True
        assert is_truthy("TRUE") is True
        assert is_truthy("1") is True
        assert is_truthy("yes") is True

    def test_false_values(self):
        assert is_truthy("false") is False
        assert is_truthy("0") is False
        assert is_truthy("no") is False
        assert is_truthy("") is False
        assert is_truthy("anything") is False

    def test_none(self):
        assert is_truthy(None) is False

    def test_whitespace_stripped(self):
        assert is_truthy("  true  ") is True
        assert is_truthy("  1  ") is True


# ---------------------------------------------------------------------------
# init_analytics tests
# ---------------------------------------------------------------------------


class TestInitAnalytics:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("DEV", raising=False)
        monkeypatch.delenv("AIRGAP", raising=False)
        monkeypatch.delenv("OKAREO_ANALYTICS_OPT_IN", raising=False)
        monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
        monkeypatch.delenv("TRANSPORT", raising=False)
        client = init_analytics()
        assert client.enabled is False
        assert client.http_client is None
        assert client.transport_type == "stdio"
        assert len(client.distinct_id) > 0

    def test_enabled_when_opt_in_true(self, monkeypatch):
        monkeypatch.setenv("OKAREO_ANALYTICS_OPT_IN", "true")
        monkeypatch.setenv("POSTHOG_API_KEY", "phk_testkey")
        monkeypatch.delenv("DEV", raising=False)
        monkeypatch.delenv("AIRGAP", raising=False)
        client = init_analytics()
        assert client.enabled is True
        assert client.http_client is not None

    def test_disabled_when_ph_key_missing(self, monkeypatch):
        monkeypatch.setenv("OKAREO_ANALYTICS_OPT_IN", "true")
        monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
        monkeypatch.delenv("DEV", raising=False)
        monkeypatch.delenv("AIRGAP", raising=False)
        client = init_analytics()
        assert client.enabled is False
        assert client.http_client is None

    def test_opt_in_overridden_by_dev(self, monkeypatch):
        monkeypatch.setenv("OKAREO_ANALYTICS_OPT_IN", "true")
        monkeypatch.setenv("POSTHOG_API_KEY", "phk_testkey")
        monkeypatch.setenv("DEV", "true")
        monkeypatch.delenv("AIRGAP", raising=False)
        client = init_analytics()
        assert client.enabled is False
        assert client.http_client is None

    def test_opt_in_overridden_by_airgap(self, monkeypatch):
        monkeypatch.setenv("OKAREO_ANALYTICS_OPT_IN", "true")
        monkeypatch.setenv("POSTHOG_API_KEY", "phk_testkey")
        monkeypatch.delenv("DEV", raising=False)
        monkeypatch.setenv("AIRGAP", "true")
        client = init_analytics()
        assert client.enabled is False
        assert client.http_client is None

    def test_disabled_when_both_true(self, monkeypatch):
        monkeypatch.setenv("OKAREO_ANALYTICS_OPT_IN", "true")
        monkeypatch.setenv("POSTHOG_API_KEY", "phk_testkey")
        monkeypatch.setenv("DEV", "true")
        monkeypatch.setenv("AIRGAP", "true")
        client = init_analytics()
        assert client.enabled is False

    def test_enabled_when_both_false_and_opt_in(self, monkeypatch):
        monkeypatch.setenv("OKAREO_ANALYTICS_OPT_IN", "true")
        monkeypatch.setenv("POSTHOG_API_KEY", "phk_testkey")
        monkeypatch.setenv("DEV", "false")
        monkeypatch.setenv("AIRGAP", "false")
        client = init_analytics()
        assert client.enabled is True

    def test_transport_type_from_env(self, monkeypatch):
        monkeypatch.setenv("TRANSPORT", "streamable-http")
        monkeypatch.setenv("POSTHOG_API_KEY", "phk_testkey")
        monkeypatch.delenv("DEV", raising=False)
        monkeypatch.delenv("AIRGAP", raising=False)
        client = init_analytics()
        assert client.transport_type == "streamable-http"

    def test_distinct_id_is_uuid4(self, monkeypatch):
        monkeypatch.delenv("DEV", raising=False)
        monkeypatch.setenv("POSTHOG_API_KEY", "phk_testkey")
        monkeypatch.delenv("AIRGAP", raising=False)
        client = init_analytics()
        # UUID4 has 36 characters (8-4-4-4-12 with dashes)
        assert len(client.distinct_id) == 36
        assert client.distinct_id.count("-") == 4

    def test_different_sessions_get_different_ids(self, monkeypatch):
        monkeypatch.delenv("DEV", raising=False)
        monkeypatch.setenv("POSTHOG_API_KEY", "phk_testkey")
        monkeypatch.delenv("AIRGAP", raising=False)
        client1 = init_analytics()
        client2 = init_analytics()
        assert client1.distinct_id != client2.distinct_id


# ---------------------------------------------------------------------------
# shutdown_analytics tests
# ---------------------------------------------------------------------------


class TestShutdownAnalytics:
    def test_closes_http_client(self):
        mock_client = AsyncMock()
        client = AnalyticsClient(
            http_client=mock_client,
            distinct_id="test-id",
            transport_type="stdio",
            server_version="0.0.7",
            enabled=True,
        )
        asyncio.run(shutdown_analytics(client))
        mock_client.aclose.assert_awaited_once()

    def test_handles_none_client(self):
        client = AnalyticsClient(
            http_client=None,
            distinct_id="test-id",
            transport_type="stdio",
            server_version="0.0.7",
            enabled=False,
        )
        # Should not raise
        asyncio.run(shutdown_analytics(client))

    def test_suppresses_close_errors(self):
        mock_client = AsyncMock()
        mock_client.aclose.side_effect = Exception("close failed")
        client = AnalyticsClient(
            http_client=mock_client,
            distinct_id="test-id",
            transport_type="stdio",
            server_version="0.0.7",
            enabled=True,
        )
        # Should not raise
        asyncio.run(shutdown_analytics(client))


# ---------------------------------------------------------------------------
# emit_tool_event tests
# ---------------------------------------------------------------------------


class TestEmitToolEvent:
    def test_disabled_client_does_not_emit(self):
        client = AnalyticsClient(
            http_client=None,
            distinct_id="test-id",
            transport_type="stdio",
            server_version="0.0.7",
            enabled=False,
        )
        # Should not raise — no task created
        emit_tool_event(client, "list_checks", True)

    def test_enabled_client_creates_task(self):
        mock_http = MagicMock()
        client = AnalyticsClient(
            http_client=mock_http,
            distinct_id="test-uuid",
            transport_type="stdio",
            server_version="0.0.7",
            enabled=True,
        )
        with patch("src.analytics.asyncio.get_running_loop"), \
             patch("src.analytics.asyncio.create_task") as mock_task:
            emit_tool_event(client, "run_test", True)
            mock_task.assert_called_once()

    def test_payload_contains_only_allowed_fields(self):
        mock_http = MagicMock()
        client = AnalyticsClient(
            http_client=mock_http,
            distinct_id="test-uuid",
            transport_type="stdio",
            server_version="0.0.7",
            enabled=True,
            api_key="phk_testkey",
        )
        captured_payload = {}

        async def capture_send(http_client, payload):
            captured_payload.update(payload)

        with patch("src.analytics._send_event", side_effect=capture_send), \
             patch("src.analytics.asyncio.get_running_loop"), \
             patch("src.analytics.asyncio.create_task") as mock_task:
            # Make create_task actually call the coroutine
            def run_coro(coro):
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(coro)
                finally:
                    loop.close()
                return MagicMock()

            mock_task.side_effect = run_coro
            emit_tool_event(client, "run_test", True)

        assert captured_payload["api_key"] == "phk_testkey"
        assert captured_payload["distinct_id"] == "test-uuid"
        assert captured_payload["event"] == "okareo_mcp_tool_call"
        assert "timestamp" in captured_payload

        props = captured_payload["properties"]
        assert props["tool_name"] == "run_test"
        assert props["transport_type"] == "stdio"
        assert props["server_version"] == "0.0.7"
        assert props["tool_call_success"] is True
        assert props["$process_person_profile"] is False

        # Ensure no extra properties
        allowed_props = {
            "tool_name",
            "transport_type",
            "server_version",
            "tool_call_success",
            "$process_person_profile",
        }
        assert set(props.keys()) == allowed_props

    def test_uses_process_uuid_as_distinct_id(self):
        mock_http = MagicMock()
        client = AnalyticsClient(
            http_client=mock_http,
            distinct_id="process-uuid",
            transport_type="stdio",
            server_version="0.0.7",
            enabled=True,
        )
        captured_payload = {}

        async def capture_send(http_client, payload):
            captured_payload.update(payload)

        with patch("src.analytics._send_event", side_effect=capture_send), \
             patch("src.analytics.asyncio.get_running_loop"), \
             patch("src.analytics.asyncio.create_task") as mock_task:
            def run_coro(coro):
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(coro)
                finally:
                    loop.close()
                return MagicMock()

            mock_task.side_effect = run_coro
            emit_tool_event(client, "run_test", False)

        assert captured_payload["distinct_id"] == "process-uuid"
        assert captured_payload["properties"]["tool_call_success"] is False

    def test_suppresses_create_task_errors(self):
        mock_http = MagicMock()
        client = AnalyticsClient(
            http_client=mock_http,
            distinct_id="test-uuid",
            transport_type="stdio",
            server_version="0.0.7",
            enabled=True,
        )
        with patch("src.analytics.asyncio.create_task", side_effect=RuntimeError("no loop")):
            # Should not raise
            emit_tool_event(client, "run_test", True)
