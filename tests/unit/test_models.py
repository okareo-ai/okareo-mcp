"""Unit tests for model management tools."""

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register_and_get_tools():
    """Register model tools on a mock MCP and return the tool functions."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")

    from src.tools.models import register_tools
    register_tools(mcp)

    # Extract registered tool functions from the MCP internal registry
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


# ---------------------------------------------------------------------------
# T009: Tool renames — verify new names registered, old names absent
# ---------------------------------------------------------------------------

class TestToolRenames:
    """Verify FR-020 tool renames are reflected in MCP registration."""

    NEW_TOOL_NAMES = [
        "list_available_llms",
        "register_generation_model",
        "list_generation_models",
        "get_generation_model",
        "update_generation_model",
        "delete_generation_model",
    ]

    OLD_TOOL_NAMES = [
        "list_available_models",
        "register_model",
        "list_models",
        "get_model",
        "update_model",
        "delete_model",
    ]

    def test_new_tool_names_registered(self, tools):
        """All 6 new tool names are registered in the MCP server."""
        for name in self.NEW_TOOL_NAMES:
            assert name in tools, f"Expected tool '{name}' to be registered"

    def test_old_tool_names_not_registered(self, tools):
        """None of the 6 old tool names are registered."""
        for name in self.OLD_TOOL_NAMES:
            assert name not in tools, f"Old tool name '{name}' should not be registered"

    def test_exactly_six_tools_registered(self, tools):
        """Exactly 6 tools are registered from models.py."""
        assert len(tools) == 6


# ---------------------------------------------------------------------------
# 049: the model register_model hands back has its thread stopped
# ---------------------------------------------------------------------------

class TestTheReturnedModelsThreadIsStopped:
    """okareo 0.0.159's ``register_model`` hands back a ``ModelUnderTest``
    whose datapoint thread started when it was built; the tools stop it once
    they have read the model (049 FR-010, research R15)."""

    @pytest.mark.parametrize(
        "tool", ["register_generation_model", "update_generation_model"]
    )
    def test_the_tool_stops_the_models_thread(self, tools, tool, stand_in_model):
        import json
        from types import SimpleNamespace
        from unittest.mock import patch

        from src.okareo_client import ResolvedProject

        built = []

        def register_model(**kwargs):
            built.append(stand_in_model())
            return built[-1]

        okareo = SimpleNamespace(register_model=register_model, api_key="k")
        with patch("src.tools.models.get_okareo_client", return_value=okareo), \
             patch(
                 "src.tools.models.resolve_project",
                 return_value=ResolvedProject(id="p1", name="Global", basis="default"),
             ), \
             patch(
                 "src.tools.models._get_available_models",
                 return_value=[SimpleNamespace(model_name="azure/gpt-4o-mini")],
             ), \
             patch("src.tools.models.resolve_artifact_by_name", return_value=object()):
            out = json.loads(tools[tool](name="m", model_name="azure/gpt-4o-mini"))

        assert "error" not in out
        assert len(built) == 1
        assert not built[0].worker_thread.is_alive()
