"""Unit tests for MCP server instructions (FR-009, SC-003)."""

import re


# All 29 registered tool names across 5 domains
REGISTERED_TOOLS = {
    # Scenarios
    "save_scenario",
    "list_scenarios",
    "get_scenario",
    "create_scenario_version",
    "preview_delete_scenario",
    "delete_scenario",
    # Generation Models
    "list_available_llms",
    "register_generation_model",
    "list_generation_models",
    "get_generation_model",
    "update_generation_model",
    "delete_generation_model",
    # Tests & Checks
    "list_checks",
    "run_test",
    "list_test_runs",
    "get_test_run_results",
    "get_conversation_transcript",
    "create_or_update_check",
    "generate_check",
    "get_check",
    "delete_check",
    # Simulations
    "create_or_update_target",
    "get_target",
    "list_targets",
    "delete_target",
    "create_or_update_driver",
    "get_driver",
    "list_drivers",
    "list_driver_voices",
    "run_simulation",
    "list_simulations",
    # Re-evaluation (022-sdk-132-upgrade)
    "reevaluate_test_run",
    # Voice monitoring (022-sdk-132-upgrade)
    "ingest_conversations",
    "connect_voice_integration",
    "list_voice_integrations",
    "get_voice_integration",
    "update_voice_integration",
    "rotate_voice_integration_secret",
    "delete_voice_integration",
    "get_voice_webhook_url",
    # Analytics & dashboards (022-sdk-132-upgrade)
    "query_analytics",
    "list_dashboards",
    "get_dashboard",
    "save_dashboard",
    "reorder_dashboards",
    "delete_dashboard",
    # Documentation
    "get_docs",
    "get_templates",
    # Tenant management (FR-023..FR-029, added 2026-05-18)
    "list_tenants",
    "switch_tenant",
}


def _get_instructions() -> str:
    """Import the mcp server and return its instructions string."""
    from src.server import mcp

    return mcp.instructions


def test_instructions_not_empty():
    """Instructions string must be present and non-empty (FR-001)."""
    instructions = _get_instructions()
    assert instructions is not None
    assert len(instructions.strip()) > 0


def test_instructions_tool_names_valid():
    """Every tool name referenced in instructions must be a registered tool (FR-009, SC-003)."""
    instructions = _get_instructions()

    # Extract all snake_case identifiers that look like tool names
    # Match word boundaries around snake_case names (2+ segments or known single-word tools)
    candidates = set(re.findall(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b", instructions))

    # Filter to only names that are actual tool references (exclude common phrases)
    non_tool_patterns = {
        "detail_level",
        "quality_checks",
        "driver_personas",
        "check_prompts",
        "test_data",
        "test_cases",
        "next_message_params",
        "custom_endpoint",
        "endpoint_targets",
        "test_run_id",
        "app_link",
        "model_metrics",
        "scenario_index",
        # Tenant-selection response fields + error codes (not tool names)
        "tenant_id",
        "active_tenant_id",
        "active_tenant_source",
        "tenant_selection_requires_oauth",
        # Parameter names referenced in instructions (not tool names)
        "all_versions",
        "call_id",
    }
    tool_references = candidates - non_tool_patterns

    # Every tool reference in the instructions must exist in REGISTERED_TOOLS
    invalid = tool_references - REGISTERED_TOOLS
    assert invalid == set(), f"Instructions reference unknown tools: {invalid}"


def test_instructions_contains_all_domains():
    """Instructions must describe all five tool domains (FR-002)."""
    instructions = _get_instructions()
    assert "Scenarios" in instructions
    assert "Generation Models" in instructions
    assert "Tests" in instructions or "Checks" in instructions
    assert "Simulations" in instructions
    assert "Documentation" in instructions


def test_instructions_contains_evaluation_workflow():
    """Instructions must define the evaluation workflow sequence (FR-003)."""
    instructions = _get_instructions()
    assert "save_scenario" in instructions
    assert "register_generation_model" in instructions
    assert "run_test" in instructions
    assert "get_test_run_results" in instructions


def test_instructions_contains_simulation_workflow():
    """Instructions must define the simulation workflow sequence (FR-004)."""
    instructions = _get_instructions()
    assert "create_or_update_target" in instructions
    assert "create_or_update_driver" in instructions
    assert "run_simulation" in instructions


def test_instructions_contains_documentation_guidance():
    """Instructions must recommend get_docs and get_templates (FR-007)."""
    instructions = _get_instructions()
    assert "get_docs" in instructions
    assert "get_templates" in instructions
