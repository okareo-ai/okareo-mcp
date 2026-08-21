"""End-to-end project resolution against mocked Okareo responses (036).

The centrepiece is User Story 2: organizations holding only Global must see
no behavioral change at all. Every existing organization is in that state on
release day, so the no-op is asserted structurally rather than assumed.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp import FastMCP
from okareo_api_client.errors import UnexpectedStatus

from src.error_handling import (
    ProjectMisconfigured,
    ProjectNotSelected,
)
from src.okareo_client import _reset_for_tests, resolve_project

GLOBAL_ID = "11111111-1111-4111-8111-111111111111"
BILLING_ID = "22222222-2222-4222-8222-222222222222"
SUPPORT_ID = "33333333-3333-4333-8333-333333333333"


def _project(pid: str, name: str):
    p = MagicMock()
    p.id = pid
    p.name = name
    return p


ONLY_GLOBAL = [_project(GLOBAL_ID, "Global")]
MULTI = [
    _project(GLOBAL_ID, "Global"),
    _project(BILLING_ID, "Billing Agent"),
    _project(SUPPORT_ID, "Support Bot"),
]


def _okareo(projects):
    okareo = MagicMock()
    okareo.api_key = "test-key"
    okareo.get_projects.return_value = list(projects)
    return okareo


def _scenario_tools():
    from src.tools.scenarios import register_tools

    mcp = FastMCP("test")
    register_tools(mcp)
    return {n: t.fn for n, t in mcp._tool_manager._tools.items()}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    _reset_for_tests()
    monkeypatch.setenv("OKAREO_API_KEY", "test-key")
    monkeypatch.delenv("OKAREO_PROJECT", raising=False)
    monkeypatch.delenv("TRANSPORT", raising=False)
    yield
    _reset_for_tests()


class TestGlobalOnlyOrganizationIsUntouched:
    """US2 / FR-032 / SC-003 — the guarantee that protects every existing user."""

    def test_resolves_to_global_without_prompting(self):
        okareo = _okareo(ONLY_GLOBAL)
        resolved = resolve_project(okareo)
        assert (resolved.name, resolved.basis) == ("Global", "default")

    def test_costs_exactly_one_project_fetch_per_organization(self):
        """SC-003: no added round trip versus the pre-feature baseline."""
        okareo = _okareo(ONLY_GLOBAL)
        for _ in range(10):
            resolve_project(okareo)
        assert okareo.get_projects.call_count == 1

    def test_full_tool_flow_needs_no_project_argument(self):
        okareo = _okareo(ONLY_GLOBAL)
        tools = _scenario_tools()
        with patch(
            "src.tools.scenarios.get_okareo_client", return_value=okareo
        ), patch(
            "okareo_api_client.api.default"
            ".get_scenario_sets_v0_scenario_sets_get.sync",
            return_value=[],
        ):
            out = json.loads(tools["list_scenarios"]())
        assert "error" not in out
        assert out["project"]["basis"] == "default"

    def test_never_raises_not_selected(self):
        okareo = _okareo(ONLY_GLOBAL)
        for _ in range(3):
            resolve_project(okareo)  # would raise if the org were ambiguous


class TestMultiProjectOrganizationIsNeverGuessed:
    """FR-003 / SC-005 — no silent fallback, ever."""

    def test_unresolved_raises_with_the_full_project_list(self):
        okareo = _okareo(MULTI)
        with pytest.raises(ProjectNotSelected) as exc:
            resolve_project(okareo)
        assert {p["name"] for p in exc.value.projects} == {
            "Global", "Billing Agent", "Support Bot",
        }

    def test_does_not_fall_back_to_global(self):
        okareo = _okareo(MULTI)
        with pytest.raises(ProjectNotSelected):
            resolve_project(okareo)

    def test_tool_surfaces_the_machine_readable_outcome(self):
        """FR-020: the co-pilot must be able to recognize this and ask."""
        okareo = _okareo(MULTI)
        tools = _scenario_tools()
        with patch("src.tools.scenarios.get_okareo_client", return_value=okareo):
            out = json.loads(tools["list_scenarios"]())
        assert out["error"]["code"] == "project_not_selected"
        assert len(out["error"]["projects"]) == 3
        assert "project" not in out  # nothing was acted on

    def test_named_project_proceeds_without_a_prompt(self):
        okareo = _okareo(MULTI)
        tools = _scenario_tools()
        with patch(
            "src.tools.scenarios.get_okareo_client", return_value=okareo
        ), patch(
            "okareo_api_client.api.default"
            ".get_scenario_sets_v0_scenario_sets_get.sync",
            return_value=[],
        ):
            out = json.loads(tools["list_scenarios"](project="Billing Agent"))
        assert out["project"]["id"] == BILLING_ID


class TestPinnedConnection:
    """US4 — the pin governs, and its failure mode is its own."""

    def test_pin_resolves_with_basis_pin(self, monkeypatch):
        monkeypatch.setenv("OKAREO_PROJECT", "Billing Agent")
        resolved = resolve_project(_okareo(MULTI))
        assert (resolved.id, resolved.basis) == (BILLING_ID, "pin")

    def test_per_operation_argument_still_overrides_the_pin(self, monkeypatch):
        """FR-015: US5's quick look elsewhere works on a pinned connection."""
        monkeypatch.setenv("OKAREO_PROJECT", "Billing Agent")
        resolved = resolve_project(_okareo(MULTI), "Support Bot")
        assert (resolved.id, resolved.basis) == (SUPPORT_ID, "explicit")

    def test_override_does_not_persist(self, monkeypatch):
        """FR-015: the next unqualified call returns to the pin."""
        monkeypatch.setenv("OKAREO_PROJECT", "Billing Agent")
        okareo = _okareo(MULTI)
        resolve_project(okareo, "Support Bot")
        assert resolve_project(okareo).id == BILLING_ID

    def test_bad_pin_is_misconfigured_and_never_falls_back(self, monkeypatch):
        monkeypatch.setenv("OKAREO_PROJECT", "Ghost")
        with pytest.raises(ProjectMisconfigured):
            resolve_project(_okareo(MULTI))

    def test_unpinned_connection_is_unaffected(self, monkeypatch):
        """FR-013: a pin applies to its own connection only."""
        monkeypatch.setenv("OKAREO_PROJECT", "Billing Agent")
        assert resolve_project(_okareo(MULTI)).basis == "pin"
        monkeypatch.delenv("OKAREO_PROJECT")
        _reset_for_tests()
        with pytest.raises(ProjectNotSelected):
            resolve_project(_okareo(MULTI))


class TestTheAskPathAnswersTheObviousFollowUp:
    """FR-026 / research R11 — 'then make me one' must not be improvised."""

    def test_not_selected_names_where_projects_are_created(self):
        tools = _scenario_tools()
        with patch(
            "src.tools.scenarios.get_okareo_client", return_value=_okareo(MULTI)
        ):
            out = json.loads(tools["list_scenarios"]())
        assert "Okareo web application" in out["error"]["note"]

    def test_no_creation_tool_exists_to_call_instead(self):
        from src.server import mcp

        assert "create_project" not in mcp._tool_manager._tools


# ---------------------------------------------------------------------------
# Revision 2 — artifact name resolution inside the acting project (R13)
# ---------------------------------------------------------------------------

REPS_ID = "44444444-4444-4444-8444-444444444444"


def _reps_org():
    """A multi-project org where the target exists ONLY in REPS.

    The mock encodes the real backend's behaviour, which is the whole point of
    the defect: the models-under-test LISTING honours project_id, while the
    by-name lookups do not and therefore cannot see a REPS-only artifact.
    A mock that answered by-name lookups regardless of project would pass
    against the unfixed code and prove nothing (research R13).
    """
    okareo = MagicMock()
    okareo.api_key = "test-key"
    okareo.get_projects.return_value = [
        _project(GLOBAL_ID, "Global"),
        _project(REPS_ID, "REPS"),
    ]

    def _unscoped_by_name(*_a, **_kw):
        raise UnexpectedStatus(404, b"not found")

    okareo.get_target_by_name.side_effect = _unscoped_by_name
    okareo.get_model.side_effect = _unscoped_by_name

    def _run_simulation(**kw):
        # Mirrors the SDK: given a NAME it resolves via the unscoped
        # get_target_by_name and cannot see a REPS-only target; given a
        # resolved Target object it succeeds. This is the bug, in one mock.
        target = kw.get("target")
        if isinstance(target, str):
            raise UnexpectedStatus(404, b"not found")
        return MagicMock()

    okareo.run_simulation.side_effect = _run_simulation
    return okareo


def _reps_target():
    mut = MagicMock()
    mut.id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    mut.name = "checkout-agent"
    mut.project_id = REPS_ID
    mut.models = {"custom_endpoint": {"type": "custom_endpoint"}}
    return mut


def _invoke_thunk(submit_thunk, **_kw):
    """Actually run the submission, so the SDK path under test executes.

    Stubbing _buffered_submit's return value would skip the thunk entirely —
    and the thunk is where the unscoped lookup happens. A test that skips it
    passes against the unfixed code, which is how this defect shipped.
    """
    result = submit_thunk()
    return ("finished", result, "run-1", "https://app")


def _sim_tools():
    from src.tools.simulations import register_tools

    mcp = FastMCP("test")
    register_tools(mcp)
    return {n: t.fn for n, t in mcp._tool_manager._tools.items()}


class TestArtifactsAreUsableInTheProjectThatListsThem:
    """FR-001a — the reported bug: listable but not usable.

    `list_targets` is project-filtered and shows the target; `run_simulation`
    hands the target NAME to okareo.run_simulation, which resolves it through
    an unscoped lookup and 404s. An artifact a project lists must be an
    artifact that project's operations can use.
    """

    def _run(self):
        from okareo_api_client.api import default as _default_pkg

        okareo = _reps_org()
        scenario = MagicMock()
        scenario.name = "my-scenario"
        scenario.scenario_id = "sc-1"
        scenario.scenario_count = 3

        scen_mod = MagicMock()
        scen_mod.sync.return_value = [scenario]
        mut_mod = MagicMock()
        mut_mod.sync.return_value = [_reps_target()]

        with patch(
            "src.tools.simulations.get_okareo_client", return_value=okareo
        ), patch.object(
            _default_pkg, "get_scenario_sets_v0_scenario_sets_get",
            scen_mod, create=True,
        ), patch.object(
            _default_pkg, "get_all_models_under_test_v0_models_under_test_get",
            mut_mod, create=True,
        ), patch(
            "src.tools.simulations._buffered_submit",
            side_effect=_invoke_thunk,
        ), patch(
            "okareo.model_under_test.ModelUnderTest.run_test",
            return_value=MagicMock(),
        ):
            out = json.loads(_sim_tools()["run_simulation"](
                name="sim-1",
                scenario_name="my-scenario",
                target_name="checkout-agent",
                project="REPS",
            ))
        return out, mut_mod

    def test_run_simulation_resolves_the_target_inside_the_acting_project(self):
        """The regression test for the reported 404."""
        out, _ = self._run()
        assert "error" not in out, out
        assert out["project"]["name"] == "REPS"

    def test_the_target_lookup_carries_a_project_filter(self):
        """The load-bearing assertion.

        Asserting only on the return value would pass against a fix that
        happened to work for some other reason.
        """
        _, mut_mod = self._run()
        assert mut_mod.sync.called, "target was never resolved via a listing"
        assert any(
            str(c.kwargs.get("project_id")) == REPS_ID
            for c in mut_mod.sync.call_args_list
        ), "the target lookup did not carry project_id"


class TestTheWholeClassNotJustTheReportedSymptom:
    """FR-001a — the bug report named run_simulation; six other tools shared
    its cause. Fixing only the reported symptom would have left them broken."""

    def _tools(self, module):
        import importlib

        mod = importlib.import_module(f"src.tools.{module}")
        mcp = FastMCP("test")
        mod.register_tools(mcp)
        return {n: t.fn for n, t in mcp._tool_manager._tools.items()}

    def _listing(self, name="checkout-agent"):
        mut = _reps_target()
        mut.name = name
        mod = MagicMock()
        mod.sync.return_value = [mut]
        return mod

    def test_get_generation_model_resolves_inside_the_project(self):
        from okareo_api_client.api import default as _default_pkg

        listing = self._listing("my-model")
        with patch(
            "src.tools.models.get_okareo_client", return_value=_reps_org()
        ), patch.object(
            _default_pkg, "get_all_models_under_test_v0_models_under_test_get",
            listing, create=True,
        ):
            out = json.loads(
                self._tools("models")["get_generation_model"](
                    name="my-model", project="REPS"
                )
            )
        assert "error" not in out, out
        assert str(listing.sync.call_args.kwargs["project_id"]) == REPS_ID

    def test_list_test_runs_resolves_the_model_inside_the_project(self):
        from okareo_api_client.api import default as _default_pkg

        listing = self._listing("my-model")
        okareo = _reps_org()
        okareo.find_test_runs.return_value = []
        with patch(
            "src.tools.tests.get_okareo_client", return_value=okareo
        ), patch.object(
            _default_pkg, "get_all_models_under_test_v0_models_under_test_get",
            listing, create=True,
        ):
            self._tools("tests")["list_test_runs"](
                model_name="my-model", project="REPS"
            )
        assert str(listing.sync.call_args.kwargs["project_id"]) == REPS_ID

    def test_a_missing_artifact_names_the_project_searched(self):
        """FR-030 — and names no other project."""
        from okareo_api_client.api import default as _default_pkg

        listing = self._listing("something-else")
        with patch(
            "src.tools.models.get_okareo_client", return_value=_reps_org()
        ), patch.object(
            _default_pkg, "get_all_models_under_test_v0_models_under_test_get",
            listing, create=True,
        ):
            out = json.loads(
                self._tools("models")["get_generation_model"](
                    name="absent", project="REPS"
                )
            )
        err = out["error"]
        assert err["code"] == "artifact_not_in_project"
        assert "REPS" in err["message"]
        assert err["available"] == ["something-else"]
        assert "Global" not in json.dumps(err)


class TestPerOperationOverrideReachesArtifactResolution:
    """US5 / FR-015 — naming a project on one call must scope that call's
    artifact lookups too, not just the project it reports."""

    def _tools(self):
        import importlib

        mod = importlib.import_module("src.tools.models")
        mcp = FastMCP("test")
        mod.register_tools(mcp)
        return {n: t.fn for n, t in mcp._tool_manager._tools.items()}

    def test_explicit_project_scopes_the_artifact_lookup(self):
        from okareo_api_client.api import default as _default_pkg

        mut = _reps_target()
        mut.name = "my-model"
        listing = MagicMock()
        listing.sync.return_value = [mut]

        with patch(
            "src.tools.models.get_okareo_client", return_value=_reps_org()
        ), patch.object(
            _default_pkg, "get_all_models_under_test_v0_models_under_test_get",
            listing, create=True,
        ):
            out = json.loads(
                self._tools()["get_generation_model"](name="my-model", project="REPS")
            )
        assert out["project"]["name"] == "REPS"
        assert str(listing.sync.call_args.kwargs["project_id"]) == REPS_ID

    def test_the_override_does_not_leak_into_the_next_call(self):
        from okareo_api_client.api import default as _default_pkg

        mut = _reps_target()
        mut.name = "my-model"
        listing = MagicMock()
        listing.sync.return_value = [mut]
        okareo = _reps_org()

        with patch(
            "src.tools.models.get_okareo_client", return_value=okareo
        ), patch.object(
            _default_pkg, "get_all_models_under_test_v0_models_under_test_get",
            listing, create=True,
        ):
            tools = self._tools()
            tools["get_generation_model"](name="my-model", project="REPS")
            out = json.loads(tools["get_generation_model"](name="my-model"))

        # No project argument, multi-project org, no pin -> must ask, not
        # inherit REPS from the previous call.
        assert out["error"]["code"] == "project_not_selected"


class TestGlobalOnlyOrgsUnaffectedByArtifactResolution:
    """US2 / FR-032 — revision 2 must add no step and no failure mode."""

    def _tools(self):
        import importlib

        mod = importlib.import_module("src.tools.models")
        mcp = FastMCP("test")
        mod.register_tools(mcp)
        return {n: t.fn for n, t in mcp._tool_manager._tools.items()}

    def test_name_addressed_operation_works_with_no_project_argument(self):
        from okareo_api_client.api import default as _default_pkg

        mut = _reps_target()
        mut.name = "my-model"
        mut.project_id = GLOBAL_ID
        listing = MagicMock()
        listing.sync.return_value = [mut]

        with patch(
            "src.tools.models.get_okareo_client", return_value=_okareo(ONLY_GLOBAL)
        ), patch.object(
            _default_pkg, "get_all_models_under_test_v0_models_under_test_get",
            listing, create=True,
        ):
            out = json.loads(self._tools()["get_generation_model"](name="my-model"))

        assert "error" not in out, out
        assert out["project"]["basis"] == "default"

    def test_exactly_one_artifact_listing_per_operation(self):
        """FR-032/SC-003: the lookup replaces the old by-name call rather than
        being added on top of it."""
        from okareo_api_client.api import default as _default_pkg

        mut = _reps_target()
        mut.name = "my-model"
        listing = MagicMock()
        listing.sync.return_value = [mut]
        okareo = _okareo(ONLY_GLOBAL)

        with patch(
            "src.tools.models.get_okareo_client", return_value=okareo
        ), patch.object(
            _default_pkg, "get_all_models_under_test_v0_models_under_test_get",
            listing, create=True,
        ):
            self._tools()["get_generation_model"](name="my-model")

        assert listing.sync.call_count == 1
        assert not okareo.get_model.called, "the unscoped lookup must be gone"
