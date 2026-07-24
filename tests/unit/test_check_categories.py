"""E3 (spec 032): list_checks groups checks by their `__category` tag.

The Okareo platform organizes checks with `__category:<Category>` convention
tags. The listing surfaces that hierarchy so a co-pilot selects checks from
the category matching the task and modality.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _tests_tools():
    from mcp.server.fastmcp import FastMCP

    from src.tools.tests import register_tools

    mcp = FastMCP("test")
    register_tools(mcp)
    return {name: tool.fn for name, tool in mcp._tool_manager._tools.items()}


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("OKAREO_API_KEY", "test-api-key-12345")


def _brief(name, tags=None, version=None, description="d", output="bool"):
    b = SimpleNamespace(
        name=name,
        description=description,
        output_data_type=output,
    )
    props = {}
    if tags is not None:
        props["tags"] = tags
    if version is not None:
        props["version"] = version
    b.additional_properties = props
    return b


class TestCategoryGrouping:
    @patch("src.tools.tests.get_okareo_client")
    def test_groups_by_category_tag(self, mock_client):
        okareo = MagicMock()
        okareo.get_all_checks.return_value = [
            _brief("is_json", tags=["__category:Output Validation"]),
            _brief("latency", tags=["__category:Performance"]),
            _brief("fluency", tags=["__category:Output Quality"]),
        ]
        mock_client.return_value = okareo

        result = json.loads(_tests_tools()["list_checks"](limit=0))

        cats = result["checks_by_category"]
        assert [c["name"] for c in cats["Output Validation"]] == ["is_json"]
        assert [c["name"] for c in cats["Performance"]] == ["latency"]
        assert [c["name"] for c in cats["Output Quality"]] == ["fluency"]
        assert result["uncategorized"] == []

    @patch("src.tools.tests.get_okareo_client")
    def test_untagged_check_lands_in_uncategorized(self, mock_client):
        okareo = MagicMock()
        okareo.get_all_checks.return_value = [
            _brief("custom_check"),
            _brief("other", tags=["prod"]),  # tags but no __category
        ]
        mock_client.return_value = okareo

        result = json.loads(_tests_tools()["list_checks"](limit=0))

        assert result["checks_by_category"] == {}
        names = {c["name"] for c in result["uncategorized"]}
        assert names == {"custom_check", "other"}

    @patch("src.tools.tests.get_okareo_client")
    def test_multi_category_check_appears_under_each(self, mock_client):
        okareo = MagicMock()
        okareo.get_all_checks.return_value = [
            _brief(
                "wer",
                tags=["__category:Voice Quality", "__category:Output Quality"],
            ),
        ]
        mock_client.return_value = okareo

        result = json.loads(_tests_tools()["list_checks"](limit=0))

        cats = result["checks_by_category"]
        assert [c["name"] for c in cats["Voice Quality"]] == ["wer"]
        assert [c["name"] for c in cats["Output Quality"]] == ["wer"]
        # The duplicate-name semantics are stated for the co-pilot.
        assert "note" in result

    @patch("src.tools.tests.get_okareo_client")
    def test_check_fields_preserved(self, mock_client):
        okareo = MagicMock()
        okareo.get_all_checks.return_value = [
            _brief(
                "is_json",
                tags=["__category:Output Validation"],
                description="Valid JSON?",
                output="bool",
            ),
        ]
        mock_client.return_value = okareo

        result = json.loads(_tests_tools()["list_checks"](limit=0))

        entry = result["checks_by_category"]["Output Validation"][0]
        assert entry["name"] == "is_json"
        assert entry["description"] == "Valid JSON?"
        assert entry["output_data_type"] == "bool"

    @patch("src.tools.tests.get_okareo_client")
    def test_all_versions_still_annotates_version(self, mock_client):
        okareo = MagicMock()
        okareo.get_all_checks.return_value = [
            _brief("my-check", tags=["__category:Output Quality"], version=1),
            _brief("my-check", tags=["__category:Output Quality"], version=2),
        ]
        mock_client.return_value = okareo

        result = json.loads(_tests_tools()["list_checks"](all_versions=True))

        okareo.get_all_checks.assert_called_once_with(all_versions=True)
        entries = result["checks_by_category"]["Output Quality"]
        assert sorted(e["version"] for e in entries) == [1, 2]

    @patch("src.tools.tests.get_okareo_client")
    def test_empty_catalog_is_usable(self, mock_client):
        okareo = MagicMock()
        okareo.get_all_checks.return_value = []
        mock_client.return_value = okareo

        result = json.loads(_tests_tools()["list_checks"]())

        assert result["checks_by_category"] == {}
        assert result["uncategorized"] == []
        assert result["count"] == 0

    @patch("src.tools.tests.get_okareo_client")
    def test_limit_applies_to_total_checks(self, mock_client):
        okareo = MagicMock()
        okareo.get_all_checks.return_value = [
            _brief(f"check-{i}", tags=["__category:Output Quality"])
            for i in range(30)
        ]
        mock_client.return_value = okareo

        result = json.loads(_tests_tools()["list_checks"](limit=5))

        total = sum(
            len(v) for v in result["checks_by_category"].values()
        ) + len(result["uncategorized"])
        assert total == 5
