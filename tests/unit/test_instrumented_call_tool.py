"""Regression guard for the instrumented CallTool wrapper's error paths.

Every tool is annotated ``-> str``, so FastMCP advertises an outputSchema for
each one and the low-level MCP handler rejects any non-error result that lacks
structuredContent with "Output validation error: outputSchema defined but no
structured output returned". Wrapper-level errors (throttle, missing API key,
tool exceptions) must therefore come back as a full ``CallToolResult`` with
``isError=True`` so the handler passes them through verbatim and the real
error message reaches the client.
"""

import asyncio
import json

import mcp.types as types

from src import server


def _call(name: str, arguments: dict) -> types.CallToolResult:
    handler = server.mcp._mcp_server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )
    return asyncio.run(handler(req)).root


def test_error_content_is_error_result():
    result = server._error_content('{"error": "boom"}')
    assert isinstance(result, types.CallToolResult)
    assert result.isError is True
    assert result.content[0].text == '{"error": "boom"}'


def test_tool_exception_surfaces_real_error_not_schema_violation(monkeypatch):
    # Force the wrapper's exception path deterministically.
    async def _raise(name, arguments):
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(server, "_original_call_tool", _raise)

    result = _call("get_driver", {"name": "any"})
    assert result.isError is True
    text = result.content[0].text
    assert "outputSchema defined" not in text
    payload = json.loads(text)
    assert "error" in payload


def test_call_scope_opened_and_annotations_reach_emit(monkeypatch):
    from unittest.mock import MagicMock, patch

    from src.analytics import AnalyticsClient
    from src.analytics_context import annotate

    monkeypatch.setenv("OKAREO_API_KEY", "test-key")
    monkeypatch.setattr(server, "_okareo_client", MagicMock())

    async def _annotate_then_ok(name, arguments):
        annotate(project_id="proj-from-tool", entity_type="scenario")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text='{"ok": true}')],
        )

    monkeypatch.setattr(server, "_original_call_tool", _annotate_then_ok)
    client = AnalyticsClient(
        http_client=MagicMock(),
        distinct_id="d",
        transport_type="stdio",
        server_version="0.0.test",
        enabled=True,
    )
    monkeypatch.setattr(server, "_analytics_client", client)

    with patch("src.server.emit_tool_event") as mock_emit:
        result = _call("get_driver", {"name": "any"})
        assert result.isError is False
        mock_emit.assert_called_once()
        kwargs = mock_emit.call_args.kwargs
        assert kwargs["annotations"]["project_id"] == "proj-from-tool"
        assert kwargs["annotations"]["entity_type"] == "scenario"
        assert kwargs["success"] is True


def test_tool_that_raises_still_emits_partial_annotations(monkeypatch):
    from unittest.mock import MagicMock, patch

    from src.analytics import AnalyticsClient
    from src.analytics_context import annotate

    monkeypatch.setenv("OKAREO_API_KEY", "test-key")
    monkeypatch.setattr(server, "_okareo_client", MagicMock())

    async def _partial_then_raise(name, arguments):
        annotate(lookup_by="name")
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(server, "_original_call_tool", _partial_then_raise)
    client = AnalyticsClient(
        http_client=MagicMock(),
        distinct_id="d",
        transport_type="stdio",
        server_version="0.0.test",
        enabled=True,
    )
    monkeypatch.setattr(server, "_analytics_client", client)

    with patch("src.server.emit_tool_event") as mock_emit:
        result = _call("get_driver", {"name": "any"})
        assert result.isError is True
        mock_emit.assert_called_once()
        kwargs = mock_emit.call_args.kwargs
        assert kwargs["success"] is False
        assert kwargs["annotations"]["lookup_by"] == "name"
