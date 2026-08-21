"""Cross-cutting guarantees over every project-scoped tool (036).

These are the properties that are tedious to check by hand and easy to lose
one tool at a time: that the project is accepted, reported, and advertised to
the co-pilot everywhere it should be, and nowhere it should not.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from src.okareo_client import PROJECT_PARAM_DESC, ResolvedProject

# Tools that act on project-scoped artifacts (contracts/tools.md).
PROJECT_SCOPED: dict[str, set[str]] = {
    "scenarios": {
        "save_scenario", "list_scenarios", "get_scenario",
        "create_scenario_version", "preview_delete_scenario", "delete_scenario",
    },
    "models": {
        "register_generation_model", "list_generation_models",
        "get_generation_model", "update_generation_model",
        "delete_generation_model",
    },
    "simulations": {
        "create_or_update_target", "get_target", "list_targets",
        "delete_target", "run_simulation", "list_simulations",
    },
    "tests": {
        "run_test", "list_test_runs", "get_test_run_results",
        "get_conversation_transcript", "reevaluate_test_run",
    },
    "voice": {
        "ingest_conversations", "connect_voice_integration",
        "list_voice_integrations", "get_voice_integration",
        "update_voice_integration", "rotate_voice_integration_secret",
        "delete_voice_integration", "get_voice_webhook_url",
    },
    "insights": {
        "query_analytics", "list_dashboards", "get_dashboard",
        "save_dashboard", "reorder_dashboards", "delete_dashboard",
    },
}

# Organization-shared or global-catalog tools: they MUST NOT take a project.
NOT_PROJECT_SCOPED: dict[str, set[str]] = {
    "models": {"list_available_llms"},
    "simulations": {
        "create_or_update_driver", "get_driver", "list_drivers",
        "list_driver_voices",
    },
    "tests": {"list_checks"},
    "checks": {
        "create_or_update_check", "generate_check", "get_check", "delete_check",
    },
}

# 36, not the 37 quoted in the plan: `save_scenario` has two mode-specific
# definitions (hosted vs stdio) and only one ever registers, so the decorator
# count and the tool count differ by one.
EXPECTED_TOTAL = 36


def _tools(module: str) -> dict:
    import importlib

    mod = importlib.import_module(f"src.tools.{module}")
    mcp = FastMCP("test")
    mod.register_tools(mcp)
    return mcp._tool_manager._tools


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("OKAREO_API_KEY", "test-api-key-12345")
    monkeypatch.delenv("OKAREO_PROJECT", raising=False)


def _all_scoped() -> list[tuple[str, str]]:
    return [(m, t) for m, names in PROJECT_SCOPED.items() for t in sorted(names)]


class TestSurfaceSize:
    def test_project_scoped_tool_count(self):
        assert sum(len(v) for v in PROJECT_SCOPED.values()) == EXPECTED_TOTAL

    def test_every_declared_tool_actually_exists(self):
        for module, names in {**PROJECT_SCOPED, **NOT_PROJECT_SCOPED}.items():
            registered = set(_tools(module))
            missing = names - registered
            assert not missing, f"{module}: declared but not registered: {missing}"


class TestProjectParameter:
    """FR-004a: one parameter named `project`, name or id."""

    @pytest.mark.parametrize("module,tool", _all_scoped())
    def test_accepts_project(self, module, tool):
        schema = _tools(module)[tool].parameters
        assert "project" in schema["properties"], f"{module}.{tool} has no `project`"

    @pytest.mark.parametrize("module,tool", _all_scoped())
    def test_project_is_optional(self, module, tool):
        """FR-032: existing callers that omit it must keep working."""
        schema = _tools(module)[tool].parameters
        assert "project" not in schema.get("required", [])

    @pytest.mark.parametrize("module,tool", _all_scoped())
    def test_no_project_id_or_project_name_parameter(self, module, tool):
        """FR-004a forbids a name/id pair — one parameter, both forms."""
        props = _tools(module)[tool].parameters["properties"]
        assert "project_id" not in props
        assert "project_name" not in props


class TestParameterGuidanceReachesTheSchema:
    """FR-016a — the assertion that catches a docstring-only regression.

    FastMCP builds its input schema from the signature and ignores docstring
    `Args:` blocks, so guidance written in a docstring still passes every
    behavioral test while never reaching the co-pilot (research R10).
    """

    @pytest.mark.parametrize("module,tool", _all_scoped())
    def test_project_parameter_carries_the_shared_description(self, module, tool):
        prop = _tools(module)[tool].parameters["properties"]["project"]
        assert prop.get("description") == PROJECT_PARAM_DESC

    def test_the_description_says_what_fr_016a_requires(self):
        text = PROJECT_PARAM_DESC.lower()
        assert "preference" in text                      # a user preference
        assert "later conversations" in text             # carried forward
        assert "does not remember" in text               # not server-held
        assert "never guess" in text                     # ask, don't invent
        assert "list_projects" in text                   # how to ask


class TestSharedToolsAreNotProjectScoped:
    """FR-027/FR-028: shared ingredients must not sprout a project knob."""

    @pytest.mark.parametrize(
        "module,tool",
        [(m, t) for m, names in NOT_PROJECT_SCOPED.items() for t in sorted(names)],
    )
    def test_no_project_parameter(self, module, tool):
        props = _tools(module)[tool].parameters["properties"]
        assert "project" not in props, f"{module}.{tool} must stay org-shared"


class TestResponsesReportTheProject:
    """FR-016/FR-017/SC-004."""

    GLOBAL_ID = "11111111-1111-4111-8111-111111111111"

    def _okareo(self, projects):
        okareo = MagicMock()
        okareo.api_key = "k"
        okareo.get_projects.return_value = projects
        return okareo

    def _project(self, pid, name):
        p = MagicMock()
        p.id = pid
        p.name = name
        return p

    def test_list_scenarios_reports_project(self):
        from src.okareo_client import _reset_for_tests

        _reset_for_tests()
        okareo = self._okareo([self._project(self.GLOBAL_ID, "Global")])
        tools = _tools("scenarios")
        with patch("src.tools.scenarios.get_okareo_client", return_value=okareo), patch(
            "okareo_api_client.api.default"
            ".get_scenario_sets_v0_scenario_sets_get.sync",
            return_value=[],
        ):
            out = json.loads(tools["list_scenarios"].fn())
        assert out["project"] == {
            "id": self.GLOBAL_ID, "name": "Global", "basis": "default",
        }

    def test_explicit_project_is_reported_as_explicit(self):
        from src.okareo_client import _reset_for_tests

        _reset_for_tests()
        billing = "22222222-2222-4222-8222-222222222222"
        okareo = self._okareo([
            self._project(self.GLOBAL_ID, "Global"),
            self._project(billing, "Billing Agent"),
        ])
        tools = _tools("scenarios")
        with patch("src.tools.scenarios.get_okareo_client", return_value=okareo), patch(
            "okareo_api_client.api.default"
            ".get_scenario_sets_v0_scenario_sets_get.sync",
            return_value=[],
        ):
            out = json.loads(tools["list_scenarios"].fn(project="Billing Agent"))
        assert out["project"]["name"] == "Billing Agent"
        assert out["project"]["basis"] == "explicit"

    def test_error_responses_are_not_stamped(self):
        """FR-018: an operation that never resolved acted on nothing."""
        from src.okareo_client import _reset_for_tests, _stamp_project

        _reset_for_tests()
        err = json.dumps({"error": {"code": "project_not_selected"}})
        assert _stamp_project(err, ResolvedProject("i", "n", "explicit")) == err

    def test_stamp_is_a_no_op_when_nothing_resolved(self):
        from src.okareo_client import _stamp_project

        body = json.dumps({"ok": True})
        assert _stamp_project(body, None) == body


class TestEveryToolActuallyResolvesAProject:
    """FR-001: a project-scoped operation must act on a determinate project.

    Accepting a `project` parameter is not enough — a tool that never calls
    the resolver would silently ignore the user's choice and act in the
    backend's default project, which is exactly the mis-scoping SC-005
    forbids. This checks the call, not the signature.
    """

    @pytest.mark.parametrize("module,tool", _all_scoped())
    def test_tool_body_resolves_a_project(self, module, tool):
        import inspect

        registered = _tools(module)[tool].fn
        # Unwrap @project_scoped to read the tool's own body.
        fn = getattr(registered, "__wrapped__", registered)
        source = inspect.getsource(fn)
        resolves = "resolve_project" in source or "_save_scenario_impl" in source
        assert resolves, f"{module}.{tool} never resolves a project"


class TestUnresolvedOperationsSurfaceTheOutcome:
    """FR-020: the co-pilot must be able to recognize 'choose a project'."""

    MULTI_IDS = (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    )

    def _multi_okareo(self):
        okareo = MagicMock()
        okareo.api_key = "k"
        projects = []
        for pid, name in zip(self.MULTI_IDS, ("Global", "Billing Agent")):
            p = MagicMock()
            p.id = pid
            p.name = name
            projects.append(p)
        okareo.get_projects.return_value = projects
        return okareo

    @pytest.mark.parametrize(
        "module,tool,kwargs",
        [
            ("scenarios", "list_scenarios", {}),
            ("models", "list_generation_models", {}),
            ("simulations", "list_targets", {}),
            ("tests", "list_test_runs", {}),
            ("voice", "list_voice_integrations", {}),
            ("insights", "list_dashboards", {}),
        ],
    )
    def test_multi_project_org_gets_project_not_selected(self, module, tool, kwargs):
        from src.okareo_client import _reset_for_tests

        _reset_for_tests()
        with patch(
            f"src.tools.{module}.get_okareo_client", return_value=self._multi_okareo()
        ):
            out = json.loads(_tools(module)[tool].fn(**kwargs))
        assert out["error"]["code"] == "project_not_selected"
        assert len(out["error"]["projects"]) == 2
        assert "project" not in out

    def test_payload_needs_no_optional_client_capability(self):
        """FR-021: plain JSON — no elicitation, no sampling, no roots."""
        from src.okareo_client import _reset_for_tests

        _reset_for_tests()
        with patch(
            "src.tools.scenarios.get_okareo_client", return_value=self._multi_okareo()
        ):
            out = json.loads(_tools("scenarios")["list_scenarios"].fn())
        err = out["error"]
        assert isinstance(err["message"], str)
        assert all(
            isinstance(p["id"], str) and isinstance(p["name"], str)
            for p in err["projects"]
        )


class TestSharedIngredientsAreMarkedShared:
    """FR-027 / FR-028 — checks and drivers belong to the organization."""

    SHARED = [
        ("checks", "create_or_update_check"),
        ("checks", "generate_check"),
        ("checks", "get_check"),
        ("checks", "delete_check"),
        ("tests", "list_checks"),
        ("simulations", "create_or_update_driver"),
        ("simulations", "get_driver"),
        ("simulations", "list_drivers"),
    ]

    def test_driver_listing_sends_no_project_filter(self):
        """research R6 — the defect that would silently partition a shared library."""
        okareo = MagicMock()
        okareo.api_key = "k"
        with patch(
            "src.tools.simulations.get_okareo_client", return_value=okareo
        ), patch(
            "okareo_api_client.api.default.get_all_drivers_v0_drivers_get.sync",
            return_value=[],
        ) as sync:
            _tools("simulations")["list_drivers"].fn()
        assert "project_id" not in sync.call_args.kwargs, (
            "list_drivers must not filter by project — drivers are shared (FR-027)"
        )

    def test_driver_listing_marks_the_response_organization_shared(self):
        okareo = MagicMock()
        okareo.api_key = "k"
        with patch(
            "src.tools.simulations.get_okareo_client", return_value=okareo
        ), patch(
            "okareo_api_client.api.default.get_all_drivers_v0_drivers_get.sync",
            return_value=[],
        ):
            out = json.loads(_tools("simulations")["list_drivers"].fn())
        assert out["scope"] == "organization"
        assert "shared across every project" in out["note"]

    @pytest.mark.parametrize("module,tool", SHARED)
    def test_shared_tools_declare_no_project_parameter(self, module, tool):
        props = _tools(module)[tool].parameters["properties"]
        assert "project" not in props


class TestProjectAndScopeAreMutuallyExclusive:
    """FR-018 — an operation is project-scoped or it is not, never both."""

    def test_stamps_do_not_collide(self):
        from src.okareo_client import _stamp_project, _stamp_scope

        body = json.dumps({"ok": True})
        scoped = _stamp_project(body, ResolvedProject("i", "Billing", "explicit"))
        assert "scope" not in json.loads(scoped)

        shared = _stamp_scope(body, "Checks are shared.")
        assert "project" not in json.loads(shared)

    def test_no_project_scoped_tool_is_also_organization_scoped(self):
        import inspect

        for module, tool in _all_scoped():
            fn = _tools(module)[tool].fn
            src = inspect.getsource(getattr(fn, "__wrapped__", fn))
            assert "organization_scoped" not in src, f"{module}.{tool} is both"

    def test_shared_tools_never_report_a_project(self):
        okareo = MagicMock()
        okareo.api_key = "k"
        with patch(
            "src.tools.simulations.get_okareo_client", return_value=okareo
        ), patch(
            "okareo_api_client.api.default.get_all_drivers_v0_drivers_get.sync",
            return_value=[],
        ):
            out = json.loads(_tools("simulations")["list_drivers"].fn())
        assert "project" not in out


class TestFailuresAreNotBlamedOnProjectsWithoutCause:
    """FR-030a — an unverified diagnosis is worse than a generic one.

    An earlier revision appended a cross-project explanation to every 404 that
    happened while a project was resolved. Because every project-scoped tool
    resolves a project, that was nearly all of them — and it misdiagnosed a
    same-project failure, sending users to fix a project that was fine.
    """

    def test_a_generic_404_says_nothing_about_projects(self):
        from src.error_handling import format_tool_error
        from src.okareo_client import _active_project

        class _NotFound(Exception):
            status_code = 404

        token = _active_project.set(ResolvedProject("i", "Billing Agent", "explicit"))
        try:
            err = json.loads(format_tool_error(_NotFound("missing")))["error"]
        finally:
            _active_project.reset(token)

        blob = json.dumps(err)
        assert "Billing Agent" not in blob
        assert "different project" not in blob
        assert "list_projects" not in blob

    def test_only_the_resolver_may_claim_a_project_cause(self):
        """It has just listed the acting project, so it actually knows."""
        from src.error_handling import ArtifactNotInProject, format_tool_error

        err = json.loads(
            format_tool_error(
                ArtifactNotInProject(
                    "No target named 'x' in project 'REPS'.",
                    project={"id": "p", "name": "REPS"},
                    available=["a", "b"],
                )
            )
        )["error"]
        assert err["code"] == "artifact_not_in_project"
        assert "REPS" in err["message"]
        assert err["available"] == ["a", "b"]

    def test_the_removed_helper_has_no_callers(self):
        """T018 — the branch is gone, so its helper should be too."""
        import src.error_handling as eh

        assert not hasattr(eh, "_active_project_name")


class TestNoByNameLookupEscapesTheProject:
    """FR-001a — the guard that would have caught the original defect.

    Fixing ten call sites does not stop an eleventh being added. This asserts
    the *class* is closed: no project-scoped tool may reach an artifact
    through an SDK helper whose lookup carries no project.
    """

    # Lookups that resolve a name with no project parameter. Reaching an
    # artifact through one of these from a project-scoped tool is the defect.
    UNSCOPED_LOOKUPS = ("get_target_by_name", "get_model(")

    # Deliberately exempt: drivers are organization-shared (FR-027), so an
    # unfiltered lookup is CORRECT. Filtering it would partition a shared
    # library — the defect research R6 removed from list_drivers.
    EXEMPT = ("get_driver_by_name",)

    def test_no_project_scoped_module_uses_an_unscoped_lookup(self):
        import inspect

        offenders = []
        for module in sorted(PROJECT_SCOPED):
            import importlib

            src = inspect.getsource(importlib.import_module(f"src.tools.{module}"))
            for line in src.splitlines():
                code = line.split("#", 1)[0]
                if any(bad in code for bad in self.UNSCOPED_LOOKUPS):
                    offenders.append(f"{module}: {line.strip()}")
        assert not offenders, (
            "These reach an artifact through a lookup that carries no "
            "project, so they cannot see artifacts outside the backend's "
            "default project (research R13):\n  " + "\n  ".join(offenders)
        )

    def test_the_driver_exemption_is_intentional_and_still_present(self):
        """If drivers stop being organization-shared, this test should fail
        and force the exemption to be reconsidered rather than assumed."""
        import inspect

        import src.tools.simulations as sims

        src = inspect.getsource(sims)
        assert "get_driver_by_name" in src, (
            "Driver lookups are organization-shared by design (FR-027). If "
            "this changed, revisit the exemption in research R13 rather than "
            "silently project-filtering a shared library."
        )

    def test_the_resolver_is_what_project_scoped_tools_use(self):
        import inspect

        import importlib

        for module in sorted(PROJECT_SCOPED):
            src = inspect.getsource(importlib.import_module(f"src.tools.{module}"))
            if "resolve_artifact_by_name" not in src:
                continue
            assert "project_id" in src, (
                f"{module} resolves artifact names but never computes a project"
            )


class TestSharedIngredientsSurviveRevisionTwo:
    """FR-027 — the artifact-resolution fix must not filter shared lookups."""

    def test_driver_listing_still_sends_no_project_filter(self):
        okareo = MagicMock()
        okareo.api_key = "k"
        with patch(
            "src.tools.simulations.get_okareo_client", return_value=okareo
        ), patch(
            "okareo_api_client.api.default.get_all_drivers_v0_drivers_get.sync",
            return_value=[],
        ) as sync:
            _tools("simulations")["list_drivers"].fn()
        assert "project_id" not in sync.call_args.kwargs

    def test_driver_lookups_are_not_routed_through_the_project_resolver(self):
        """A driver resolved inside the acting project would be partitioned."""
        import inspect

        import src.tools.simulations as sims

        src = inspect.getsource(sims)
        for line in src.splitlines():
            code = line.split("#", 1)[0]
            if "resolve_artifact_by_name" in code:
                assert "driver" not in code.lower(), (
                    "drivers are organization-shared (FR-027); resolving them "
                    "inside a project would partition a shared library"
                )


class TestTargetConfigIsReadFromTheRealListingShape:
    """Regression: `run_simulation` reported "no usable configuration" for
    every target after the revision-2 rewrite.

    The models-under-test listing returns `models` as a generated container
    whose VALUES are generated objects, not plain dicts. Code that filtered
    `models.values()` with `isinstance(v, dict)` therefore found nothing.
    `_fetch_targets` only reads `.keys()`, so `list_targets` kept working and
    the failure looked target-specific when it was universal.

    These build the real generated types on purpose — plain-dict fixtures are
    what let the bug through.
    """

    @staticmethod
    def _real_models(config: dict, key: str = "voice"):
        from okareo_api_client.models.model_under_test_response_models_type_0 import (
            ModelUnderTestResponseModelsType0,
        )

        return ModelUnderTestResponseModelsType0.from_dict({key: config})

    def _extract(self, models):
        """Mirror the extraction under test."""
        import src.tools.simulations as sims

        models_dict = sims._serialize_value(models) or {}
        target_dict, target_type = {}, None
        for key in ("voice", "custom_endpoint", *models_dict.keys()):
            if key not in models_dict:
                continue
            candidate = models_dict[key]
            if not isinstance(candidate, dict):
                candidate = sims._serialize_value(candidate) or {}
            if isinstance(candidate, dict) and candidate:
                target_dict = dict(candidate)
                target_type = target_dict.get("type") or key
                target_dict.setdefault("type", target_type)
                break
        return target_dict, target_type

    def test_generated_container_values_are_not_plain_dicts(self):
        """The premise of the bug, asserted so it cannot silently change."""
        models = self._real_models({"type": "voice"})
        assert not any(
            isinstance(v, dict) for v in models.additional_properties.values()
        )

    def test_voice_target_config_is_recovered(self):
        config = {
            "type": "voice", "edge_type": "twilio",
            "to_phone_number": "+16507441546",
        }
        target_dict, target_type = self._extract(self._real_models(config))
        assert target_type == "voice"
        assert target_dict["to_phone_number"] == "+16507441546"

    def test_custom_endpoint_target_config_is_recovered(self):
        config = {"type": "custom_endpoint", "url": "https://example.test"}
        target_dict, target_type = self._extract(
            self._real_models(config, key="custom_endpoint")
        )
        assert target_type == "custom_endpoint"
        assert target_dict["url"] == "https://example.test"

    def test_type_is_taken_from_the_key_when_the_config_omits_it(self):
        """The old by-name response carried `type`; the listing's key is it."""
        target_dict, target_type = self._extract(
            self._real_models({"to_phone_number": "+1"}, key="voice")
        )
        assert target_type == "voice"
        assert target_dict["type"] == "voice"

    def test_blank_credentials_do_not_make_a_config_unusable(self):
        """Blank strings sit inside a truthy dict — they were never what
        triggered 'no usable configuration'."""
        config = {
            "type": "voice", "edge_type": "twilio",
            "account_sid": "", "auth_token": "", "from_phone_number": "",
            "to_phone_number": "+16507441546",
        }
        target_dict, _ = self._extract(self._real_models(config))
        assert target_dict, "a config with blank credentials is still a config"
