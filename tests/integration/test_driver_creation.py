"""E6 (spec 032): create_or_update_driver appends canonical blocks (mocked SDK).

The stored prompt is core + canonical Hard Rules + Conversation Behavior,
exactly once, language-aware, across creates and updates.
"""

import json
from unittest.mock import MagicMock, patch

import pytest


def _register_and_get_tools():
    from mcp.server.fastmcp import FastMCP

    from src.tools.simulations import register_tools

    mcp = FastMCP("test")
    register_tools(mcp)
    return {name: tool.fn for name, tool in mcp._tool_manager._tools.items()}


@pytest.fixture
def tools():
    return _register_and_get_tools()


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("OKAREO_API_KEY", "test-api-key-12345")


CORE = (
    "## Persona\n\n-   A new user named Taylor.\n\n"
    "## Scenario Details\n\n{scenario_input}\n\n"
    "## Objectives\n\n1. Learn what the product does.\n\n"
    "## Soft Tactics\n\n1. Probe politely."
)


def _mock_backend(mock_request):
    def echo(okareo, method, path, json=None):
        return dict(json or {}, id="drv-1")
    mock_request.side_effect = echo


class TestDriverCanonicalBlocks:
    @patch("src.tools.simulations.okareo_api_request")
    @patch("src.tools.simulations.get_okareo_client")
    def test_create_appends_blocks_once(self, mock_client, mock_request, tools):
        mock_client.return_value = MagicMock()
        _mock_backend(mock_request)

        result = json.loads(tools["create_or_update_driver"](
            name="taylor", prompt_template=CORE,
        ))

        assert "error" not in result, result
        assert result["canonical_blocks_appended"] is True
        stored = mock_request.call_args.kwargs["json"]["prompt_template"]
        assert stored.startswith("## Persona")
        assert stored.count("## Hard Rules") == 1
        assert stored.count("## Turn-End Checklist") == 1
        assert stored.count("## Conversation Behavior") == 1
        assert "Always and only respond in English." in stored

    @patch("src.tools.simulations.okareo_api_request")
    @patch("src.tools.simulations.get_okareo_client")
    def test_update_with_already_appended_prompt_stays_single(
        self, mock_client, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        _mock_backend(mock_request)

        first = json.loads(tools["create_or_update_driver"](
            name="taylor", prompt_template=CORE,
        ))
        assert "error" not in first, first
        stored_once = mock_request.call_args.kwargs["json"]["prompt_template"]

        second = json.loads(tools["create_or_update_driver"](
            name="taylor", prompt_template=stored_once,
        ))
        assert "error" not in second, second
        stored_twice = mock_request.call_args.kwargs["json"]["prompt_template"]

        assert stored_twice == stored_once
        assert stored_twice.count("## Hard Rules") == 1

    @patch("src.tools.simulations.okareo_api_request")
    @patch("src.tools.simulations.get_okareo_client")
    def test_language_drives_hard_rules_line(
        self, mock_client, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        _mock_backend(mock_request)

        result = json.loads(tools["create_or_update_driver"](
            name="carmen", prompt_template=CORE, language="es",
        ))

        assert "error" not in result, result
        stored = mock_request.call_args.kwargs["json"]["prompt_template"]
        assert (
            "-   Always and only respond in Spanish (es). "
            "Never respond in any other language." in stored
        )
