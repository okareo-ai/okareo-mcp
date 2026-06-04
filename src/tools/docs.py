"""Documentation and template tools for the Okareo MCP server.

Provides two MCP tools:

- get_docs: Query the Okareo documentation RAG system for conceptual or
  user-legible explanations of Okareo primitives and workflows. Respects the
  AIRGAP environment variable for graceful degradation in restricted networks.

- get_templates: Retrieve prompt templates for common Okareo patterns. Served
  as static content from local files — always available, even offline.
"""

import json
import os
from importlib.resources import files
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from src.analytics import is_truthy
from src.error_handling import format_tool_error

MAPI_BASE_URL = os.environ.get("OKAREO_MAPI_BASE_URL", "https://mapi.okareo.com")

TEMPLATE_NAMES = [
    "basic_scenario",
    "boolean_check_prompt",
    "score_check_prompt",
    "check_code",
    "target_validate_check_prompt",
    "driver_prompt",
    "driver_voice_extension_prompt",
    "analysis_check_prompt",
    "custom_endpoint_streaming",
    "voice_augmentations",
]

TEMPLATE_DESCRIPTIONS = {
    "basic_scenario": "Template for creating a basic Okareo test scenario with input/result rows.",
    "boolean_check_prompt": "Template for a pass/fail (boolean) check that returns true/false.",
    "score_check_prompt": "Template for a scored check that returns a numeric score (e.g., 1-5).",
    "check_code": "Template for a code-based check using a Python function for deterministic evaluation.",
    "target_validate_check_prompt": "Template for validating target output against expected results from the scenario.",
    "driver_prompt": "Template for a Driver persona prompt defining a simulated user for multi-turn simulations.",
    "driver_voice_extension_prompt": "Template for extending a Driver persona with voice-specific interaction behaviors.",
    "analysis_check_prompt": "Template for an analysis check that returns qualitative written feedback instead of a score or pass/fail.",
    "custom_endpoint_streaming": "SSE streaming configuration for custom endpoint targets — stop conditions, select filters, and common patterns.",
    "voice_augmentations": "Voice simulation augmentations — the five strategies (cap, directed_speech, secondary_speaker, backchannel, barge_in), composable noise add-on, composition rule, per-field bounds, and copy-paste examples.",
}


def _load_template(template_name: str) -> str:
    """Read a prompt template fresh from disk on each call.

    Uses importlib.resources so it works in editable installs, pip installs,
    and uvx environments. Reading at invocation time (not import time) means
    template edits are reflected immediately without server restart.
    """
    return (
        files("src.templates")
        .joinpath(f"{template_name}.md")
        .read_text(encoding="utf-8")
    )


def register_tools(mcp: FastMCP) -> None:
    """Register documentation and template tools with the FastMCP server."""

    @mcp.tool(
        title="Query Okareo Documentation",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    def get_docs(
        query: str,
        mode: str,
        top_k: Optional[int] = None,
    ) -> str:
        """Query the Okareo documentation system for information about Okareo primitives and workflows.

        Use this tool when the agent or user needs to understand how Okareo
        concepts work — Scenarios, Checks, Targets, Drivers, Evaluations,
        and Simulations.

        Two modes are available:
        - 'conceptual': Detailed technical documentation for agent reasoning.
          Default top_k=5 (returns up to 5 documentation entries).
        - 'user_legible': Plain-language explanations for human users.
          Default top_k=3 (returns up to 3 documentation entries).

        If the Okareo documentation service is unavailable (e.g. air-gapped
        environment), the tool returns a helpful error suggesting get_templates
        as a fallback.

        Args:
            query: The question to ask the Okareo documentation system. Be
                specific — e.g., 'How do Checks and Evaluations work together?'
                or 'What is a Driver persona?'
            mode: Documentation mode — 'conceptual' or 'user_legible'.
            top_k: Number of documentation entries to return. Defaults to 5
                for conceptual mode, 3 for user_legible mode. Maximum 10.
        """
        # Input validation
        if not query or not query.strip():
            return json.dumps({
                "error": "query is required. Ask a specific question about Okareo concepts.",
            })

        if mode not in ("conceptual", "user_legible"):
            return json.dumps({
                "error": "mode must be 'conceptual' or 'user_legible'.",
            })

        # Resolve top_k
        if top_k is None or top_k <= 0:
            resolved_top_k = 5 if mode == "conceptual" else 3
        elif top_k > 10:
            resolved_top_k = 10
        else:
            resolved_top_k = top_k

        # Airgap gate: skip all external calls when AIRGAP is enabled
        if is_truthy(os.environ.get("AIRGAP")):
            return json.dumps({
                "error": (
                    "Documentation service is disabled in airgap mode. "
                    "No external network calls are made when AIRGAP is enabled."
                ),
                "suggestion": "Use get_templates for prompt templates and examples.",
            })

        api_key = os.environ.get("OKAREO_API_KEY", "")
        headers = {"api-key": api_key}

        # Docs RAG call
        try:
            response = httpx.post(
                f"{MAPI_BASE_URL}/v1/docs",
                headers=headers,
                json={"query": query, "mode": mode, "topK": resolved_top_k},
            )
            response.raise_for_status()
        except Exception as e:
            return format_tool_error(e)

        try:
            results = response.json()
        except (ValueError, TypeError):
            return json.dumps({
                "error": "Failed to fetch documentation: unexpected response format.",
                "suggestion": "Use get_templates for prompt templates and examples.",
            })

        # Normalize results to a list
        if isinstance(results, list):
            entries = results
        elif isinstance(results, dict) and "results" in results:
            entries = results["results"]
        else:
            entries = [results] if results else []

        return json.dumps({
            "mode": mode,
            "query": query,
            "top_k": resolved_top_k,
            "results": entries,
            "count": len(entries),
        })

    @mcp.tool(
        title="Get Okareo Templates",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def get_templates(
        template_name: Optional[str] = None,
    ) -> str:
        """Retrieve prompt templates for common Okareo patterns.

        Returns starter templates for building Okareo test components. These
        templates are served as static content from the MCP — no network calls
        required. Always available, even in air-gapped environments.

        Available templates:
        - basic_scenario: Template for creating a basic Okareo test scenario
        - boolean_check_prompt: Template for a pass/fail (boolean) check prompt
        - score_check_prompt: Template for a scored check prompt
        - check_code: Template for a code-based check (Python function)
        - target_validate_check_prompt: Template for validating target output
        - driver_prompt: Template for a Driver persona prompt
        - driver_voice_extension_prompt: Template for voice interaction extensions
        - analysis_check_prompt: Template for an analysis check (qualitative feedback)

        Args:
            template_name: Template identifier to retrieve. Omit to get a
                lightweight listing of all available templates (names and
                descriptions only). Provide a template_name to get the full
                template content. Valid values: basic_scenario,
                boolean_check_prompt, score_check_prompt, check_code,
                target_validate_check_prompt, driver_prompt,
                driver_voice_extension_prompt, analysis_check_prompt.
        """
        if template_name is not None:
            if template_name not in TEMPLATE_NAMES:
                return json.dumps({
                    "error": f"Template '{template_name}' not found.",
                    "available_templates": TEMPLATE_NAMES,
                })
            try:
                content = _load_template(template_name)
            except Exception as e:
                return format_tool_error(e)
            return json.dumps({
                "template_name": template_name,
                "content": content,
            })

        # Return lightweight listing (names + descriptions only)
        templates = []
        for name in TEMPLATE_NAMES:
            templates.append({
                "template_name": name,
                "description": TEMPLATE_DESCRIPTIONS[name],
            })

        return json.dumps({
            "templates": templates,
            "count": len(templates),
        })

    return None
