"""E7 (spec 032): driver language derives from the selected voice.

With a `voice` set and `language` omitted, the MCP fills `language` from the
voice's catalog language and discloses it. An explicit `language` whose base
code conflicts with the voice's language is rejected naming both values.
Catalog unavailable → checks skipped (non-fatal).
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
    "## Persona\n\n-   Aiko, a curious caller.\n\n"
    "## Scenario Details\n\n{scenario_input}\n\n"
    "## Objectives\n\n1. Ask about the service.\n\n"
    "## Soft Tactics\n\n1. Probe politely."
)

CATALOG_VOICES = [
    {"id": "v-aiko", "name": "Aiko - Calming Voice", "language": "ja"},
    {"id": "v-blake", "name": "Blake - Helpful Agent", "language": "en",
     "accent": "American"},
    {"id": "v-gerard", "name": "Gerard", "language": "fr",
     "accent": "Parisian"},
    {"id": "v-nolang", "name": "Mystery Voice"},
]


def _driver_backend(mock_request):
    def echo(okareo, method, path, json=None):
        return dict(json or {}, id="drv-1")
    mock_request.side_effect = echo


def _with_catalog(mock_fetch):
    mock_fetch.return_value = {
        "voices": CATALOG_VOICES,
        "voice_profiles": [],
        "languages": ["en", "fr", "ja"],
    }


@patch("src.tools.simulations.okareo_api_request")
@patch("src.tools.simulations._fetch_voice_catalog")
@patch("src.tools.simulations.get_okareo_client")
class TestLanguageDerivation:
    def test_voice_without_language_derives_and_discloses(
        self, mock_client, mock_fetch, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        _with_catalog(mock_fetch)
        _driver_backend(mock_request)

        result = json.loads(tools["create_or_update_driver"](
            name="aiko-driver", prompt_template=CORE, voice="v-aiko",
        ))

        assert "error" not in result, result
        assert result["language_derived_from_voice"] == "ja"
        body = mock_request.call_args.kwargs["json"]
        assert body["language"] == "ja"
        # The derived language reaches the appended Hard Rules language rule.
        assert "Japanese (ja)" in body["prompt_template"]

    def test_matching_base_code_accepted(
        self, mock_client, mock_fetch, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        _with_catalog(mock_fetch)
        _driver_backend(mock_request)

        result = json.loads(tools["create_or_update_driver"](
            name="gerard-driver", prompt_template=CORE,
            voice="v-gerard", language="fr-CA",
        ))

        assert "error" not in result, result
        assert "language_derived_from_voice" not in result
        assert mock_request.call_args.kwargs["json"]["language"] == "fr-CA"

    def test_conflicting_language_rejected_naming_both(
        self, mock_client, mock_fetch, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        _with_catalog(mock_fetch)
        _driver_backend(mock_request)

        result = json.loads(tools["create_or_update_driver"](
            name="mismatch-driver", prompt_template=CORE,
            voice="v-aiko", language="es",
        ))

        assert "error" in result
        assert "'ja'" in result["error"]
        assert "'es'" in result["error"]
        mock_request.assert_not_called()

    def test_voice_without_catalog_language_skips_checks(
        self, mock_client, mock_fetch, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        _with_catalog(mock_fetch)
        _driver_backend(mock_request)

        result = json.loads(tools["create_or_update_driver"](
            name="mystery-driver", prompt_template=CORE,
            voice="v-nolang", language="es",
        ))

        assert "error" not in result, result
        assert mock_request.call_args.kwargs["json"]["language"] == "es"

    def test_catalog_unavailable_skips_checks(
        self, mock_client, mock_fetch, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        mock_fetch.side_effect = Exception("catalog down")
        _driver_backend(mock_request)

        result = json.loads(tools["create_or_update_driver"](
            name="aiko-driver", prompt_template=CORE,
            voice="v-aiko", language="es",
        ))

        assert "error" not in result, result
        assert mock_request.call_args.kwargs["json"]["language"] == "es"


@patch("src.tools.simulations.okareo_api_request")
@patch("src.tools.simulations.get_okareo_client")
class TestNoVoicePassThrough:
    def test_language_passes_through_without_voice(
        self, mock_client, mock_request, tools
    ):
        mock_client.return_value = MagicMock()
        _driver_backend(mock_request)

        result = json.loads(tools["create_or_update_driver"](
            name="text-driver", prompt_template=CORE, language="es",
        ))

        assert "error" not in result, result
        body = mock_request.call_args.kwargs["json"]
        assert body["language"] == "es"
        assert "Spanish (es)" in body["prompt_template"]
