"""Unit tests for scenario management tools."""

import json
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

_PATCH_GET_CLIENT = "src.tools.scenarios.get_okareo_client"
_PATCH_RESOLVE_PROJECT = "src.tools.scenarios.resolve_project_id"


def _register_and_get_tools():
    """Register scenario tools on a mock MCP and return the tool functions."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")

    from src.tools.scenarios import register_tools
    register_tools(mcp)

    tools = {}
    for name, tool in mcp._tool_manager._tools.items():
        tools[name] = tool.fn
    return tools


@pytest.fixture
def tools():
    return _register_and_get_tools()


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setenv("OKAREO_API_KEY", "test-api-key-12345")


@pytest.fixture
def mock_get_scenarios():
    """Mock the get_scenario_sets openapi submodule on its parent package.

    Using patch.object on the parent (rather than patching sys.modules) is
    robust to whether the submodule has been imported elsewhere in the
    session — a `from X import Y` for a submodule Y permanently binds Y
    onto X.__dict__ at first import.
    """
    from okareo_api_client.api import default as _default_pkg

    mock_module = MagicMock()
    with patch.object(
        _default_pkg,
        "get_scenario_sets_v0_scenario_sets_get",
        mock_module,
        create=True,
    ):
        yield mock_module


def _make_mock_scenario_response(
    scenario_id="test-uuid-123",
    name="test-scenario",
    scenario_count=0,
    project_id="proj-uuid-456",
    tags=None,
    time_created="2026-03-05T10:00:00Z",
):
    """Create a mock ScenarioSetResponse."""
    resp = MagicMock()
    resp.scenario_id = scenario_id
    resp.name = name
    resp.scenario_count = scenario_count
    resp.project_id = project_id
    resp.tags = tags if tags is not None else []
    resp.time_created = time_created
    resp.app_link = "https://app.okareo.com/scenario/test-uuid-123"
    return resp


class TestListScenariosLimit:
    """T030: list_scenarios respects the limit parameter and sorts by created_date descending."""

    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_default_limit_returns_20(self, mock_resolve, mock_get_client, tools, mock_get_scenarios):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        # Create 30 scenarios with sequential timestamps
        scenarios = [
            _make_mock_scenario_response(
                scenario_id=f"uuid-{i:03d}",
                name=f"scenario-{i:03d}",
                time_created=f"2026-03-{i+1:02d}T00:00:00Z",
            )
            for i in range(30)
        ]
        mock_get_scenarios.sync.return_value = scenarios

        result = json.loads(tools["list_scenarios"]())

        assert result["count"] == 20
        assert len(result["scenarios"]) == 20
        # Most recent first (scenario-029 has latest date)
        assert result["scenarios"][0]["name"] == "scenario-029"

    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_custom_limit(self, mock_resolve, mock_get_client, tools, mock_get_scenarios):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        scenarios = [
            _make_mock_scenario_response(
                scenario_id=f"uuid-{i}",
                name=f"scenario-{i}",
                time_created=f"2026-03-{i+1:02d}T00:00:00Z",
            )
            for i in range(30)
        ]
        mock_get_scenarios.sync.return_value = scenarios

        result = json.loads(tools["list_scenarios"](limit=5))

        assert result["count"] == 5
        assert len(result["scenarios"]) == 5

    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_limit_zero_returns_all(self, mock_resolve, mock_get_client, tools, mock_get_scenarios):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        scenarios = [
            _make_mock_scenario_response(
                scenario_id=f"uuid-{i}",
                name=f"scenario-{i}",
                time_created=f"2026-03-{i+1:02d}T00:00:00Z",
            )
            for i in range(30)
        ]
        mock_get_scenarios.sync.return_value = scenarios

        result = json.loads(tools["list_scenarios"](limit=0))

        assert result["count"] == 30
        assert len(result["scenarios"]) == 30


class TestListScenariosResponseShape:
    """T031: list_scenarios returns only the specified summary fields."""

    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_response_has_correct_fields(self, mock_resolve, mock_get_client, tools, mock_get_scenarios):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_get_scenarios.sync.return_value = [
            _make_mock_scenario_response(
                scenario_id="uuid-1",
                name="my-scenario",
                scenario_count=5,
                project_id="proj-uuid-456",
                tags=["qa", "v1"],
                time_created="2026-03-05T10:00:00Z",
            )
        ]

        result = json.loads(tools["list_scenarios"]())
        scenario = result["scenarios"][0]

        # Required fields present
        assert scenario["name"] == "my-scenario"
        assert scenario["id"] == "uuid-1"
        assert scenario["project_id"] == "proj-uuid-456"
        assert scenario["tags"] == ["qa", "v1"]
        assert scenario["row_count"] == 5
        assert scenario["created_date"] == "2026-03-05T10:00:00Z"

        # Old fields absent
        assert "scenario_id" not in scenario
        assert "scenario_count" not in scenario
        assert "time_created" not in scenario
        assert "app_link" not in scenario


class TestSaveScenarioTags:
    """T032: save_scenario accepts tags and calls update_scenario_set."""

    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_tags_triggers_update(self, mock_resolve, mock_get_client, tools, mock_get_scenarios):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_get_scenarios.sync.return_value = []

        mock_result = _make_mock_scenario_response(scenario_count=0)
        mock_client.create_scenario_set.return_value = mock_result

        # Mock the update endpoint
        with patch.dict(sys.modules, {
            "okareo_api_client.api.default.update_scenario_set_v0_scenario_sets_scenario_id_put": MagicMock(),
        }) as patched:
            update_mod = patched["okareo_api_client.api.default.update_scenario_set_v0_scenario_sets_scenario_id_put"]

            rows = [{"input": "q1", "result": "a1"}]
            result = json.loads(tools["save_scenario"](name="tagged", rows=rows, tags=["qa", "v1"]))

            assert result["tags"] == ["qa", "v1"]
            update_mod.sync.assert_called_once()

    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_no_tags_skips_update(self, mock_resolve, mock_get_client, tools, mock_get_scenarios):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_get_scenarios.sync.return_value = []

        mock_result = _make_mock_scenario_response(scenario_count=0)
        mock_client.create_scenario_set.return_value = mock_result

        rows = [{"input": "q1", "result": "a1"}]
        result = json.loads(tools["save_scenario"](name="untagged", rows=rows))

        assert result["tags"] == []


class TestSaveScenarioResponseShape:
    """T033: save_scenario returns the ScenarioSummary shape."""

    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_response_has_correct_fields(self, mock_resolve, mock_get_client, tools, mock_get_scenarios):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_get_scenarios.sync.return_value = []

        mock_result = _make_mock_scenario_response(
            scenario_id="new-uuid",
            name="test-shape",
            project_id="proj-uuid-456",
            time_created="2026-03-07T12:00:00Z",
        )
        mock_client.create_scenario_set.return_value = mock_result

        rows = [{"input": "q1", "result": "a1"}]
        result = json.loads(tools["save_scenario"](name="test-shape", rows=rows))

        # Required fields present
        assert result["name"] == "test-shape"
        assert result["id"] == "new-uuid"
        assert result["project_id"] == "proj-uuid-456"
        assert result["tags"] == []
        assert result["row_count"] == 1
        assert result["created_date"] == "2026-03-07T12:00:00Z"
        assert result["created"] is True

        # Old fields absent
        assert "scenario_id" not in result
        assert "app_link" not in result


class TestSaveScenarioRowCountFileUpload:
    """T022: save_scenario with file_path always uses file line count, not API scenario_count."""

    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_row_count_from_file_ignores_api_zero(self, mock_resolve, mock_get_client, tools, mock_get_scenarios):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_get_scenarios.sync.return_value = []

        mock_result = _make_mock_scenario_response(scenario_count=0)
        mock_client.upload_scenario_set.return_value = mock_result

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"input": "q1", "result": "a1"}\n')
            f.write('{"input": "q2", "result": "a2"}\n')
            f.write('{"input": "q3", "result": "a3"}\n')
            tmp_path = f.name

        result = json.loads(tools["save_scenario"](name="test-file", file_path=tmp_path))

        assert result["row_count"] == 3
        assert result["created"] is True

    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_row_count_from_file_ignores_api_nonzero(self, mock_resolve, mock_get_client, tools, mock_get_scenarios):
        """Even if API returns a non-zero scenario_count, file line count is used."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_get_scenarios.sync.return_value = []

        mock_result = _make_mock_scenario_response(scenario_count=999)
        mock_client.upload_scenario_set.return_value = mock_result

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"input": "q1", "result": "a1"}\n')
            f.write('{"input": "q2", "result": "a2"}\n')
            tmp_path = f.name

        result = json.loads(tools["save_scenario"](name="test-file-2", file_path=tmp_path))

        assert result["row_count"] == 2


class TestSaveScenarioRowCountInlineRows:
    """T023: save_scenario with inline rows always uses len(rows), not API scenario_count."""

    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_row_count_from_rows_ignores_api(self, mock_resolve, mock_get_client, tools, mock_get_scenarios):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_get_scenarios.sync.return_value = []

        mock_result = _make_mock_scenario_response(scenario_count=42)
        mock_client.create_scenario_set.return_value = mock_result

        rows = [
            {"input": "q1", "result": "a1"},
            {"input": "q2", "result": "a2"},
            {"input": "q3", "result": "a3"},
            {"input": "q4", "result": "a4"},
        ]

        result = json.loads(tools["save_scenario"](name="test-inline", rows=rows))

        assert result["row_count"] == 4
        assert result["created"] is True


class TestCreateScenarioVersionRowCount:
    """T024: create_scenario_version always uses len(rows), not API scenario_count."""

    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_row_count_ignores_api(self, mock_resolve, mock_get_client, tools, mock_get_scenarios):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        existing = _make_mock_scenario_response(name="my-test", scenario_count=5)
        mock_get_scenarios.sync.return_value = [existing]

        mock_result = _make_mock_scenario_response(name="my-test-v2", scenario_count=0)
        mock_client.create_scenario_set.return_value = mock_result

        rows = [
            {"input": "q1", "result": "a1"},
            {"input": "q2", "result": "a2"},
            {"input": "q3", "result": "a3"},
        ]

        result = json.loads(tools["create_scenario_version"](base_name="my-test", rows=rows))

        assert result["row_count"] == 3
        assert result["name"] == "my-test-v2"
