"""Unit tests for backend-error surfacing in src.error_handling (US3)."""

import json

from src.error_handling import format_tool_error


class _FakeResponse:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class HTTPStatusError(Exception):
    """Mimics httpx.HTTPStatusError by class name (classify_error keys on it)."""

    def __init__(self, response):
        super().__init__("error")
        self.response = response


def test_422_validation_detail_is_surfaced():
    exc = HTTPStatusError(
        _FakeResponse(
            422,
            body={
                "detail": [
                    {
                        "loc": ["body", "measures"],
                        "msg": "unsupported measure",
                        "type": "value_error",
                    }
                ]
            },
        )
    )

    result = json.loads(format_tool_error(exc))

    assert result["error"]["category"] == "validation"
    assert "measures" in result["error"]["message"]
    assert "unsupported measure" in result["error"]["message"]
    # Validation suggestion, not the generic "try again later".
    assert "try again later" not in result["error"]["suggestion"]


def test_string_detail_is_surfaced():
    exc = HTTPStatusError(_FakeResponse(400, body={"detail": "Project not found"}))

    result = json.loads(format_tool_error(exc))

    assert "Project not found" in result["error"]["message"]


def test_500_without_detail_falls_back_to_status():
    exc = HTTPStatusError(_FakeResponse(500, body=None, text=""))

    result = json.loads(format_tool_error(exc))

    assert result["error"]["category"] == "server_error"
    assert "HTTP 500" in result["error"]["message"]


class TestProjectErrors:
    """036-project-scoping: machine-recognizable project outcomes (R8)."""

    def _payload(self, exc):
        from src.error_handling import format_tool_error

        return json.loads(format_tool_error(exc))["error"]

    def test_not_selected_carries_code_projects_and_creation_note(self):
        """FR-020 + FR-026: enough for the co-pilot to ask, and to answer
        'then make me one' without improvising."""
        from src.error_handling import PROJECT_CREATION_NOTE, ProjectNotSelected

        err = self._payload(
            ProjectNotSelected(
                "No project has been selected.",
                projects=[{"id": "a", "name": "Global"}, {"id": "b", "name": "Billing"}],
            )
        )
        assert err["code"] == "project_not_selected"
        assert [p["name"] for p in err["projects"]] == ["Global", "Billing"]
        assert err["note"] == PROJECT_CREATION_NOTE
        assert "Okareo web application" in err["note"]

    def test_not_found_carries_code_and_projects(self):
        from src.error_handling import ProjectNotFound

        err = self._payload(
            ProjectNotFound("No project named 'X'.", projects=[{"id": "a", "name": "Global"}])
        )
        assert err["code"] == "project_not_found"
        assert err["projects"]

    def test_misconfigured_names_where_the_pin_lives(self):
        """US4 sc. 3: a pin error is fixed in config, not in conversation."""
        from src.error_handling import ProjectMisconfigured

        err = self._payload(
            ProjectMisconfigured("Pinned to 'Ghost'.", pin="Ghost")
        )
        assert err["code"] == "project_misconfigured"
        assert err["pin"] == "Ghost"
        assert "connection configuration" in err["suggestion"]

    def test_three_codes_are_distinct(self):
        from src.error_handling import (
            ProjectMisconfigured,
            ProjectNotFound,
            ProjectNotSelected,
        )

        codes = {
            ProjectNotSelected.code,
            ProjectNotFound.code,
            ProjectMisconfigured.code,
        }
        assert len(codes) == 3

    def test_distinguishable_from_artifact_not_found(self):
        """FR-030: 'belongs to another project' must never read as 'does not exist'."""
        from src.error_handling import ProjectNotFound, format_tool_error

        project_err = json.loads(format_tool_error(ProjectNotFound("nope")))["error"]

        class _NotFound(Exception):
            status_code = 404

        artifact_err = json.loads(format_tool_error(_NotFound("missing")))["error"]
        assert "code" in project_err
        assert artifact_err.get("code") != project_err["code"]

    def test_only_not_selected_carries_the_creation_note(self):
        """The pointer belongs where the user discovers they need a project."""
        from src.error_handling import ProjectMisconfigured, ProjectNotFound

        assert "note" not in self._payload(ProjectNotFound("x"))
        assert "note" not in self._payload(ProjectMisconfigured("x"))


class TestArtifactNotInProject:
    """FR-001a / FR-030 — the only error allowed to claim a project cause."""

    def _payload(self, exc):
        from src.error_handling import format_tool_error

        return json.loads(format_tool_error(exc))["error"]

    def test_carries_a_distinct_code(self):
        from src.error_handling import ArtifactNotInProject, ProjectNotFound

        err = self._payload(ArtifactNotInProject("No target named 'x'."))
        assert err["code"] == "artifact_not_in_project"
        assert err["code"] != ProjectNotFound.code

    def test_names_the_project_searched_and_what_is_available(self):
        from src.error_handling import ArtifactNotInProject

        err = self._payload(
            ArtifactNotInProject(
                "No target named 'checkout-agent' in project 'REPS'.",
                project={"id": "p1", "name": "REPS"},
                available=["support-agent", "billing-agent"],
            )
        )
        assert err["project"]["name"] == "REPS"
        assert err["available"] == ["support-agent", "billing-agent"]
        assert "REPS" in err["message"]

    def test_never_asserts_an_owning_project(self):
        """FR-030: cross-project visibility may become a security boundary."""
        from src.error_handling import ArtifactNotInProject

        err = self._payload(
            ArtifactNotInProject(
                "No target named 'x' in project 'REPS'.",
                project={"id": "p1", "name": "REPS"},
                available=[],
            )
        )
        blob = json.dumps(err).lower()
        for claim in ("belongs to", "owned by", "lives in project"):
            assert claim not in blob


class TestNoUnverifiedProjectDiagnosis:
    """FR-030a — the system may not blame project scoping on a hunch."""

    def test_404_carries_no_project_narrative(self):
        from src.error_handling import format_tool_error

        class _NotFound(Exception):
            status_code = 404

        err = json.loads(format_tool_error(_NotFound("missing")))["error"]
        blob = json.dumps(err).lower()
        for claim in ("belongs to a different project", "call list_projects", "not found in project"):
            assert claim not in blob

    def test_500_carries_no_project_narrative(self):
        from src.error_handling import format_tool_error

        class _Server(Exception):
            status_code = 500

        err = json.loads(format_tool_error(_Server("boom")))["error"]
        assert "project" not in err["message"].lower()
