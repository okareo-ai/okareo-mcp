"""Unit tests for the get_reps_baseline MCP tool (src/tools/reps.py).

The engine is mocked at the ``src.reps_baseline.get_snapshot`` boundary —
no network, no tarballs.
"""

import json
import re

import pytest

from src import reps_baseline
from src.reps_baseline import (
    RECOGNIZED_PILLARS,
    BaselineSnapshot,
    BaselineUnavailableError,
    FileRecord,
)

FETCHED_AT = 1_753_400_000.0  # arbitrary fixed epoch
ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _record(path: str, content: bytes, *, oversize: bool = False) -> FileRecord:
    first = path.split("/")[0]
    pillar = first if "/" in path and first in RECOGNIZED_PILLARS else None
    return FileRecord(
        path=path,
        size=len(content),
        pillar=pillar,
        content=None if oversize else content,
        oversize=oversize,
    )


def make_snapshot(**overrides) -> BaselineSnapshot:
    records = [
        _record("README.md", b"# REPS\n"),
        _record(
            "S-security/scenarios/verification-gate.jsonl",
            b'{"input": "attack one"}\n{"input": "attack two"}\n',
        ),
        _record("S-security/scenarios/verification-gate_meta.md", b"# Meta\n"),
        _record("S-security/drivers/goal-hijacker.md", b"# Driver\n"),
        _record("R-reasoning/coverage.json", b'{"pillar": "R-reasoning"}\n'),
        _record("explore/probe.md", b"# Probe\n"),
        _record("shared/rubric.md", b"# Shared rubric\n"),
        _record("S-security/huge.bin", b"", oversize=True),
        _record("S-security/binary.dat", b"\xff\xfe\x00\x80"),
    ]
    defaults = dict(
        tag="v0.5.1",
        fetched_at=FETCHED_AT,
        checked_at=FETCHED_AT,
        files={r.path: r for r in records},
        pinned=False,
        stale=False,
        stale_reason=None,
    )
    defaults.update(overrides)
    return BaselineSnapshot(**defaults)


def _register_and_get_tool():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    from src.tools.reps import register_tools

    register_tools(mcp)
    return mcp, mcp._tool_manager._tools["get_reps_baseline"].fn


@pytest.fixture
def tool():
    return _register_and_get_tool()[1]


@pytest.fixture(autouse=True)
def no_airgap(monkeypatch):
    monkeypatch.delenv("AIRGAP", raising=False)


@pytest.fixture
def snapshot(monkeypatch):
    snap = make_snapshot()
    monkeypatch.setattr(reps_baseline, "get_snapshot", lambda: snap)
    return snap


# ---------------------------------------------------------------------------
# Registration metadata (T010)
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_tool_registered_with_metadata(self):
        mcp, _ = _register_and_get_tool()
        tool_obj = mcp._tool_manager._tools["get_reps_baseline"]
        assert tool_obj.title == "Get REPS Baseline Material"
        assert tool_obj.annotations.readOnlyHint is True
        assert tool_obj.annotations.destructiveHint is False
        assert tool_obj.annotations.idempotentHint is True
        assert tool_obj.annotations.openWorldHint is True

    def test_registered_on_real_server(self):
        from src.server import mcp as server_mcp

        assert "get_reps_baseline" in server_mcp._tool_manager._tools


# ---------------------------------------------------------------------------
# Discovery mode (T007)
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_full_tree_includes_non_pillar_locations(self, tool, snapshot):
        result = json.loads(tool())
        assert result["mode"] == "discovery"
        assert result["pillar"] is None
        assert result["count"] == len(snapshot.files)
        by_path = {f["path"]: f for f in result["files"]}
        assert by_path["shared/rubric.md"]["pillar"] is None
        assert by_path["README.md"]["pillar"] is None
        assert by_path["S-security/drivers/goal-hijacker.md"]["pillar"] == "S-security"

    def test_files_sorted_and_shaped(self, tool, snapshot):
        result = json.loads(tool())
        paths = [f["path"] for f in result["files"]]
        assert paths == sorted(paths)
        entry = next(f for f in result["files"] if f["path"] == "explore/probe.md")
        assert set(entry) == {"path", "size", "pillar"}
        assert entry["size"] == len(b"# Probe\n")

    def test_oversize_flag_in_listing(self, tool, snapshot):
        result = json.loads(tool())
        entry = next(f for f in result["files"] if f["path"] == "S-security/huge.bin")
        assert entry["oversize"] is True

    def test_pillar_filter(self, tool, snapshot):
        result = json.loads(tool(pillar="S-security"))
        assert result["pillar"] == "S-security"
        assert result["count"] > 0
        assert all(f["pillar"] == "S-security" for f in result["files"])
        assert result["pillars"] == RECOGNIZED_PILLARS

    def test_unknown_pillar(self, tool, snapshot):
        result = json.loads(tool(pillar="X-bogus"))
        assert result["error"]["code"] == "unknown_pillar"
        assert result["error"]["valid_pillars"] == RECOGNIZED_PILLARS


# ---------------------------------------------------------------------------
# Fetch mode (T008)
# ---------------------------------------------------------------------------

class TestFetch:
    def test_fetch_byte_fidelity_jsonl(self, tool, snapshot):
        result = json.loads(tool(path="S-security/scenarios/verification-gate.jsonl"))
        assert result["mode"] == "fetch"
        original = snapshot.files["S-security/scenarios/verification-gate.jsonl"]
        assert result["content"].encode("utf-8") == original.content
        assert result["size"] == original.size

    def test_fetch_meta_md(self, tool, snapshot):
        result = json.loads(tool(path="S-security/scenarios/verification-gate_meta.md"))
        assert result["content"] == "# Meta\n"

    def test_unknown_path(self, tool, snapshot):
        result = json.loads(tool(path="S-security/nope.jsonl"))
        assert result["error"]["code"] == "unknown_path"
        assert "suggestion" in result["error"]

    @pytest.mark.parametrize(
        "traversal",
        [
            "../src/server.py",
            "S-security/../../etc/passwd",
            "/etc/passwd",
        ],
    )
    def test_traversal_attempts_never_match(self, tool, snapshot, traversal):
        result = json.loads(tool(path=traversal))
        assert result["error"]["code"] == "unknown_path"

    def test_oversize_file_not_servable(self, tool, snapshot):
        result = json.loads(tool(path="S-security/huge.bin"))
        assert result["error"]["code"] == "file_not_servable"
        assert result["error"]["path"] == "S-security/huge.bin"

    def test_non_utf8_file_not_servable(self, tool, snapshot):
        result = json.loads(tool(path="S-security/binary.dat"))
        assert result["error"]["code"] == "file_not_servable"

    def test_blank_path_invalid_request(self, tool, snapshot):
        result = json.loads(tool(path="   "))
        assert result["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# Version parameter and gates (T009)
# ---------------------------------------------------------------------------

class TestVersionAndGates:
    def test_version_matching_served_tag_succeeds(self, tool, snapshot):
        result = json.loads(tool(version="v0.5.1"))
        assert result["mode"] == "discovery"

    def test_version_mismatch_rejected(self, tool, snapshot):
        result = json.loads(tool(version="v0.4.0"))
        assert result["error"]["code"] == "version_not_available"
        assert result["error"]["available_versions"] == ["v0.5.1"]
        assert "v0.5.1" in result["error"]["message"]

    def test_airgap_gate_no_engine_access(self, tool, monkeypatch):
        called = []
        monkeypatch.setattr(
            reps_baseline, "get_snapshot", lambda: called.append(1)
        )
        monkeypatch.setenv("AIRGAP", "1")
        result = json.loads(tool())
        assert result["error"]["code"] == "baseline_unavailable"
        assert called == []

    def test_engine_unavailable_maps_to_error(self, tool, monkeypatch):
        def boom():
            raise BaselineUnavailableError("no cached copy exists")

        monkeypatch.setattr(reps_baseline, "get_snapshot", boom)
        result = json.loads(tool())
        assert result["error"]["code"] == "baseline_unavailable"
        assert "no cached copy" in result["error"]["message"]
        assert "tag" not in result  # no snapshot — provenance honestly absent


# ---------------------------------------------------------------------------
# Provenance envelope invariants (T013/T014 — US2)
# ---------------------------------------------------------------------------

SUCCESS_CALLS = [
    {},
    {"pillar": "S-security"},
    {"path": "explore/probe.md"},
    {"version": "v0.5.1"},
]

ERROR_CALLS = [
    ({"pillar": "X-bogus"}, "unknown_pillar"),
    ({"path": "nope.md"}, "unknown_path"),
    ({"path": "S-security/huge.bin"}, "file_not_servable"),
    ({"path": "  "}, "invalid_request"),
    ({"version": "v0.0.1"}, "version_not_available"),
]


class TestProvenance:
    @pytest.mark.parametrize("kwargs", SUCCESS_CALLS)
    def test_every_success_carries_provenance(self, tool, snapshot, kwargs):
        result = json.loads(tool(**kwargs))
        assert "error" not in result
        assert result["tag"] == "v0.5.1"
        assert ISO_UTC_RE.match(result["fetched_at"])
        assert result["stale"] is False
        assert result["pin"] is False
        assert "stale_reason" not in result

    @pytest.mark.parametrize(("kwargs", "code"), ERROR_CALLS)
    def test_every_error_carries_tag_and_stale(self, tool, snapshot, kwargs, code):
        result = json.loads(tool(**kwargs))
        assert result["error"]["code"] == code
        assert result["tag"] == "v0.5.1"
        assert result["stale"] is False


# ---------------------------------------------------------------------------
# Staleness passthrough (T017/T018 — US3) and pin passthrough (T020/T021 — US4)
# ---------------------------------------------------------------------------

class TestStaleAndPin:
    def test_stale_snapshot_surfaces_reason(self, tool, monkeypatch):
        snap = make_snapshot(stale=True, stale_reason="github_unreachable")
        monkeypatch.setattr(reps_baseline, "get_snapshot", lambda: snap)
        result = json.loads(tool())
        assert result["stale"] is True
        assert result["stale_reason"] == "github_unreachable"

    def test_stale_surfaced_on_errors_too(self, tool, monkeypatch):
        snap = make_snapshot(stale=True, stale_reason="github_unreachable")
        monkeypatch.setattr(reps_baseline, "get_snapshot", lambda: snap)
        result = json.loads(tool(path="nope.md"))
        assert result["error"]["code"] == "unknown_path"
        assert result["stale"] is True

    def test_pinned_snapshot_sets_pin_true(self, tool, monkeypatch):
        snap = make_snapshot(tag="v0.4.0", pinned=True)
        monkeypatch.setattr(reps_baseline, "get_snapshot", lambda: snap)
        result = json.loads(tool())
        assert result["pin"] is True
        assert result["tag"] == "v0.4.0"

    def test_version_check_against_pinned_tag(self, tool, monkeypatch):
        snap = make_snapshot(tag="v0.4.0", pinned=True)
        monkeypatch.setattr(reps_baseline, "get_snapshot", lambda: snap)
        ok = json.loads(tool(version="v0.4.0"))
        assert ok["mode"] == "discovery"
        rejected = json.loads(tool(version="v0.5.1"))
        assert rejected["error"]["code"] == "version_not_available"
        assert rejected["error"]["available_versions"] == ["v0.4.0"]
