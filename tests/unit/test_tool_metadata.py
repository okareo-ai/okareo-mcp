"""Connector Directory metadata audit (feature 024).

Asserts every registered tool carries a human-readable title and a complete,
non-contradictory set of behavioral annotations, and that any future prompt
carries a human-readable name. This is the regression guard described in
specs/024-tool-titles-annotations/contracts/tool-metadata.schema.md (C1-C8):
adding a tool/prompt without metadata fails CI.
"""

import asyncio

import pytest

from src.server import mcp

EXPECTED_TOOL_COUNT = 50


def _tools():
    return asyncio.run(mcp.list_tools())


def _prompts():
    return asyncio.run(mcp.list_prompts())


@pytest.fixture(scope="module")
def tools():
    return _tools()


# C7 — Coverage
def test_tool_count(tools):
    assert len(tools) == EXPECTED_TOOL_COUNT, (
        f"expected {EXPECTED_TOOL_COUNT} tools, got {len(tools)}; "
        "update EXPECTED_TOOL_COUNT and the data-model matrix if tools changed"
    )


# C1 — Title present
def test_every_tool_has_title(tools):
    missing = [t.name for t in tools if not getattr(t, "title", None)]
    assert not missing, f"tools missing a human-readable title: {missing}"


# C2 — Title legible
def test_titles_are_human_readable(tools):
    for t in tools:
        title = t.title
        assert "_" not in title, f"{t.name}: title '{title}' contains underscores"
        assert title != t.name, f"{t.name}: title must differ from the raw identifier"
        assert title[:1].isupper(), f"{t.name}: title '{title}' should be title-case"


# C3 — Annotations complete
def test_every_tool_has_complete_annotations(tools):
    for t in tools:
        a = t.annotations
        assert a is not None, f"{t.name}: missing annotations"
        for field in (
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
        ):
            assert isinstance(getattr(a, field), bool), (
                f"{t.name}: annotation '{field}' must be an explicit bool"
            )


# C4 — No contradiction
def test_no_readonly_and_destructive(tools):
    bad = [t.name for t in tools if t.annotations.readOnlyHint and t.annotations.destructiveHint]
    assert not bad, f"tools marked both read-only and destructive: {bad}"


# C5 — Delete semantics
def test_delete_tools_are_destructive(tools):
    for t in tools:
        if t.name.startswith("delete_"):
            a = t.annotations
            assert a.readOnlyHint is False, f"{t.name}: delete tool must not be read-only"
            assert a.destructiveHint is True, f"{t.name}: delete tool must be destructive"
            assert a.idempotentHint is False, f"{t.name}: delete tool must be non-idempotent"


# C6 — Read semantics
def test_list_and_get_tools_are_read_only(tools):
    for t in tools:
        if t.name.startswith(("list_", "get_")):
            a = t.annotations
            assert a.readOnlyHint is True, f"{t.name}: list/get tool must be read-only"
            assert a.destructiveHint is False, f"{t.name}: list/get tool must not be destructive"


# C8 — Prompt naming (forward-looking guard; 0 prompts today => vacuously true)
def test_every_prompt_has_name():
    prompts = _prompts()
    unnamed = [getattr(p, "name", None) for p in prompts if not getattr(p, "name", None)]
    assert not unnamed, f"prompts missing a human-readable name: {unnamed}"
