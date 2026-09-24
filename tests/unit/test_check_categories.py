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

        # 043 FR-024: categories carry names; entries live once in `checks`.
        cats = result["checks_by_category"]
        assert cats["Output Validation"] == ["is_json"]
        assert cats["Performance"] == ["latency"]
        assert cats["Output Quality"] == ["fluency"]
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
        assert set(result["uncategorized"]) == {"custom_check", "other"}

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
        assert cats["Voice Quality"] == ["wer"]
        assert cats["Output Quality"] == ["wer"]
        # One entry, referenced twice -- not serialized twice (FR-024).
        assert len([c for c in result["checks"] if c["name"] == "wer"]) == 1
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

        assert result["checks_by_category"]["Output Validation"] == ["is_json"]
        entry = next(c for c in result["checks"] if c["name"] == "is_json")
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
        # The category names the check once; both versions live in `checks`.
        assert result["checks_by_category"]["Output Quality"] == ["my-check"]
        assert sorted(c["version"] for c in result["checks"]) == [1, 2]

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


class TestCheckCategoryDeduplication:
    """043 US5 / FR-024: a check in three categories had its full entry —
    description included — serialized three times."""

    def _call(self, **kwargs):
        import json
        from unittest.mock import MagicMock, patch
        from mcp.server.fastmcp import FastMCP
        from src.tools.tests import register_tools

        mcp = FastMCP("test")
        register_tools(mcp)
        tools = {k: t.fn for k, t in mcp._tool_manager._tools.items()}

        def _check(name, cats, desc):
            c = MagicMock()
            c.name = name
            c.description = desc
            c.output_data_type = "bool"
            c.additional_properties = {
                "tags": [f"__category:{c_}" for c_ in cats]
            }
            return c

        checks = [
            _check("multi", ["Voice", "Quality", "Safety"], "D" * 200),
            _check("single", ["Quality"], "S" * 200),
            _check("none", [], "N" * 200),
        ]
        okareo = MagicMock()
        okareo.get_all_checks.return_value = checks
        with patch("src.tools.tests.get_okareo_client", return_value=okareo):
            return json.loads(tools["list_checks"](**kwargs))

    def test_multi_category_check_is_serialized_once(self):
        out = self._call()
        blob = json.dumps(out)
        # The long description must appear exactly once, not once per category.
        assert blob.count("D" * 200) == 1

    def test_categories_still_reference_the_check(self):
        out = self._call()
        for cat in ("Voice", "Quality", "Safety"):
            assert "multi" in json.dumps(out["checks_by_category"][cat])

    def test_every_check_is_reachable(self):
        out = self._call()
        names = {c["name"] for c in out["checks"]}
        assert names == {"multi", "single", "none"}

    def test_uncategorized_checks_are_still_reported(self):
        out = self._call()
        assert "none" in json.dumps(out["uncategorized"])

    def test_check_details_are_intact(self):
        out = self._call()
        entry = next(c for c in out["checks"] if c["name"] == "multi")
        assert entry["description"] == "D" * 200
        assert entry["output_data_type"] == "bool"
