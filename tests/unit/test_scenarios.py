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


def _jsonl(n, start=0):
    """Build JSONL text with n input/result rows."""
    return "".join(
        json.dumps({"input": f"q{i}", "result": f"a{i}"}) + "\n"
        for i in range(start, start + n)
    )


_PATCH_UPLOAD_BYTES = "src.tools.scenarios._upload_scenario_from_bytes"


class TestSaveScenarioContentPath:
    """US1 (T005): hosted `content` upload path + 2,000-row threshold guard."""

    @patch(_PATCH_UPLOAD_BYTES)
    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_content_uploads_via_bytes_with_row_count(
        self, mock_resolve, mock_get_client, mock_upload, tools, mock_get_scenarios
    ):
        mock_get_client.return_value = MagicMock()
        mock_get_scenarios.sync.return_value = []
        mock_upload.return_value = _make_mock_scenario_response(scenario_count=0)

        result = json.loads(tools["save_scenario"](name="from-content", content=_jsonl(500)))

        assert result["created"] is True
        assert result["row_count"] == 500
        # Uploaded via the in-memory BytesIO path with the raw JSONL bytes.
        mock_upload.assert_called_once()
        _okareo, name_arg, data_arg, _project = mock_upload.call_args[0]
        assert name_arg == "from-content"
        assert isinstance(data_arg, bytes)
        assert data_arg.count(b"\n") == 500

    @patch(_PATCH_UPLOAD_BYTES)
    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_content_at_threshold_is_rejected(
        self, mock_resolve, mock_get_client, mock_upload, tools, mock_get_scenarios
    ):
        mock_get_client.return_value = MagicMock()
        mock_get_scenarios.sync.return_value = []

        result = json.loads(tools["save_scenario"](name="too-big", content=_jsonl(2000)))

        assert "error" in result
        assert "2000" in result["error"]
        assert "upload it directly to Okareo" in result["error"]
        mock_upload.assert_not_called()

    @patch(_PATCH_UPLOAD_BYTES)
    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_content_just_under_threshold_succeeds(
        self, mock_resolve, mock_get_client, mock_upload, tools, mock_get_scenarios
    ):
        mock_get_client.return_value = MagicMock()
        mock_get_scenarios.sync.return_value = []
        mock_upload.return_value = _make_mock_scenario_response(scenario_count=0)

        result = json.loads(tools["save_scenario"](name="just-ok", content=_jsonl(1999)))

        assert result["created"] is True
        assert result["row_count"] == 1999
        mock_upload.assert_called_once()


class TestSaveScenarioGuidanceAndErrors:
    """US2 (T010): actionable errors, mode gating, and byte cap."""

    @patch(_PATCH_UPLOAD_BYTES)
    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_malformed_line_rejected(
        self, mock_resolve, mock_get_client, mock_upload, tools, mock_get_scenarios
    ):
        mock_get_client.return_value = MagicMock()
        mock_get_scenarios.sync.return_value = []

        bad = _jsonl(2) + "not json at all\n"
        result = json.loads(tools["save_scenario"](name="bad", content=bad))

        assert "error" in result
        assert "line 3" in result["error"]
        mock_upload.assert_not_called()

    @patch(_PATCH_UPLOAD_BYTES)
    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_non_object_line_rejected(
        self, mock_resolve, mock_get_client, mock_upload, tools, mock_get_scenarios
    ):
        mock_get_client.return_value = MagicMock()
        mock_get_scenarios.sync.return_value = []

        result = json.loads(tools["save_scenario"](name="arr", content='[1, 2, 3]\n'))

        assert "error" in result
        assert "not a JSON object" in result["error"]
        mock_upload.assert_not_called()

    @patch(_PATCH_UPLOAD_BYTES)
    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_empty_content_rejected(
        self, mock_resolve, mock_get_client, mock_upload, tools, mock_get_scenarios
    ):
        mock_get_client.return_value = MagicMock()
        mock_get_scenarios.sync.return_value = []

        result = json.loads(tools["save_scenario"](name="empty", content="   \n  \n"))

        assert "error" in result
        assert "No rows found" in result["error"]
        mock_upload.assert_not_called()

    def test_file_path_rejected_in_http_mode(self, tools, monkeypatch):
        monkeypatch.setenv("TRANSPORT", "streamable-http")

        result = json.loads(tools["save_scenario"](name="hosted", file_path="/tmp/whatever.jsonl"))

        assert "error" in result
        assert "hosted server" in result["error"]
        assert "File not found" not in result["error"]
        assert "content" in result["error"]

    @patch(_PATCH_UPLOAD_BYTES)
    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_content_over_byte_cap_rejected(
        self, mock_resolve, mock_get_client, mock_upload, tools, mock_get_scenarios
    ):
        mock_get_client.return_value = MagicMock()
        mock_get_scenarios.sync.return_value = []

        from src.tools.scenarios import MAX_INLINE_BYTES

        oversized = "x" * (MAX_INLINE_BYTES + 1)
        result = json.loads(tools["save_scenario"](name="huge", content=oversized))

        assert "error" in result
        assert "inline size limit" in result["error"]
        mock_upload.assert_not_called()


class TestSaveScenarioNoRegression:
    """US3 (T014): stdio file_path and inline rows behave as before."""

    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_stdio_file_path_uploads_unchanged(
        self, mock_resolve, mock_get_client, tools, mock_get_scenarios, monkeypatch
    ):
        monkeypatch.setenv("TRANSPORT", "stdio")
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_get_scenarios.sync.return_value = []
        mock_client.upload_scenario_set.return_value = _make_mock_scenario_response(scenario_count=0)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"input": "q1", "result": "a1"}\n')
            f.write('{"input": "q2", "result": "a2"}\n')
            tmp_path = f.name

        result = json.loads(tools["save_scenario"](name="stdio-file", file_path=tmp_path))

        assert result["row_count"] == 2
        assert result["created"] is True
        mock_client.upload_scenario_set.assert_called_once()

    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_rows_path_unchanged(
        self, mock_resolve, mock_get_client, tools, mock_get_scenarios
    ):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_get_scenarios.sync.return_value = []
        mock_client.create_scenario_set.return_value = _make_mock_scenario_response(scenario_count=0)

        rows = [{"input": "q1", "result": "a1"}, {"input": "q2", "result": "a2"}]
        result = json.loads(tools["save_scenario"](name="rows-only", rows=rows))

        assert result["row_count"] == 2
        mock_client.create_scenario_set.assert_called_once()


class TestSaveScenarioCardinalityAndIdempotency:
    """Polish (T016): single-source rejection and idempotent name hit."""

    def test_no_source_rejected(self, tools):
        result = json.loads(tools["save_scenario"](name="none"))
        assert "error" in result
        assert "exactly one" in result["error"]

    def test_multiple_sources_rejected(self, tools):
        result = json.loads(tools["save_scenario"](
            name="two", content=_jsonl(1), rows=[{"input": "q", "result": "a"}]
        ))
        assert "error" in result
        assert "only one" in result["error"]

    @patch(_PATCH_UPLOAD_BYTES)
    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_existing_name_is_idempotent_no_upload(
        self, mock_resolve, mock_get_client, mock_upload, tools, mock_get_scenarios
    ):
        mock_get_client.return_value = MagicMock()
        existing = _make_mock_scenario_response(name="dupe", scenario_count=7)
        mock_get_scenarios.sync.return_value = [existing]

        result = json.loads(tools["save_scenario"](name="dupe", content=_jsonl(3)))

        assert result["created"] is False
        assert result["row_count"] == 7
        mock_upload.assert_not_called()


class TestSaveScenarioModeSpecificSchema:
    """FR-014 / SC-008: the hosted registration must not advertise file_path;
    the stdio registration must keep it."""

    def _registered_tool(self, monkeypatch, transport):
        from mcp.server.fastmcp import FastMCP

        from src.tools.scenarios import register_tools

        monkeypatch.setenv("TRANSPORT", transport)
        mcp = FastMCP("test")
        register_tools(mcp)
        return mcp._tool_manager._tools["save_scenario"]

    def test_hosted_schema_has_no_file_path(self, monkeypatch):
        tool = self._registered_tool(monkeypatch, "streamable-http")
        assert set(tool.parameters.get("properties", {})) == {
            "name",
            "content",
            "rows",
            "tags",
        }

    def test_hosted_description_never_mentions_file_path(self, monkeypatch):
        tool = self._registered_tool(monkeypatch, "streamable-http")
        description = tool.description or ""
        assert "file_path" not in description
        assert "file path" not in description.lower()

    def test_stdio_schema_still_offers_file_path(self, monkeypatch):
        tool = self._registered_tool(monkeypatch, "stdio")
        assert "file_path" in tool.parameters.get("properties", {})


class TestSaveScenarioHostedDefensiveRejection:
    """FR-014 / FR-010b: a hosted client that sends file_path anyway gets
    actionable feed-rows-directly guidance, and no upload is attempted."""

    def _hosted_tool(self, monkeypatch):
        from mcp.server.fastmcp import FastMCP

        from src.tools.scenarios import register_tools

        monkeypatch.setenv("TRANSPORT", "streamable-http")
        mcp = FastMCP("test")
        register_tools(mcp)
        return mcp._tool_manager._tools["save_scenario"]

    @patch(_PATCH_UPLOAD_BYTES)
    def test_file_path_argument_is_dropped_and_rejected_with_guidance(
        self, mock_upload, monkeypatch
    ):
        tool = self._hosted_tool(monkeypatch)
        # Mimic the MCP call path: pydantic validates the arguments against
        # the hosted schema (extra="ignore" drops the unknown file_path).
        validated = tool.fn_metadata.arg_model.model_validate(
            {"name": "from-old-client", "file_path": "/tmp/data.jsonl"}
        )
        passed = validated.model_dump_one_level()
        assert "file_path" not in passed

        result = json.loads(tool.fn(**passed))

        assert "error" in result
        assert "not supported on the hosted server" in result["error"]
        assert "content" in result["error"]
        assert "upload it directly to Okareo" in result["error"]
        mock_upload.assert_not_called()

    @patch(_PATCH_UPLOAD_BYTES)
    def test_impl_backstop_rejects_file_path_in_http_mode(
        self, mock_upload, monkeypatch
    ):
        from src.tools.scenarios import _save_scenario_impl

        monkeypatch.setenv("TRANSPORT", "streamable-http")
        result = json.loads(
            _save_scenario_impl(name="direct", file_path="/tmp/data.jsonl")
        )

        assert "error" in result
        assert "not available on the hosted server" in result["error"]
        mock_upload.assert_not_called()


class TestSaveScenarioStdioRegression:
    """FR-009: the stdio surface and guidance are unchanged by the hosted
    file_path removal."""

    def test_stdio_zero_source_error_lists_all_three_sources(
        self, tools, monkeypatch
    ):
        monkeypatch.setenv("TRANSPORT", "stdio")
        result = json.loads(tools["save_scenario"](name="none"))
        assert "content" in result["error"]
        assert "file_path" in result["error"]
        assert "rows" in result["error"]

    @patch(_PATCH_GET_CLIENT)
    @patch(_PATCH_RESOLVE_PROJECT, return_value="proj-123")
    def test_hosted_rows_path_still_uses_create_scenario_set(
        self, mock_resolve, mock_get_client, mock_get_scenarios, monkeypatch
    ):
        from mcp.server.fastmcp import FastMCP

        from src.tools.scenarios import register_tools

        monkeypatch.setenv("TRANSPORT", "streamable-http")
        mcp = FastMCP("test")
        register_tools(mcp)
        hosted_save = mcp._tool_manager._tools["save_scenario"].fn

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_get_scenarios.sync.return_value = []
        mock_client.create_scenario_set.return_value = _make_mock_scenario_response(
            scenario_count=0
        )

        rows = [{"input": "q1", "result": "a1"}]
        result = json.loads(hosted_save(name="hosted-rows", rows=rows))

        assert result["row_count"] == 1
        mock_client.create_scenario_set.assert_called_once()
