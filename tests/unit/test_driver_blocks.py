"""E6 (spec 032): canonical driver blocks — vendored parity, idempotent append.

The MCP appends the same Hard Rules and Conversation Behavior blocks the
okareo-server appends to UI-generated drivers. Snapshots in
``tests/unit/snapshots/`` were extracted byte-for-byte from the server source
(``app/drivers/driver_prompt_generator.py::_SUFFIX_TEMPLATE`` and
``app/drivers/voice_guidance.py::VOICE_CALL_BEHAVIORS``) at release time.
"""

from pathlib import Path

from src.driver_blocks import (
    CONVERSATION_BEHAVIOR,
    HARD_RULES_TEMPLATE,
    append_canonical_blocks,
    build_language_display,
    strip_canonical_sections,
)

SNAPSHOTS = Path(__file__).parent / "snapshots"

CORE_PROMPT = """## Persona

-   **Identity:** You are role-playing a new user named **Taylor**.

## Scenario Details

{scenario_input}

## Objectives

1. Get the other party to list three supported tasks.

## Soft Tactics

1. If the reply is vague, politely probe."""


class TestVendoredParity:
    def test_hard_rules_matches_server_snapshot(self):
        expected = (SNAPSHOTS / "hard_rules_template.txt").read_text()
        assert HARD_RULES_TEMPLATE == expected

    def test_conversation_behavior_matches_server_snapshot(self):
        expected = (SNAPSHOTS / "conversation_behavior.txt").read_text()
        assert CONVERSATION_BEHAVIOR == expected


class TestLanguageDisplay:
    def test_default_is_english(self):
        assert build_language_display(None) == "English"
        assert build_language_display("en") == "English"

    def test_simple_code_gets_name_and_code(self):
        assert build_language_display("es") == "Spanish (es)"

    def test_regional_variant(self):
        assert build_language_display("fr-CA") == "French (Canada) (fr-CA)"

    def test_language_rule_line_rendered(self):
        out = append_canonical_blocks(CORE_PROMPT, language="es")
        assert (
            "-   Always and only respond in Spanish (es). "
            "Never respond in any other language." in out
        )


class TestAppendCanonicalBlocks:
    def test_appends_both_blocks_after_core(self):
        out = append_canonical_blocks(CORE_PROMPT)
        assert out.startswith("## Persona")
        assert "{scenario_input}" in out
        assert out.count("## Hard Rules") == 1
        assert out.count("## Turn-End Checklist") == 1
        assert out.count("## Conversation Behavior") == 1
        # Ordering: core, then Hard Rules, then Conversation Behavior.
        assert out.index("## Objectives") < out.index("## Hard Rules")
        assert out.index("## Hard Rules") < out.index("## Conversation Behavior")

    def test_idempotent(self):
        once = append_canonical_blocks(CORE_PROMPT, language="es")
        twice = append_canonical_blocks(once, language="es")
        assert once == twice

    def test_caller_authored_variants_replaced_by_canonical(self):
        prompt = CORE_PROMPT + (
            "\n\n## Hard Rules\n\n- My own rule that conflicts.\n"
            "\n## Conversation Behavior\n\nBe chatty and helpful.\n"
        )
        out = append_canonical_blocks(prompt)
        assert "My own rule that conflicts" not in out
        assert "Be chatty and helpful" not in out
        assert out.count("## Hard Rules") == 1
        assert out.count("## Conversation Behavior") == 1
        assert "Never act like a helpful assistant." in out

    def test_language_update_replaces_old_rule(self):
        """Re-appending with a different language swaps the language rule."""
        spanish = append_canonical_blocks(CORE_PROMPT, language="es")
        japanese = append_canonical_blocks(spanish, language="ja")
        assert "Spanish (es)" not in japanese
        assert "Japanese (ja)" in japanese

    def test_old_structure_prompt_preserved_and_extended(self):
        """A pre-enhancement prompt (old section names) gains the blocks
        without its body being corrupted."""
        old = (
            "## Role\nYou are Alex, a frustrated customer.\n\n"
            "## Primary Objectives\n1. Get a refund. {scenario_input}\n"
        )
        out = append_canonical_blocks(old)
        assert "## Role" in out
        assert "Get a refund. {scenario_input}" in out
        assert "## Hard Rules" in out
        assert "## Conversation Behavior" in out

    def test_strip_returns_core_only(self):
        appended = append_canonical_blocks(CORE_PROMPT)
        assert strip_canonical_sections(appended) == CORE_PROMPT.strip()
