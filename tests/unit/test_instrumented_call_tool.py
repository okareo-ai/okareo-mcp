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
