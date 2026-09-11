"""Check placeholder guidance audit (042, phase 01.3).

Okareo now REJECTS a judge prompt carrying a placeholder that is not a current
template variable — on save and in ``calibrate_check`` — instead of quietly
passing the unknown token through to the judge as literal text. Every authoring
surface this server ships therefore has to teach the current names only:

- the four ``*_check_prompt.md`` templates ``get_templates`` serves, and
- the ``create_or_update_check`` / ``calibrate_check`` docstrings.

Three properties are pinned here:

1. Every ``{brace}`` token an agent is invited to copy names a current
   template variable. Author-fill slots — the text a human writes, which
   Okareo never substitutes — appear as ``<angle>`` slots so an agent can
   never paste one into a prompt and have it rejected.
2. The placeholder documentation splits into "fill these in yourself" and
   "runtime placeholders", so the two kinds are not one undifferentiated list.
3. The rejection note appears verbatim on every surface.

The brace scan is deliberately **section-scoped, never whole-file**:
``target_validate_check_prompt.md`` also documents a Target request body,
whose JSON and Target-substitution variables (``{session_id}``,
``{latest_message}``, …) legitimately carry braces and belong to a different
namespace; and the rejection note itself names the rejected aliases in braces.
"""

import re
from pathlib import Path

import pytest

# The four check-prompt templates get_templates serves.
CHECK_TEMPLATES = [
    "boolean_check_prompt",
    "score_check_prompt",
    "analysis_check_prompt",
    "target_validate_check_prompt",
]

# The 10 current check template variables (app/template_variables/constants.py,
# CHECK_VARIABLES minus the 5 deprecated aliases).
VALID_PLACEHOLDERS = {
    "model_output",
    "scenario_input",
    "scenario_result",
    "model_input",
    "message_history",
    "tool_calls",
    "tools",
    "model_output_metadata",
    "simulation_message_history",
    "user_only_audio",
}

# Rejected on save and by calibrate_check — never documented as a placeholder.
DEPRECATED_ALIASES = {
    "generation",
    "input",
    "result",
    "audio_messages",
    "audio_output",
}

# Author-fill slots per template: the author replaces these with their own
# text. They are NOT runtime placeholders, so they must never wear braces.
AUTHOR_FILL_SLOTS = {
    "boolean_check_prompt": {"criterion_description"},
    "score_check_prompt": {"min_score", "max_score", "scoring_rubric"},
    "analysis_check_prompt": {"analysis_criteria"},
    "target_validate_check_prompt": {"validation_criteria"},
}

# One note, verbatim on every surface (markdown backticks and line wrapping
# are normalized away before comparison).
REJECTION_NOTE = (
    "Okareo rejects any placeholder that is not listed above. That "
    "includes the legacy aliases {generation}, {input}, {result}, "
    "{audio_messages}, and {audio_output} — use {model_output}, "
    "{scenario_input}, {scenario_result}, and {user_only_audio} instead. "
    "A rejected prompt fails on save and fails calibrate_check."
)

# The docstrings audited alongside the templates.
DOCSTRING_TOOLS = ["create_or_update_check", "calibrate_check"]

# Sections whose brace tokens are audited. Everything else in a template —
# the Target request body half of target_validate_check_prompt.md above all —
# is out of scope on purpose.
SCANNED_SECTIONS = [
    "Prompt Structure",
    "Fill these in yourself",
    "Runtime placeholders",
    "All Available Placeholders",
]

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_BRACE_TOKEN = re.compile(r"\{([^{}\n]*)\}")
# Same identifier-shape filter the backend validator uses: a token counts as a
# placeholder only if its root is identifier-shaped, with dotted/indexed path
# segments allowed. JSON braces never match.
_IDENTIFIER_SHAPED = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.-?[A-Za-z0-9_]+|\[-?[0-9:]*\])*$"
)


def _templates_dir() -> Path:
    import src.tools.docs as docs

    return Path(docs.__file__).parent.parent / "templates"


def _template_text(name: str) -> str:
    return (_templates_dir() / f"{name}.md").read_text()


def _normalize(text: str) -> str:
    """Collapse whitespace and drop markdown backticks, keeping braces."""
    return re.sub(r"\s+", " ", text.replace("`", "")).strip().lower()


def _without_note(text: str) -> str:
    """Normalized text with the rejection note removed.

    The note names the rejected aliases in braces on purpose; scanning it
    would flag the very guidance the note exists to give.
    """
    return _normalize(text).replace(_normalize(REJECTION_NOTE), " ")


def _sections(text: str) -> dict[str, str]:
    """Map each markdown heading to the body text beneath it.

    Fenced blocks are opaque: the check prompts themselves use ``##`` headings
    inside the fenced example, and those are prompt text, not document
    structure.
    """
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        heading = None if fenced else _HEADING.match(line)
        if heading:
            if current is not None:
                sections[current] = "\n".join(buffer)
            current = heading.group(2)
            buffer = []
        else:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer)
    return sections


def _placeholder_roots(text: str) -> set[str]:
    """Roots of the identifier-shaped brace tokens in a chunk of guidance."""
    roots = set()
    for raw in _BRACE_TOKEN.findall(_without_note(text)):
        token = raw.strip()
        if not _IDENTIFIER_SHAPED.match(token):
            continue
        roots.add(token.split(".")[0].split("[")[0])
    return roots


def _table_placeholders(body: str) -> set[str]:
    """Placeholder names from the first column of a markdown table."""
    names = set()
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cell = stripped.strip("|").split("|")[0].strip().strip("`")
        match = _BRACE_TOKEN.fullmatch(cell)
        if match:
            names.add(match.group(1).strip())
    return names


def _tool_descriptions() -> dict[str, str]:
    from mcp.server.fastmcp import FastMCP

    from src.tools.checks import register_tools

    mcp = FastMCP("test")
    register_tools(mcp)
    return {name: tool.description for name, tool in mcp._tool_manager._tools.items()}


def _guidance_surface(surface: str) -> str:
    """A template's audited sections, or a tool's docstring."""
    if surface in CHECK_TEMPLATES:
        sections = _sections(_template_text(surface))
        return "\n".join(sections.get(name, "") for name in SCANNED_SECTIONS)
    return _tool_descriptions()[surface]


ALL_SURFACES = CHECK_TEMPLATES + DOCSTRING_TOOLS


class TestScannedSectionsExist:
    """Section scoping only works if the sections are actually there."""

    @pytest.mark.parametrize("name", CHECK_TEMPLATES)
    @pytest.mark.parametrize("section", SCANNED_SECTIONS)
    def test_section_present(self, name, section):
        assert section in _sections(_template_text(name)), (
            f"{name}.md is missing the '{section}' section"
        )


class TestOnlyCurrentPlaceholders:
    @pytest.mark.parametrize("surface", ALL_SURFACES)
    def test_no_unknown_brace_token(self, surface):
        roots = _placeholder_roots(_guidance_surface(surface))
        unknown = roots - VALID_PLACEHOLDERS
        assert unknown == set(), (
            f"{surface} documents placeholders Okareo rejects: {sorted(unknown)}"
        )

    @pytest.mark.parametrize("surface", ALL_SURFACES)
    def test_no_deprecated_alias(self, surface):
        roots = _placeholder_roots(_guidance_surface(surface))
        assert roots & DEPRECATED_ALIASES == set()

    @pytest.mark.parametrize("surface", ALL_SURFACES)
    def test_guidance_never_calls_an_alias_deprecated(self, surface):
        """Reject, not deprecate: 'deprecated' reads as 'still works'."""
        assert "deprecated" not in _guidance_surface(surface).lower()


class TestAuthorFillSlots:
    @pytest.mark.parametrize("name", CHECK_TEMPLATES)
    def test_slots_are_angle_bracketed_everywhere(self, name):
        text = _template_text(name)
        for slot in AUTHOR_FILL_SLOTS[name]:
            assert f"<{slot}>" in text, f"{name}.md: <{slot}> slot missing"
            assert f"{{{slot}}}" not in text, (
                f"{name}.md still writes {slot} as a runtime placeholder; an "
                "agent copying the example would have the prompt rejected"
            )

    @pytest.mark.parametrize("name", CHECK_TEMPLATES)
    def test_slots_appear_in_the_copied_example(self, name):
        """An agent copies the fenced block, so the slots must be in it."""
        block = _sections(_template_text(name))["Prompt Structure"]
        for slot in AUTHOR_FILL_SLOTS[name]:
            assert f"<{slot}>" in block

    @pytest.mark.parametrize("name", CHECK_TEMPLATES)
    def test_slots_are_listed_in_the_fill_these_in_table(self, name):
        body = _sections(_template_text(name))["Fill these in yourself"]
        for slot in AUTHOR_FILL_SLOTS[name]:
            assert f"<{slot}>" in body

    @pytest.mark.parametrize("name", CHECK_TEMPLATES)
    def test_fill_these_in_table_carries_no_brace_tokens(self, name):
        body = _sections(_template_text(name))["Fill these in yourself"]
        assert _placeholder_roots(body) == set()


class TestPlaceholderTables:
    @pytest.mark.parametrize("name", CHECK_TEMPLATES)
    def test_runtime_table_lists_only_current_placeholders(self, name):
        body = _sections(_template_text(name))["Runtime placeholders"]
        listed = _table_placeholders(body)
        assert listed, f"{name}.md: runtime placeholder table is empty"
        assert listed <= VALID_PLACEHOLDERS

    @pytest.mark.parametrize("name", CHECK_TEMPLATES)
    def test_all_available_table_is_exactly_the_ten_current_variables(self, name):
        body = _sections(_template_text(name))["All Available Placeholders"]
        assert _table_placeholders(body) == VALID_PLACEHOLDERS

    @pytest.mark.parametrize("name", CHECK_TEMPLATES)
    def test_user_only_audio_is_documented(self, name):
        """Added by 042: the audio-only variable was missing from all four."""
        body = _sections(_template_text(name))["All Available Placeholders"]
        assert "user_only_audio" in _table_placeholders(body)

    @pytest.mark.parametrize("surface", DOCSTRING_TOOLS)
    def test_docstrings_name_every_current_placeholder(self, surface):
        roots = _placeholder_roots(_tool_descriptions()[surface])
        assert roots == VALID_PLACEHOLDERS


class TestRejectionNote:
    @pytest.mark.parametrize("surface", ALL_SURFACES)
    def test_note_present_verbatim(self, surface):
        if surface in CHECK_TEMPLATES:
            text = _template_text(surface)
        else:
            text = _tool_descriptions()[surface]
        assert _normalize(REJECTION_NOTE) in _normalize(text), (
            f"{surface} is missing the placeholder rejection note"
        )


class TestExpectedResultIsGone:
    """`{expected_result}` is not a template variable and never was.

    target_validate_check_prompt.md used it as if it were: the substitutor
    passes unknown tokens through, so the judge received the literal text
    `{expected_result}` instead of the scenario's expected output. The
    variable that holds it is `{scenario_result}`.

    Token match, not substring: basic_scenario.md legitimately carries
    `{expected_result_1}` in a different (scenario-generation) namespace.
    """

    def test_no_template_uses_the_expected_result_token(self):
        offenders = [
            path.name
            for path in sorted(_templates_dir().glob("*.md"))
            if re.search(r"\{expected_result\}", path.read_text())
        ]
        assert offenders == []

    def test_target_validate_reads_the_scenario_result(self):
        block = _sections(_template_text("target_validate_check_prompt"))[
            "Prompt Structure"
        ]
        assert "{scenario_result}" in block
