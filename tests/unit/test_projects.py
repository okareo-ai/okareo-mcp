"""Unit tests for the project tools (036-project-scoping, 040-project-lifecycle-tools).

Six tools: two read (``list_projects``, ``select_project``) and four write
(``create_project``, ``update_project``, ``archive_project``,
``unarchive_project``).

036's FR-025 ("no tool creates a Project") and FR-026 ("say Projects are
created in the Okareo web application") were narrowed 2026-08-19 by
037-project-clone and retired 2026-08-26 by 040. The boundary guard below
holds what survived that revision: there is **no Project delete at any name**,
and the read tools stay read.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from src.okareo_client import _reset_for_tests

GLOBAL_ID = "11111111-1111-4111-8111-111111111111"
BILLING_ID = "22222222-2222-4222-8222-222222222222"


def _project(pid: str, name: str, tags=None, is_archived: bool = False):
    """A Project as the pinned SDK hands it back.

    ``is_archived`` goes in ``additional_properties`` on purpose: the pinned
    SDK's generated ``ProjectResponse`` does not type it, and the tools read
    it from there (040 research R2).
    """
    p = MagicMock()
    p.id = pid
    p.name = name
    p.tags = list(tags) if tags else []
    p.is_archived = None  # not a typed field on the generated model
    p.additional_properties = {"is_archived": is_archived}
    return p


def _regenerated_project(pid: str, name: str, is_archived: bool = False):
    """A Project as a *regenerated* client would hand it back.

    `is_archived` typed on the model, nothing in `additional_properties`. This
    is the forward-compatibility case `_is_archived` exists to survive; without
    a fixture for it, that branch is never exercised.
    """
    p = MagicMock()
    p.id = pid
    p.name = name
    p.tags = []
    p.is_archived = is_archived
    p.additional_properties = {}
    return p


ONLY_GLOBAL = [_project(GLOBAL_ID, "Global")]
MULTI = [_project(GLOBAL_ID, "Global"), _project(BILLING_ID, "Billing Agent")]
WITH_ARCHIVED = [
    _project(GLOBAL_ID, "Global"),
    _project(BILLING_ID, "Billing Agent", tags=["2026"], is_archived=True),
]


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

    def test_reads_archive_state_from_a_regenerated_client_too(self):
        """`is_archived` is untyped on the pinned SDK and lands in
        additional_properties; a regenerated client would type it. The reader
        must survive that without a code change, so exercise both shapes."""
        with patch(
            "src.tools.projects.get_okareo_client",
            return_value=_okareo([
                _regenerated_project(GLOBAL_ID, "Global"),
                _regenerated_project(BILLING_ID, "Billing Agent", is_archived=True),
            ]),
        ):
            out = json.loads(_tools()["list_projects"]())
        rows = {e["name"]: e for e in out["projects"]}
        assert rows["Global"]["is_archived"] is False
        assert rows["Billing Agent"]["is_archived"] is True

    def test_carries_no_creation_note(self):
        """040 FR-012 retired 036 FR-026: a listing that reports lifecycle
        state does not need a footnote saying lifecycle happens elsewhere.
        Pins the removal — a deleted field needs a guard like an added one."""
        with patch("src.tools.projects.get_okareo_client", return_value=_okareo(MULTI)):
            out = json.loads(_tools()["list_projects"]())
        assert "note" not in out

    def test_reports_tags_and_archive_state_per_project(self):
        """040 FR-005: archive and unarchive are unusable without this."""
        with patch(
            "src.tools.projects.get_okareo_client",
            return_value=_okareo(WITH_ARCHIVED),
        ):
            out = json.loads(_tools()["list_projects"]())
        rows = {e["name"]: e for e in out["projects"]}
        assert rows["Global"]["is_archived"] is False
        assert rows["Global"]["tags"] == []
        assert rows["Billing Agent"]["is_archived"] is True
        assert rows["Billing Agent"]["tags"] == ["2026"]

    def test_lists_archived_projects_too(self):
        """036 FR-024, reaffirmed by 040 FR-006: archiving hides, in the Okareo
        app's picker — the listing hides nothing."""
        with patch(
            "src.tools.projects.get_okareo_client",
            return_value=_okareo(WITH_ARCHIVED),
        ):
            out = json.loads(_tools()["list_projects"]())
        assert out["count"] == 2
        assert "Billing Agent" in {e["name"] for e in out["projects"]}

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


class TestProjectLifecycleBoundary:
    """What survived 036 FR-025 → 037 FR-006 → 039 → 040.

    040 retired the "no tool mutates a Project" rule: four lifecycle tools are
    now required. The negative requirement that remains is the one the
    platform itself enforces — **there is no Project delete anywhere**, so the
    MCP must not grow one, and must not fake one with an archive underneath.
    """

    def test_the_project_tool_surface_is_exactly_these_six(self):
        from src.tools.projects import register_tools

        mcp = FastMCP("test")
        register_tools(mcp)
        assert set(mcp._tool_manager._tools) == {
            "list_projects",
            "select_project",
            "create_project",
            "update_project",
            "archive_project",
            "unarchive_project",
        }

    def test_no_project_delete_tool_at_any_name(self):
        """040 FR-013. Okareo has no Project delete in the UI, the API, or the
        SDK. Archiving is the removal and it is reversible; a delete-shaped
        tool would tell the user their data is gone when it is not."""
        from src.server import mcp

        names = set(mcp._tool_manager._tools)
        forbidden = {
            "delete_project", "remove_project", "destroy_project",
            "drop_project", "purge_project",
        }
        assert not (names & forbidden), (
            "No Project delete exists at any layer of Okareo. Offer "
            "archive_project instead."
        )

    def test_clone_creates_server_side_and_never_calls_the_sdk(self):
        """039's invariant, narrowed to the file it was always about: the
        clone's destination Project is created inside the backend's
        transaction, so clone.py must not call the SDK's create_project.
        (projects.py now does, by design — 040 FR-001.)"""
        from pathlib import Path

        clone = Path(__file__).resolve().parents[2] / "src" / "tools" / "clone.py"
        assert "create_project(" not in clone.read_text(), (
            "clone_project creates its destination server-side (039); calling "
            "the SDK's create_project here would split the transaction."
        )

    def test_the_read_tools_stay_read_only(self):
        okareo = _okareo(MULTI)
        tools = _tools()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            tools["list_projects"]()
            tools["select_project"](project="Billing Agent")
        assert not okareo.create_project.called
        assert not okareo.update_project.called
        assert not okareo.archive_project.called
        assert not okareo.unarchive_project.called


class TestCreateProject:
    NEW_ID = "33333333-3333-4333-8333-333333333333"

    def _okareo_creating(self, name="Billing Agent QA", tags=None):
        okareo = _okareo(MULTI)
        okareo.create_project.return_value = _project(self.NEW_ID, name, tags=tags)
        return okareo

    def test_creates_and_returns_the_new_project(self):
        okareo = self._okareo_creating()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(_tools()["create_project"](name="Billing Agent QA"))
        assert out["created"] is True
        assert out["project"] == {
            "id": self.NEW_ID,
            "name": "Billing Agent QA",
            "tags": [],
            "is_archived": False,
        }
        okareo.create_project.assert_called_once_with(name="Billing Agent QA")

    def test_passes_tags_through_when_given(self):
        okareo = self._okareo_creating(tags=["qa"])
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            json.loads(
                _tools()["create_project"](name="Billing Agent QA", tags=["qa"])
            )
        okareo.create_project.assert_called_once_with(
            name="Billing Agent QA", tags=["qa"]
        )

    def test_does_not_select_the_new_project(self):
        """FR-008: the server is stateless — creating cannot make it active.
        The response hands the id to the co-pilot instead."""
        okareo = self._okareo_creating()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(_tools()["create_project"](name="Billing Agent QA"))
        assert f'project="{self.NEW_ID}"' in out["instruction"]
        assert "future conversations" in out["remember"]
        blob = json.dumps(out).lower()
        for lie in ("now active", "switched", "selected for you"):
            assert lie not in blob, f"response implies state: {lie!r}"

    def test_invalidates_the_listing_cache(self):
        """FR-007: the new Project must resolve on the very next call, not
        after the 60-second TTL."""
        okareo = self._okareo_creating()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo), \
                patch("src.tools.projects.invalidate_projects_cache") as inval:
            _tools()["create_project"](name="Billing Agent QA")
        inval.assert_called_once_with(okareo)

    def test_refuses_a_blank_name_without_calling_the_backend(self):
        okareo = self._okareo_creating()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(_tools()["create_project"](name="   "))
        assert out["error"]["code"] == "project_name_required"
        assert not okareo.create_project.called

    def test_a_name_collision_is_a_validation_refusal_not_an_auth_error(self):
        """The SDK raises TypeError on a backend 400, which classify_error
        reads as an authentication failure — that would send the user to
        check their API key over a duplicate name."""
        okareo = self._okareo_creating()
        okareo.create_project.side_effect = TypeError(
            "error: A project named 'Billing Agent' already exists."
        )
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(_tools()["create_project"](name="Billing Agent"))
        assert out["error"]["code"] == "project_name_taken"
        assert out["error"]["category"] == "validation"
        assert "different name" in out["error"]["suggestion"]

    def test_a_whitespace_name_is_refused_with_the_real_reason(self):
        """The SDK's `_validate_project_name` raises locally, before the
        request, with ITS wording — not the backend's. Assert against the
        string the SDK actually produces, or this test passes while
        production breaks."""
        okareo = self._okareo_creating()
        okareo.create_project.side_effect = ValueError(
            "Project name 'Billing Agent ' has leading or trailing whitespace. "
            "Use 'Billing Agent' instead."
        )
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(_tools()["create_project"](name="Billing Agent "))
        assert out["error"]["code"] == "project_name_invalid"

    def test_the_backends_whitespace_wording_is_handled_too(self):
        """Both wordings reach us — the SDK's local one above, and the
        backend's if a path ever skips the local check. They share only the
        word 'whitespace'."""
        okareo = self._okareo_creating()
        okareo.create_project.side_effect = TypeError(
            "error: Project attribute: 'name' cannot have leading or trailing "
            "whitespace."
        )
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(_tools()["create_project"](name="Billing Agent "))
        assert out["error"]["code"] == "project_name_invalid"

    def test_does_not_invalidate_the_cache_on_failure(self):
        okareo = self._okareo_creating()
        okareo.create_project.side_effect = TypeError("error: already exists")
        with patch("src.tools.projects.get_okareo_client", return_value=okareo), \
                patch("src.tools.projects.invalidate_projects_cache") as inval:
            _tools()["create_project"](name="Billing Agent")
        assert not inval.called


class TestUpdateProject:
    def _okareo_updating(self, name="Billing Agent v2", tags=None):
        okareo = _okareo(WITH_ARCHIVED)
        okareo.update_project.return_value = _project(BILLING_ID, name, tags=tags)
        return okareo

    def test_renames_and_reports_the_previous_name(self):
        okareo = self._okareo_updating()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(
                _tools()["update_project"](
                    project="Billing Agent", name="Billing Agent v2"
                )
            )
        okareo.update_project.assert_called_once_with(
            BILLING_ID, name="Billing Agent v2"
        )
        assert out["updated"] == ["name"]
        assert out["previous"] == {"name": "Billing Agent"}
        assert out["project"]["name"] == "Billing Agent v2"

    def test_tags_only_leaves_the_name_alone(self):
        okareo = self._okareo_updating(name="Billing Agent", tags=["qa"])
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(
                _tools()["update_project"](project="Billing Agent", tags=["qa"])
            )
        okareo.update_project.assert_called_once_with(BILLING_ID, tags=["qa"])
        assert out["updated"] == ["tags"]
        assert out["previous"] == {"tags": ["2026"]}

    def test_clearing_tags_is_an_explicit_empty_list(self):
        okareo = self._okareo_updating(name="Billing Agent", tags=[])
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(
                _tools()["update_project"](project="Billing Agent", tags=[])
            )
        okareo.update_project.assert_called_once_with(BILLING_ID, tags=[])
        assert out["updated"] == ["tags"]

    def test_never_touches_archive_state(self):
        """The backend keeps archive off the general update path on purpose;
        so does this tool."""
        okareo = self._okareo_updating()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            _tools()["update_project"](project="Billing Agent", name="Billing Agent v2")
        kwargs = okareo.update_project.call_args.kwargs
        assert "is_archived" not in kwargs
        assert "archived" not in kwargs

    def test_refuses_an_empty_update(self):
        """FR-010: an empty write is a bug, not a no-op worth issuing."""
        okareo = self._okareo_updating()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(_tools()["update_project"](project="Billing Agent"))
        assert out["error"]["code"] == "project_update_empty"
        assert not okareo.update_project.called

    def test_refuses_a_missing_target(self):
        """FR-004: editing the wrong Project silently is the failure this
        parameter exists to prevent."""
        okareo = self._okareo_updating()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(_tools()["update_project"](project="", name="X"))
        assert out["error"]["code"] == "project_target_required"
        assert not okareo.update_project.called

    def test_ignores_the_connection_pin_when_choosing_the_target(
        self, monkeypatch
    ):
        """FR-004: a pin governs which Project operations act on — it must
        never decide which Project gets renamed."""
        monkeypatch.setenv("OKAREO_PROJECT", "Global")
        okareo = self._okareo_updating()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            _tools()["update_project"](project="Billing Agent", name="Billing Agent v2")
        assert okareo.update_project.call_args.args[0] == BILLING_ID

    def test_unknown_project_reports_not_found(self):
        okareo = self._okareo_updating()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(_tools()["update_project"](project="Nope", name="X"))
        assert out["error"]["code"] == "project_not_found"
        assert not okareo.update_project.called

    def test_a_rename_collision_is_a_terminal_refusal(self):
        okareo = self._okareo_updating()
        okareo.update_project.side_effect = TypeError(
            "error: A project named 'Global' already exists."
        )
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(
                _tools()["update_project"](project="Billing Agent", name="Global")
            )
        assert out["error"]["code"] == "project_name_taken"

    def test_renaming_the_default_project_is_refused_not_an_auth_error(self):
        """Review CR-1. The backend refuses this with 'The default project
        cannot be renamed.' — a guard research R3 missed. Unhandled, it fell
        through to classify_error, which reads a bare TypeError as an
        AUTHENTICATION failure and tells the user to go check their API key
        over an ordinary refusal."""
        okareo = self._okareo_updating()
        okareo.update_project.side_effect = TypeError(
            "error: The default project cannot be renamed."
        )
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(_tools()["update_project"](project="Global", name="Home"))
        assert out["error"]["code"] == "project_rename_refused"
        assert out["error"]["category"] == "validation"

    def test_an_unrecognized_backend_refusal_is_still_a_refusal(self):
        """Review CR-2. The catch-all matters more than any single message:
        a guard added to the backend tomorrow must degrade to 'refused, here
        is why', never to a credentials error."""
        okareo = self._okareo_updating()
        okareo.update_project.side_effect = TypeError(
            "error: Some future Project rule this code has never heard of."
        )
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(
                _tools()["update_project"](project="Billing Agent", name="X")
            )
        assert out["error"]["code"] == "project_request_refused"
        assert out["error"]["category"] == "validation"
        assert "never heard of" in out["error"]["message"]

    def test_a_transport_failure_is_not_dressed_up_as_a_refusal(self):
        """The other half of CR-2: matching on str(exc) alone would let a
        network error whose text happens to contain 'already exists' become a
        terminal do-not-retry name collision."""
        okareo = self._okareo_updating()
        okareo.update_project.side_effect = ConnectionError(
            "connection reset; a project named 'X' already exists in cache"
        )
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(
                _tools()["update_project"](project="Billing Agent", name="X")
            )
        assert out["error"].get("code") != "project_name_taken"
        assert out["error"]["category"] == "connectivity"

    def test_refuses_a_blank_rename(self):
        okareo = self._okareo_updating()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(
                _tools()["update_project"](project="Billing Agent", name="   ")
            )
        assert out["error"]["code"] == "project_name_required"
        assert not okareo.update_project.called

    def test_omits_previous_tags_when_the_prior_value_is_unreadable(self):
        """Review CR-4. data-model.md says `previous` carries the pre-change
        values; an invented [] is not one. Claiming the Project had no tags
        is a definite, possibly false statement the copilot relays."""
        okareo = self._okareo_updating(name="Billing Agent", tags=["qa"])
        with patch("src.tools.projects.get_okareo_client", return_value=okareo), \
                patch("src.tools.projects._find_project", return_value=None):
            out = json.loads(
                _tools()["update_project"](project="Billing Agent", tags=["qa"])
            )
        assert out["updated"] == ["tags"]
        assert "tags" not in out["previous"]

    def test_invalidates_the_listing_cache(self):
        okareo = self._okareo_updating()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo), \
                patch("src.tools.projects.invalidate_projects_cache") as inval:
            _tools()["update_project"](project="Billing Agent", name="Billing Agent v2")
        inval.assert_called_once_with(okareo)


class TestArchiveProject:
    def _okareo_archiving(self, is_archived=True):
        okareo = _okareo(MULTI)
        archived = _project(BILLING_ID, "Billing Agent", is_archived=is_archived)
        okareo.archive_project.return_value = archived
        okareo.unarchive_project.return_value = archived
        return okareo

    def test_archives_and_reports_the_resulting_state(self):
        okareo = self._okareo_archiving()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(_tools()["archive_project"](project="Billing Agent"))
        okareo.archive_project.assert_called_once_with(BILLING_ID)
        assert out["archived"] is True
        assert out["project"]["is_archived"] is True

    def test_says_plainly_that_archiving_is_not_deleting(self):
        """FR-011: the one thing a user can misread about this operation."""
        okareo = self._okareo_archiving()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(_tools()["archive_project"](project="Billing Agent"))
        note = out["note"].lower()
        assert "hides" in note
        assert "intact" in note
        assert "unarchive_project" in out["note"]

    def test_unarchives(self):
        okareo = self._okareo_archiving(is_archived=False)
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(_tools()["unarchive_project"](project="Billing Agent"))
        okareo.unarchive_project.assert_called_once_with(BILLING_ID)
        assert out["archived"] is False
        assert out["project"]["is_archived"] is False

    def test_the_default_project_refusal_names_the_reason(self):
        okareo = self._okareo_archiving()
        okareo.archive_project.side_effect = TypeError(
            "error: The default project cannot be archived."
        )
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(_tools()["archive_project"](project="Global"))
        assert out["error"]["code"] == "project_archive_refused"
        assert out["error"]["category"] == "validation"

    def test_refuses_a_missing_target(self):
        okareo = self._okareo_archiving()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            out = json.loads(_tools()["archive_project"](project=""))
        assert out["error"]["code"] == "project_target_required"
        assert not okareo.archive_project.called

    def test_ignores_the_connection_pin_when_choosing_the_target(
        self, monkeypatch
    ):
        monkeypatch.setenv("OKAREO_PROJECT", "Global")
        okareo = self._okareo_archiving()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo):
            _tools()["archive_project"](project="Billing Agent")
        okareo.archive_project.assert_called_once_with(BILLING_ID)

    def test_invalidates_the_listing_cache(self):
        okareo = self._okareo_archiving()
        with patch("src.tools.projects.get_okareo_client", return_value=okareo), \
                patch("src.tools.projects.invalidate_projects_cache") as inval:
            _tools()["archive_project"](project="Billing Agent")
        inval.assert_called_once_with(okareo)

    def test_an_archived_project_is_still_selectable(self):
        """036 FR-024, reaffirmed by 040 FR-006."""
        with patch(
            "src.tools.projects.get_okareo_client",
            return_value=_okareo(WITH_ARCHIVED),
        ):
            out = json.loads(_tools()["select_project"](project="Billing Agent"))
        assert out["project"]["id"] == BILLING_ID


class TestLifecycleToolAnnotations:
    """FR-014: the behavioral hints must match what the tools actually do.

    The repo-wide metadata audit checks these are *present*; it does not check
    their values. The create/update split (idempotent false vs true) and
    destructiveHint=False on archive are real design claims a client acts on,
    so they get a guard of their own.
    """

    EXPECTED = {
        # tool: (readOnly, destructive, idempotent)
        "list_projects": (True, False, True),
        "select_project": (True, False, True),
        # a repeat create collides on the name, so it is NOT idempotent
        "create_project": (False, False, False),
        "update_project": (False, False, True),
        # archiving deletes nothing and is reversible
        "archive_project": (False, False, True),
        "unarchive_project": (False, False, True),
    }

    def test_annotations_match_what_the_tools_do(self):
        from src.tools.projects import register_tools

        mcp = FastMCP("test")
        register_tools(mcp)
        for name, (read_only, destructive, idempotent) in self.EXPECTED.items():
            ann = mcp._tool_manager._tools[name].annotations
            assert ann is not None, f"{name} has no annotations"
            assert ann.readOnlyHint is read_only, name
            assert ann.destructiveHint is destructive, name
            assert ann.idempotentHint is idempotent, name

    def test_every_lifecycle_tool_has_a_title(self):
        from src.tools.projects import register_tools

        mcp = FastMCP("test")
        register_tools(mcp)
        for name in self.EXPECTED:
            assert mcp._tool_manager._tools[name].title, f"{name} has no title"
