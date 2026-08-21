"""Unit tests for the clone_project tool (037-project-clone).

A fake in-memory backend stands in for the Okareo API: it applies the same
lossy semantics the real create endpoint has (tags dropped, generation_type
surviving only for DRIVER/CUSTOM_GENERATOR), so the fidelity verification in
the tool passes only when the repair PUT actually runs. See research.md R1.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from src.okareo_client import ResolvedProject
from src.error_handling import ProjectNotFound

# Bind the generated submodules onto their parent package so patch.object can
# find them regardless of import order in the session (same trick as
# test_scenarios.py's mock_get_scenarios fixture).
from okareo_api_client.api.default import (  # noqa: E402, F401
    get_scenario_sets_v0_scenario_sets_get as _bind_listing,
    update_scenario_set_v0_scenario_sets_scenario_id_put as _bind_update,
)

_PATCH_GET_CLIENT = "src.tools.clone.get_okareo_client"
_PATCH_RESOLVE_PROJECT = "src.tools.clone.resolve_project"

SOURCE_ID = "11111111-1111-4111-8111-111111111111"
DEST_ID = "22222222-2222-4222-8222-222222222222"

SOURCE = ResolvedProject(id=SOURCE_ID, name="WISMO Golden", basis="explicit")

# Types the real create endpoint preserves; everything else lands as SEED
# until the repair PUT fixes it (research R1).
_CREATE_PRESERVES = {"DRIVER", "CUSTOM_GENERATOR"}


def _register_and_get_tools():
    mcp = FastMCP("test")
    from src.tools.clone import register_tools

    register_tools(mcp)
    return {name: tool.fn for name, tool in mcp._tool_manager._tools.items()}


@pytest.fixture
def tools():
    return _register_and_get_tools()


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setenv("OKAREO_API_KEY", "test-api-key-12345")


class FakeBackend:
    """In-memory scenario store keyed by project id, with lossy create."""

    def __init__(self):
        self.scenarios: dict[str, list[SimpleNamespace]] = {}
        self.rows: dict[str, list[SimpleNamespace]] = {}
        self.put_calls: list[dict] = []
        self.soft_return: SimpleNamespace | None = None

    def add_scenario(self, project_id, name, rows, tags=None, type_="DRIVER"):
        sid = str(uuid.uuid4())
        record = SimpleNamespace(
            scenario_id=sid,
            name=name,
            project_id=project_id,
            tags=list(tags or []),
            type_=type_,
            scenario_count=len(rows),
            time_created="2026-08-19T00:00:00",
        )
        self.scenarios.setdefault(str(project_id), []).append(record)
        self.rows[sid] = [
            SimpleNamespace(
                id=str(uuid.uuid4()),
                input_=r[0],
                result=r[1],
                meta_data=(r[2] if len(r) > 2 else {}),
            )
            for r in rows
        ]
        return record

    # --- SDK-facing fakes -------------------------------------------------

    def listing_sync(self, *, client, project_id, api_key):
        return list(self.scenarios.get(str(project_id), []))

    def get_scenario_data_points(self, scenario_id):
        return list(self.rows[str(scenario_id)])

    def create_scenario_set(self, sc):
        if self.soft_return is not None:
            return self.soft_return
        requested = getattr(sc, "generation_type", None)
        requested = str(requested) if requested else "SEED"
        record = self.add_scenario(
            str(sc.project_id),
            sc.name,
            [(sd.input_, sd.result) for sd in sc.seed_data],
            tags=[],  # the real create endpoint drops tags (R1)
            type_=requested if requested in _CREATE_PRESERVES else "SEED",
        )
        return SimpleNamespace(
            scenario_id=record.scenario_id,
            project_id=record.project_id,
            name=record.name,
            type_=record.type_,
            warning=None,
        )

    def update_sync(self, *, scenario_id, client, body, api_key):
        self.put_calls.append({"scenario_id": str(scenario_id), "body": body})
        for records in self.scenarios.values():
            for r in records:
                if r.scenario_id == str(scenario_id):
                    tags = getattr(body, "tags", None)
                    if tags:
                        r.tags = list(tags)
                    type_ = getattr(body, "type_", None)
                    if type_:
                        r.type_ = str(type_)
        return None


def _okareo(backend, projects=None):
    okareo = MagicMock()
    okareo.api_key = "test-key"
    okareo.get_scenario_data_points.side_effect = backend.get_scenario_data_points
    okareo.create_scenario_set.side_effect = backend.create_scenario_set
    okareo.get_projects.return_value = list(projects or [])

    created = SimpleNamespace(id=DEST_ID, name="ADP", tags=[])
    okareo.create_project.return_value = created
    return okareo


@pytest.fixture
def backend():
    b = FakeBackend()
    b.add_scenario(
        SOURCE_ID,
        "order-status-happy-path",
        [("where is my order 123?", "on the way"), ("order 456?", "delivered")],
        tags=["golden", "wismo"],
        type_="DRIVER",
    )
    b.add_scenario(
        SOURCE_ID,
        "order-status-adversarial",
        [("UNIQUE_ROW_MARKER_XYZ cancel everything", "cannot cancel")],
        tags=["golden"],
        type_="DRIVER",
    )
    b.add_scenario(
        SOURCE_ID,
        "seed-smalltalk",
        [("hi", "hello"), ("bye", "goodbye"), ("thanks", "welcome")],
        tags=[],
        type_="SEED",
    )
    return b


def _run(tools, backend, okareo, **kwargs):
    from okareo_api_client.api import default as _default_pkg

    listing = MagicMock()
    listing.sync.side_effect = backend.listing_sync
    update = MagicMock()
    update.sync.side_effect = backend.update_sync

    with patch(_PATCH_GET_CLIENT, return_value=okareo), patch(
        _PATCH_RESOLVE_PROJECT, return_value=SOURCE
    ) as resolve, patch.object(
        _default_pkg, "get_scenario_sets_v0_scenario_sets_get", listing
    ), patch.object(
        _default_pkg,
        "update_scenario_set_v0_scenario_sets_scenario_id_put",
        update,
    ):
        raw = tools["clone_project"](**kwargs)
    return json.loads(raw), resolve, raw


class TestSchemaAndDescription:
    def _tool(self):
        mcp = FastMCP("test")
        from src.tools.clone import register_tools

        register_tools(mcp)
        return mcp._tool_manager._tools["clone_project"]

    def test_parameters_are_exactly_the_contracted_three(self):
        props = self._tool().parameters["properties"]
        assert set(props) == {"source_project", "new_project_name", "dry_run"}
        assert set(self._tool().parameters["required"]) == {
            "source_project",
            "new_project_name",
        }

    def test_description_carries_the_confirm_protocol(self):
        """FR-004: the protocol travels in the description to every client."""
        description = self._tool().description.lower()
        assert "dry_run" in description
        assert "confirm" in description
        assert "clone" in description

    def test_annotations_mark_a_non_destructive_idempotent_writer(self):
        annotations = self._tool().annotations
        assert annotations.readOnlyHint is False
        assert annotations.destructiveHint is False
        assert annotations.idempotentHint is True


class TestDryRun:
    def test_counts_scenarios_and_rows_and_writes_nothing(self, tools, backend):
        okareo = _okareo(backend)
        out, resolve, _ = _run(
            tools,
            backend,
            okareo,
            source_project="WISMO Golden",
            new_project_name="ADP",
            dry_run=True,
        )
        assert out["dry_run"] is True
        assert out["scenario_count"] == 3
        assert out["row_count"] == 6
        assert out["source_project"] == {
            "id": SOURCE_ID,
            "name": "WISMO Golden",
            "basis": "explicit",
        }
        assert {s["name"] for s in out["scenarios"]} == {
            "order-status-happy-path",
            "order-status-adversarial",
            "seed-smalltalk",
        }
        assert resolve.call_args[0][1] == "WISMO Golden"
        assert not okareo.create_project.called
        assert not okareo.create_scenario_set.called
        assert backend.put_calls == []

    def test_reports_whether_the_destination_name_exists(self, tools, backend):
        existing = SimpleNamespace(id=DEST_ID, name="ADP")
        okareo = _okareo(backend, projects=[existing])
        out, _, _ = _run(
            tools,
            backend,
            okareo,
            source_project="WISMO Golden",
            new_project_name="ADP",
            dry_run=True,
        )
        assert out["destination_exists"] is True

    def test_directs_the_copilot_to_confirm_before_executing(self, tools, backend):
        out, _, _ = _run(
            tools,
            backend,
            _okareo(backend),
            source_project="WISMO Golden",
            new_project_name="ADP",
            dry_run=True,
        )
        assert "confirm" in out["next_step"].lower()


class TestExecute:
    def test_clones_every_scenario_with_field_level_fidelity(self, tools, backend):
        okareo = _okareo(backend)
        out, _, raw = _run(
            tools,
            backend,
            okareo,
            source_project="WISMO Golden",
            new_project_name="ADP",
        )
        assert out["status"] == "completed"
        assert out["created"] == 3 and out["skipped"] == 0 and out["failed"] == 0
        assert out["destination_project"]["created"] is True
        assert out["destination_project"]["id"] == DEST_ID
        assert out["fidelity_gaps"] == []
        assert all(s["fidelity"] == "verified" for s in out["scenarios"])

        cloned = {s.name: s for s in backend.scenarios[DEST_ID]}
        assert set(cloned) == {
            "order-status-happy-path",
            "order-status-adversarial",
            "seed-smalltalk",
        }
        assert cloned["order-status-happy-path"].tags == ["golden", "wismo"]
        assert cloned["seed-smalltalk"].type_ == "SEED"
        assert cloned["order-status-happy-path"].type_ == "DRIVER"

    def test_rows_are_copied_in_order(self, tools, backend):
        okareo = _okareo(backend)
        _run(
            tools,
            backend,
            okareo,
            source_project="WISMO Golden",
            new_project_name="ADP",
        )
        cloned = {s.name: s for s in backend.scenarios[DEST_ID]}
        rows = backend.rows[cloned["seed-smalltalk"].scenario_id]
        assert [r.input_ for r in rows] == ["hi", "bye", "thanks"]
        assert [r.result for r in rows] == ["hello", "goodbye", "welcome"]

    def test_no_row_content_reaches_the_conversation(self, tools, backend):
        """FR-005: the response carries names and counts, never rows."""
        _, _, raw = _run(
            tools,
            backend,
            _okareo(backend),
            source_project="WISMO Golden",
            new_project_name="ADP",
        )
        assert "UNIQUE_ROW_MARKER_XYZ" not in raw

    def test_empty_source_clones_to_an_empty_destination(self, tools):
        empty = FakeBackend()
        okareo = _okareo(empty)
        out, _, _ = _run(
            tools,
            empty,
            okareo,
            source_project="WISMO Golden",
            new_project_name="ADP",
        )
        assert out["status"] == "completed"
        assert out["scenario_count"] == 0
        assert okareo.create_project.called

    def test_one_failed_scenario_does_not_stop_the_others(self, tools, backend):
        okareo = _okareo(backend)
        real_create = backend.create_scenario_set

        def flaky(sc):
            if sc.name == "order-status-adversarial":
                raise TypeError("error: ['boom']")
            return real_create(sc)

        okareo.create_scenario_set.side_effect = flaky
        out, _, _ = _run(
            tools,
            backend,
            okareo,
            source_project="WISMO Golden",
            new_project_name="ADP",
        )
        assert out["status"] == "completed_with_failures"
        assert out["created"] == 2 and out["failed"] == 1
        failed = [s for s in out["scenarios"] if s["status"] == "failed"]
        assert failed[0]["name"] == "order-status-adversarial"
        assert failed[0]["error"]

    def test_source_row_metadata_is_reported_as_a_fidelity_gap(self, tools):
        b = FakeBackend()
        b.add_scenario(
            SOURCE_ID,
            "with-metadata",
            [("q", "a", {"speaker": "driver"})],
            tags=[],
            type_="DRIVER",
        )
        out, _, _ = _run(
            tools,
            b,
            _okareo(b),
            source_project="WISMO Golden",
            new_project_name="ADP",
        )
        assert out["status"] == "completed_with_failures"
        assert any("meta_data" in g for g in out["fidelity_gaps"])


class TestSoftReturnGuard:
    """FR-011: the pre-fix backend returns an existing set instead of creating."""

    def test_warning_on_create_aborts_loudly(self, tools, backend):
        okareo = _okareo(backend)
        backend.soft_return = SimpleNamespace(
            scenario_id=str(uuid.uuid4()),
            project_id=DEST_ID,
            name="order-status-happy-path",
            type_="DRIVER",
            warning="Scenario Set with this name already exists",
        )
        out, _, _ = _run(
            tools,
            backend,
            okareo,
            source_project="WISMO Golden",
            new_project_name="ADP",
        )
        assert out["error"]["code"] == "clone_aborted_backend_soft_return"
        assert out["error"]["copied_before_abort"] == 0
        assert okareo.create_scenario_set.call_count == 1  # aborted, not continued

    def test_wrong_project_on_create_aborts_loudly(self, tools, backend):
        okareo = _okareo(backend)
        backend.soft_return = SimpleNamespace(
            scenario_id=backend.scenarios[SOURCE_ID][0].scenario_id,
            project_id=SOURCE_ID,  # the source's own set came back
            name="order-status-happy-path",
            type_="DRIVER",
            warning=None,
        )
        out, _, _ = _run(
            tools,
            backend,
            okareo,
            source_project="WISMO Golden",
            new_project_name="ADP",
        )
        assert out["error"]["code"] == "clone_aborted_backend_soft_return"
        assert okareo.create_scenario_set.call_count == 1


class TestResume:
    def _duplicate_rejecting(self, backend, existing_projects):
        okareo = _okareo(backend, projects=existing_projects)
        okareo.create_project.side_effect = TypeError(
            "error: ['Project with this name already exists']"
        )
        return okareo

    def test_partial_clone_resumes_and_converges_tags(self, tools, backend):
        dest = SimpleNamespace(id=DEST_ID, name="ADP")
        # One scenario landed earlier, but its tag repair never ran.
        backend.add_scenario(
            DEST_ID,
            "order-status-happy-path",
            [("where is my order 123?", "on the way"), ("order 456?", "delivered")],
            tags=[],
            type_="DRIVER",
        )
        okareo = self._duplicate_rejecting(backend, [dest])
        out, _, _ = _run(
            tools,
            backend,
            okareo,
            source_project="WISMO Golden",
            new_project_name="ADP",
        )
        assert out["status"] == "completed"
        assert out["created"] == 2 and out["skipped"] == 1
        assert out["destination_project"]["created"] is False
        skipped = [s for s in out["scenarios"] if s["status"] == "skipped_existing"]
        assert skipped[0]["name"] == "order-status-happy-path"
        assert skipped[0]["fidelity"] == "verified"
        converged = {s.name: s for s in backend.scenarios[DEST_ID]}
        assert converged["order-status-happy-path"].tags == ["golden", "wismo"]

    def test_rerun_over_a_complete_clone_is_a_verified_noop(self, tools, backend):
        dest = SimpleNamespace(id=DEST_ID, name="ADP")
        for s in list(backend.scenarios[SOURCE_ID]):
            backend.add_scenario(
                DEST_ID,
                s.name,
                [(r.input_, r.result) for r in backend.rows[s.scenario_id]],
                tags=s.tags,
                type_=s.type_,
            )
        okareo = self._duplicate_rejecting(backend, [dest])
        out, _, _ = _run(
            tools,
            backend,
            okareo,
            source_project="WISMO Golden",
            new_project_name="ADP",
        )
        assert out["status"] == "completed"
        assert out["created"] == 0 and out["skipped"] == 3 and out["failed"] == 0
        assert not okareo.create_scenario_set.called
        assert all(s["fidelity"] == "verified" for s in out["scenarios"])


class TestProvenanceTag:
    def test_destination_is_created_with_the_provenance_tag(self, tools, backend):
        """The created destination Project carries a plain human-readable
        'Cloned from: <source-name>' tag — it renders as-is in the Projects
        table's tag chips, no frontend work needed."""
        okareo = _okareo(backend)
        out, _, _ = _run(
            tools,
            backend,
            okareo,
            source_project="WISMO Golden",
            new_project_name="ADP",
        )
        okareo.create_project.assert_called_once_with(
            "ADP", tags=["Cloned from: WISMO Golden"]
        )
        assert out["destination_project"]["provenance_tag"] == (
            "Cloned from: WISMO Golden"
        )

    def test_resume_neither_fails_on_nor_duplicates_the_tag(self, tools, backend):
        """A re-run resumes into the existing destination: no second
        create_project, no Project-tag mutation of any kind — the existing
        tag is simply left alone."""
        dest = SimpleNamespace(
            id=DEST_ID, name="ADP", tags=["Cloned from: WISMO Golden"]
        )
        for s in list(backend.scenarios[SOURCE_ID]):
            backend.add_scenario(
                DEST_ID,
                s.name,
                [(r.input_, r.result) for r in backend.rows[s.scenario_id]],
                tags=s.tags,
                type_=s.type_,
            )
        okareo = _okareo(backend, projects=[dest])
        okareo.create_project.side_effect = TypeError(
            "error: ['Project with this name already exists']"
        )
        out, _, _ = _run(
            tools,
            backend,
            okareo,
            source_project="WISMO Golden",
            new_project_name="ADP",
        )
        assert out["status"] == "completed"
        assert out["destination_project"]["created"] is False
        assert out["destination_project"]["provenance_tag"] is None
        assert okareo.create_project.call_count == 1  # the rejected attempt only
        assert not okareo.update_project.called


class TestCacheInvalidation:
    def test_clone_makes_the_new_project_immediately_resolvable(self, backend):
        """CR-1: the tool's own next_step sends the copilot to
        create_or_update_target(project=<destination>), which resolves through
        the 60-second project cache. The clone primes that cache while
        resolving the source, so creating the destination must invalidate it
        — otherwise every project-scoped tool 404s on the new Project until
        the TTL expires."""
        from okareo_api_client.api import default as _default_pkg

        from src.okareo_client import _reset_for_tests, resolve_project

        _reset_for_tests()
        try:
            source_proj = SimpleNamespace(id=SOURCE_ID, name="WISMO Golden")
            okareo = _okareo(backend, projects=[source_proj])

            def create(name, tags=None):
                created = SimpleNamespace(id=DEST_ID, name=name, tags=list(tags or []))
                okareo.get_projects.return_value = [source_proj, created]
                return created

            okareo.create_project.side_effect = create

            listing = MagicMock()
            listing.sync.side_effect = backend.listing_sync
            update = MagicMock()
            update.sync.side_effect = backend.update_sync

            tools = _register_and_get_tools()
            # Real resolve_project on purpose: it primes the cache with the
            # pre-clone project list, which is exactly the hazard.
            with patch(_PATCH_GET_CLIENT, return_value=okareo), patch.object(
                _default_pkg, "get_scenario_sets_v0_scenario_sets_get", listing
            ), patch.object(
                _default_pkg,
                "update_scenario_set_v0_scenario_sets_scenario_id_put",
                update,
            ):
                out = json.loads(tools["clone_project"](
                    source_project="WISMO Golden", new_project_name="ADP",
                ))
            assert out["status"] == "completed", out

            resolved = resolve_project(okareo, "ADP")
            assert resolved.id == DEST_ID
        finally:
            _reset_for_tests()


class TestRefusals:
    def test_destination_holding_unrelated_scenarios_is_refused(self, tools, backend):
        dest = SimpleNamespace(id=DEST_ID, name="ADP")
        backend.add_scenario(DEST_ID, "someone-elses-work", [("x", "y")])
        okareo = _okareo(backend, projects=[dest])
        okareo.create_project.side_effect = TypeError(
            "error: ['Project with this name already exists']"
        )
        out, _, _ = _run(
            tools,
            backend,
            okareo,
            source_project="WISMO Golden",
            new_project_name="ADP",
        )
        assert out["error"]["code"] == "clone_refused_destination_conflict"
        # The backend's own rejection is surfaced, not replaced (036 FR-001c).
        assert "already exists" in out["error"]["message"]
        assert not okareo.create_scenario_set.called

    def test_cloning_a_project_onto_itself_is_refused(self, tools, backend):
        okareo = _okareo(backend)
        out, _, _ = _run(
            tools,
            backend,
            okareo,
            source_project="WISMO Golden",
            new_project_name="wismo golden",  # case-insensitive match (FR-004)
        )
        assert out["error"]["code"] == "clone_refused_source_is_destination"
        assert not okareo.create_project.called
        assert not okareo.create_scenario_set.called

    def test_unknown_source_project_reports_not_found(self, tools, backend):
        okareo = _okareo(backend)
        from okareo_api_client.api import default as _default_pkg

        with patch(_PATCH_GET_CLIENT, return_value=okareo), patch(
            _PATCH_RESOLVE_PROJECT,
            side_effect=ProjectNotFound("No project named 'Nope'", projects=[]),
        ), patch.object(
            _default_pkg, "get_scenario_sets_v0_scenario_sets_get", MagicMock()
        ):
            out = json.loads(
                tools["clone_project"](
                    source_project="Nope", new_project_name="ADP"
                )
            )
        assert out["error"]["code"] == "project_not_found"

    def test_padded_destination_name_is_normalized(self, tools, backend):
        """Incidental whitespace is normalized, per contracts/tools.md — the
        Project is created under the trimmed name."""
        okareo = _okareo(backend)
        out, _, _ = _run(
            tools,
            backend,
            okareo,
            source_project="WISMO Golden",
            new_project_name="  ADP  ",
        )
        assert out["status"] == "completed"
        okareo.create_project.assert_called_once_with(
            "ADP", tags=["Cloned from: WISMO Golden"]
        )
        assert out["destination_project"]["name"] == "ADP"

    def test_blank_source_project_is_rejected_not_defaulted(self, tools, backend):
        """FR-007: the source is explicit always — never pin/default fallback."""
        okareo = _okareo(backend)
        out, resolve, _ = _run(
            tools,
            backend,
            okareo,
            source_project="   ",
            new_project_name="ADP",
        )
        assert "error" in out
        assert "source_project" in json.dumps(out["error"])
        assert not resolve.called
