"""Unit tests for the project tools (036-project-scoping).

The two tools here are read-only by design. 036's FR-025 ("no tool creates a
Project") was superseded 2026-08-19 by 037-project-clone: ``clone_project``
creates the destination Project as part of cloning, and the guard below now
holds the *new* boundary — clone_project is the ONLY Project-creating tool,
and the project tools themselves stay read-only.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from src.okareo_client import _reset_for_tests

GLOBAL_ID = "11111111-1111-4111-8111-111111111111"
BILLING_ID = "22222222-2222-4222-8222-222222222222"


def _project(pid: str, name: str):
    p = MagicMock()
    p.id = pid
    p.name = name
    return p


ONLY_GLOBAL = [_project(GLOBAL_ID, "Global")]
MULTI = [_project(GLOBAL_ID, "Global"), _project(BILLING_ID, "Billing Agent")]


def _tools():
    from src.tools.projects import register_tools

    mcp = FastMCP("test")
    register_tools(mcp)
    return {n: t.fn for n, t in mcp._tool_manager._tools.items()}


def _okareo(projects):
    okareo = MagicMock()
    okareo.api_key = "test-key"
    okareo.get_projects.return_value = list(projects)
    return okareo


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    _reset_for_tests()
    monkeypatch.setenv("OKAREO_API_KEY", "test-key")
    monkeypatch.delenv("OKAREO_PROJECT", raising=False)
    monkeypatch.delenv("TRANSPORT", raising=False)
    yield
    _reset_for_tests()


class TestListProjects:
    def test_lists_every_project_with_id_and_name(self):
        """FR-024: the id is what a co-pilot should persist — it survives a rename."""
        with patch("src.tools.projects.get_okareo_client", return_value=_okareo(MULTI)):
            out = json.loads(_tools()["list_projects"]())
        assert out["count"] == 2
        for entry in out["projects"]:
            assert entry["id"] and entry["name"]
        assert {e["name"] for e in out["projects"]} == {"Global", "Billing Agent"}

    def test_marks_the_active_project_and_states_the_basis(self):
        with patch("src.tools.projects.get_okareo_client", return_value=_okareo(ONLY_GLOBAL)):
            out = json.loads(_tools()["list_projects"]())
        assert out["active"] == {
            "id": GLOBAL_ID, "name": "Global", "basis": "default",
        }
        assert [e["active"] for e in out["projects"]] == [True]

    def test_succeeds_with_null_active_when_nothing_resolves(self):
        """A discovery tool must work precisely when the user cannot choose yet."""
        with patch("src.tools.projects.get_okareo_client", return_value=_okareo(MULTI)):
            out = json.loads(_tools()["list_projects"]())
        assert out["active"] is None
        assert out["count"] == 2
        assert "error" not in out

    def test_names_where_projects_are_created(self):
        """FR-026: the co-pilot must be able to answer 'then make me one'."""
        with patch("src.tools.projects.get_okareo_client", return_value=_okareo(MULTI)):
            out = json.loads(_tools()["list_projects"]())
        assert "Okareo web application" in out["note"]

    def test_takes_no_project_parameter(self):
        from src.tools.projects import register_tools

        mcp = FastMCP("test")
        register_tools(mcp)
        props = mcp._tool_manager._tools["list_projects"].parameters["properties"]
        assert props == {}


class TestSelectProject:
    def test_validates_against_the_live_list(self):
        with patch("src.tools.projects.get_okareo_client", return_value=_okareo(MULTI)):
            out = json.loads(_tools()["select_project"](project="Billing Agent"))
        assert out["project"]["id"] == BILLING_ID

    def test_unknown_project_reports_not_found_with_the_options(self):
        with patch("src.tools.projects.get_okareo_client", return_value=_okareo(MULTI)):
            out = json.loads(_tools()["select_project"](project="Nope"))
        assert out["error"]["code"] == "project_not_found"
        assert len(out["error"]["projects"]) == 2

    def test_directs_the_copilot_to_remember_the_choice(self):
        """FR-009a: SHOULD-strength — a directive, not a hint."""
        with patch("src.tools.projects.get_okareo_client", return_value=_okareo(MULTI)):
            out = json.loads(_tools()["select_project"](project="Billing Agent"))
        assert "future conversations" in out["remember"]
        assert "Billing Agent" in out["remember"]

    def test_never_claims_the_server_saved_the_selection(self):
        """FR-009a's other half: the directive must not become a false promise."""
        with patch("src.tools.projects.get_okareo_client", return_value=_okareo(MULTI)):
            out = json.loads(_tools()["select_project"](project="Billing Agent"))
        assert out["applies_to"] == "this conversation"
        blob = json.dumps(out).lower()
        for lie in ("saved", "stored", "persisted", "will remember"):
            assert lie not in blob, f"response implies durability: {lie!r}"
        assert "does not remember" in out["remember"]

    def test_tells_the_copilot_to_thread_the_project(self):
        with patch("src.tools.projects.get_okareo_client", return_value=_okareo(MULTI)):
            out = json.loads(_tools()["select_project"](project="Billing Agent"))
        assert "project=" in out["instruction"]

    def test_offers_the_pin_as_the_permanent_route(self):
        with patch("src.tools.projects.get_okareo_client", return_value=_okareo(MULTI)):
            out = json.loads(_tools()["select_project"](project="Billing Agent"))
        assert "OKAREO_PROJECT" in out["make_permanent"]

    def test_refuses_on_a_pinned_connection(self, monkeypatch):
        """FR-014: never accept a selection the pin would override."""
        monkeypatch.setenv("OKAREO_PROJECT", "Global")
        with patch("src.tools.projects.get_okareo_client", return_value=_okareo(MULTI)):
            out = json.loads(_tools()["select_project"](project="Billing Agent"))
        assert out["error"]["code"] == "project_misconfigured"
        assert "pinned" in out["error"]["message"]
        assert "connection configuration" in out["error"]["message"]

    def test_selection_does_not_create_a_project(self):
        """FR-011: selection never creates as a side effect."""
        okareo = _okareo(MULTI)
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            _tools()["select_project"](project="Billing Agent")
        assert not okareo.create_project.called


class TestProjectCreationBoundary:
    """036 FR-025, superseded by 037 FR-006: clone_project is the ONLY
    Project-creating tool. A negative requirement needs an explicit guard."""

    def test_clone_project_is_the_only_project_creating_tool(self):
        from src.server import mcp

        names = set(mcp._tool_manager._tools)
        assert "clone_project" in names, (
            "037 FR-006: clone_project is the one sanctioned Project-creating "
            "tool and must be registered."
        )
        forbidden = {
            "create_project", "new_project", "add_project", "make_project",
            "delete_project", "archive_project", "rename_project",
            "update_project", "unarchive_project",
        }
        assert not (names & forbidden), (
            "Ad-hoc Project lifecycle belongs to the Okareo web application "
            "(036 FR-025 as narrowed by 037): cloning creates its own "
            "destination, nothing else creates or mutates Projects."
        )

    def test_only_the_clone_module_calls_the_sdk_create_project(self):
        """Structural half of the boundary: no other tool module may grow a
        create_project call without this test noticing."""
        from pathlib import Path

        tools_dir = Path(__file__).resolve().parents[2] / "src" / "tools"
        offenders = sorted(
            f.name
            for f in tools_dir.glob("*.py")
            if f.name != "clone.py" and "create_project(" in f.read_text()
        )
        assert offenders == [], (
            f"create_project called outside src/tools/clone.py: {offenders}"
        )

    def test_project_tool_surface_is_exactly_two_read_only_tools(self):
        from src.tools.projects import register_tools

        mcp = FastMCP("test")
        register_tools(mcp)
        assert set(mcp._tool_manager._tools) == {"list_projects", "select_project"}

    def test_no_code_path_calls_the_sdk_create_project(self):
        okareo = _okareo(MULTI)
        tools = _tools()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            tools["list_projects"]()
            tools["select_project"](project="Billing Agent")
        assert not okareo.create_project.called
        assert not okareo.update_project.called
        assert not okareo.archive_project.called
