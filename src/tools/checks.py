"""Check management tools for the Okareo MCP server.

Provides five MCP tools for creating, generating, reading, calibrating, and
deleting checks:

- create_or_update_check: Create or update a quality check by name (upsert)
- generate_check: Generate a check from a natural language description
- get_check: Retrieve the full configuration of a check by name
- delete_check: Permanently delete a check by name
- calibrate_check: Dry-run a draft check against a finished test run, saving
  nothing — per-row verdicts plus the arguments the check actually received
"""

import json
import uuid
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from src.error_handling import format_tool_error
from src.okareo_client import organization_scoped
from src.okareo_client import get_okareo_client
from src.okareo_client import okareo_api_request


def _get_attr(obj, attr, default=None):
    """Get an attribute, returning default if Unset."""
    val = getattr(obj, attr, default)
    if type(val).__name__ == "Unset":
        return default
    return val


def _serialize_value(val):
    """Serialize a value that may be Unset, a complex object, or a primitive."""
    if val is None:
        return None
    if type(val).__name__ == "Unset":
        return None
    if hasattr(val, "additional_properties"):
        return dict(val.additional_properties)
    if hasattr(val, "to_dict"):
        return val.to_dict()
    if isinstance(val, (dict, list, str, int, float, bool)):
        return val
    return str(val)


def _resolve_check_by_name(okareo, name: str):
    """Find a check by name from the list of all checks.

    Returns:
        Tuple of (check_id, check_brief) if found, or (None, None) if not found.
    """
    checks = okareo.get_all_checks()
    for check in checks:
        check_name = _get_attr(check, "name", "")
        if check_name == name:
            check_id = _get_attr(check, "id")
            return (str(check_id), check)
    return (None, None)


def _check_version(obj):
    """Read a check's version number from its additional_properties dict.

    The generated SDK models carry per-version metadata (added in 0.0.132) in
    `additional_properties` rather than as a typed field.
    """
    props = getattr(obj, "additional_properties", None)
    if isinstance(props, dict):
        v = props.get("version")
        if isinstance(v, int):
            return v
    return None


def _check_tags(obj):
    """Read a check's tags list from its additional_properties dict."""
    props = getattr(obj, "additional_properties", None)
    if isinstance(props, dict):
        tags = props.get("tags")
        if isinstance(tags, list):
            return tags
    return []


_SHARED_NOTE = (
    "Checks are shared across every project in your organization, not private to the project you are working in."
)

# The backend caps a calibration run at its own wall clock (600s) and answers
# 504 when it is exceeded. The client waits longer on purpose so the server's
# 504 always wins the race — otherwise the caller sees a transport error and
# cannot tell a slow judge from a dead route. Keep the two numbers coupled.
#
# 720, not 630: the server tests its wall clock *between* rows, never inside
# one, so a row that starts at 599s runs to completion before the 504 is
# raised. The worst-case row is one judge call plus its retry — the judge
# retries once when the first completion does not parse to a score — each
# bounded by the backend's LLM_TIMEOUT (30s by default). That puts the server's
# own answer as late as ~660s, and the margin covers it with room to spare.
_CALIBRATE_TIMEOUT_SECONDS = 720

# Mirrors CheckCalibrateRequest.name's max_length on the route. The reserved-
# name rule that route also applies is deliberately not mirrored — see
# specs/042-calibrate-check/data-model.md.
_CALIBRATE_NAME_MAX_CHARS = 200

_CALIBRATE_NEXT_STEP = (
    "Nothing was saved. When the verdicts look right, create the check with "
    "create_or_update_check, which needs name, description, check_type, "
    "output_type, and the same prompt_template or code_contents — plus "
    "is_audio for an audio draft."
)

# inspect_only runs no judge, so there are no verdicts to look right. The next
# step is a real calibration, not a save.
_CALIBRATE_INSPECT_NEXT_STEP = (
    "Nothing was saved and no judge ran, so there are no verdicts. Read the "
    "arguments to confirm the variables the draft depends on are populated, "
    "then calibrate the same draft again with inspect_only=false to score the "
    "rows."
)

# Two unrelated failures share the 422 on this route. A draft-shaped 422 (a bad
# placeholder, code that fails validation) means rewrite the draft. These two
# phrases come from TestRunLookup.load_rescorable and mean the opposite: the
# draft is fine and the Test Run is not scoreable — it has not finished, or it
# is a type rescore does not handle.
_RUN_INELIGIBLE_422_PHRASES = (
    "is not re-evaluatable while status is",
    "has unsupported type",
)


def _calibration_refusal(error: httpx.HTTPStatusError) -> str:
    """Map a non-2xx calibration response onto guidance the agent can act on."""
    status = error.response.status_code
    try:
        body = error.response.json()
    except Exception:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, default=str)

    if status == 422:
        if detail and any(
            phrase in detail for phrase in _RUN_INELIGIBLE_422_PHRASES
        ):
            # The Test Run is the problem, not the draft. Telling the agent to
            # rewrite a draft that is already correct is the one piece of
            # advice that cannot help here.
            return json.dumps({
                "error": detail,
                "retryable": True,
                "suggestion": (
                    "The draft was not rejected — the test run is not "
                    "scoreable. If it is still running, wait for it to finish "
                    "(list_test_runs reports its status) and calibrate the "
                    "same draft again. If its type is unsupported, calibrate "
                    "the same draft against a finished generation or "
                    "multi-turn run instead. Do not rewrite the draft."
                ),
            })
        # The draft was refused before a single row was evaluated, and the
        # server's message names the offending placeholder or the code error.
        # Relaying it verbatim IS the feedback loop this tool exists for.
        return json.dumps({
            "error": detail or "The draft check was rejected; no row was evaluated.",
            "retryable": False,
            "suggestion": (
                "Fix the draft and calibrate again. Nothing was evaluated, "
                "nothing was charged, and nothing was saved."
            ),
        })
    if status == 404:
        return json.dumps({
            "error": detail or "Test run not found.",
            "suggestion": (
                "Call list_test_runs to find a finished run, then calibrate "
                "against its id."
            ),
        })
    if status == 504:
        return json.dumps({
            "error": detail or "Calibration exceeded the server's wall-clock limit.",
            "retryable": True,
            "suggestion": (
                "Retry with inspect_only=true to read the arguments at no "
                "cost, or calibrate against a run with fewer rows."
            ),
        })
    return json.dumps({
        "error": detail or f"Calibration failed with HTTP {status}.",
    })


def register_tools(mcp: FastMCP) -> None:
    """Register all check management tools with the FastMCP server."""

    @mcp.tool(
        title="Create or Update Check",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @organization_scoped(_SHARED_NOTE)
    def create_or_update_check(
        name: str,
        description: str,
        check_type: str,
        output_type: str,
        prompt_template: Optional[str] = None,
        code_contents: Optional[str] = None,
        is_audio: bool = False,
        tags: Optional[list[str]] = None,
    ) -> str:
        """Create or update a quality check by name (upsert). Supports model-based, code-based, and audio checks.

        Saving to an existing name creates a new version of that check (see
        get_check's "available_versions"). Before writing a prompt_template or
        code_contents from scratch, fetch a worked example with get_templates:
        "boolean_check_prompt", "score_check_prompt", "analysis_check_prompt",
        or "check_code".

        Args:
            name: Unique name for the check.
            description: What the check evaluates.
            check_type: "model" (an LLM judge driven by prompt_template) or
                "code" (a deterministic Python class in code_contents).
            output_type: "pass_fail" (boolean verdict), "score" (numeric, e.g.
                a 1-5 rubric), or "analysis" (free-form qualitative feedback;
                only valid with check_type="model"). For check_type="code" the
                server infers pass_fail vs score from the value evaluate()
                returns (bool vs int/float) — output_type is used only to
                validate the request, not sent to the server. Note: list_checks
                and get_check report this as output_data_type in the server
                vocabulary, where "bool" means pass_fail and "int" means score.
            prompt_template: Required when check_type="model". The judge
                prompt. Write the criterion, rubric, or instructions in your
                own words, and inject the runtime data the judge needs with
                these placeholders — the complete set Okareo substitutes:
                - {model_output}: the model output being evaluated. In a
                  multi-turn conversation this is ONLY the final assistant
                  message, not the full conversation.
                - {scenario_input}: the scenario input / source text.
                - {scenario_result}: the reference/expected output.
                - {model_input}: what was sent to the model (prompt or
                  messages).
                - {message_history}: the full multi-turn conversation — the
                  model_input messages plus the assistant's model_output. Use
                  this when the check must judge the whole conversation.
                - {tool_calls}: the tool/function calls the model just made.
                - {tools}: the tool definitions/schema available to the model.
                - {model_output_metadata}: metadata attached to the most
                  recent model output.
                - {simulation_message_history}: full conversation history
                  reconstructed from trace metadata. Only populated for traced
                  (ingested) conversations; for simulations and evaluations
                  use {message_history}.
                - {user_only_audio}: the user's audio only, for
                  speaker-scoped audio checks. Audio checks only — empty on
                  text evaluations.
                Okareo rejects any placeholder that is not listed above. That
                includes the legacy aliases {generation}, {input}, {result},
                {audio_messages}, and {audio_output} — use {model_output},
                {scenario_input}, {scenario_result}, and {user_only_audio}
                instead. A rejected prompt fails on save and fails
                calibrate_check.
            code_contents: Required when check_type="code" (output_type
                "pass_fail" or "score" only). Python source defining
                `class Check(CodeBasedCheck)` with a
                `@staticmethod def evaluate(...) -> CheckResponse` method.
                Start from `from okareo.checks import CodeBasedCheck,
                CheckResponse`. evaluate() may declare any subset of these
                parameters: model_output, scenario_input, scenario_result,
                metadata, model_input. Return CheckResponse(score=...,
                explanation=...) where score is a bool for pass_fail or an
                int/float for score. See get_templates("check_code") for
                complete examples.
            is_audio: Set to true for audio/voice evaluation. Only valid with
                check_type="model".
            tags: Optional list of string tags to organize the check. Tags are
                stored with the check and returned by get_check.
        """
        from okareo_api_client.api.default import (
            check_create_or_update_v0_check_create_or_update_post,
        )
        from okareo_api_client.models.check_create_update_schema import (
            CheckCreateUpdateSchema,
        )
        from okareo_api_client.models.check_create_update_schema_check_config_type_0 import (
            CheckCreateUpdateSchemaCheckConfigType0,
        )

        # Validate check_type
        if check_type not in ("model", "code"):
            return json.dumps({
                "error": "check_type must be 'model' or 'code'.",
            })

        # Validate output_type
        if output_type not in ("pass_fail", "score", "analysis"):
            return json.dumps({
                "error": "output_type must be 'pass_fail', 'score', or 'analysis'.",
            })

        # Validate name
        if not name or not name.strip():
            return json.dumps({"error": "name is required and cannot be empty."})

        # Validate: analysis requires model
        if output_type == "analysis" and check_type == "code":
            return json.dumps({
                "error": "analysis output_type is only supported with check_type='model'.",
            })

        # Validate: audio requires model
        if is_audio and check_type == "code":
            return json.dumps({
                "error": "Audio checks are only supported with check_type='model'.",
            })

        # Validate conditional fields
        if check_type == "model" and not prompt_template:
            return json.dumps({
                "error": "prompt_template is required for model-based checks.",
            })

        if check_type == "code" and not code_contents:
            return json.dumps({
                "error": "code_contents is required for code-based checks.",
            })

        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        try:
            if check_type == "model":
                if output_type in ("pass_fail", "score"):
                    # Use SDK's ModelBasedCheck for pass_fail and score
                    from okareo.checks import CheckOutputType, ModelBasedCheck

                    sdk_output_type = (
                        CheckOutputType.PASS_FAIL
                        if output_type == "pass_fail"
                        else CheckOutputType.SCORE
                    )
                    check = ModelBasedCheck(
                        prompt_template=prompt_template,
                        check_type=sdk_output_type,
                        is_audio=is_audio,
                    )
                    response = okareo.create_or_update_check(
                        name=name,
                        description=description,
                        check=check,
                        tags=tags,
                    )
                else:
                    # analysis: bypass SDK enum, use low-level API directly
                    config_dict = {
                        "prompt_template": prompt_template,
                        "type": "analysis",
                        "audio": is_audio,
                    }
                    check_config = CheckCreateUpdateSchemaCheckConfigType0.from_dict(
                        config_dict
                    )
                    body = CheckCreateUpdateSchema(
                        name=name,
                        description=description,
                        check_config=check_config,
                    )
                    if tags is not None:
                        body["tags"] = tags
                    response = check_create_or_update_v0_check_create_or_update_post.sync(
                        client=okareo.client,
                        api_key=okareo.api_key,
                        body=body,
                    )
            else:
                # code-based: bypass CodeBasedCheck's inspect.getmodule(),
                # use low-level API directly. No "type" is sent — since SDK
                # 0.0.144 the server infers pass/fail vs score from the value
                # evaluate() returns.
                config_dict = {
                    "code_contents": code_contents,
                }
                check_config = CheckCreateUpdateSchemaCheckConfigType0.from_dict(
                    config_dict
                )
                body = CheckCreateUpdateSchema(
                    name=name,
                    description=description,
                    check_config=check_config,
                )
                if tags is not None:
                    body["tags"] = tags
                response = check_create_or_update_v0_check_create_or_update_post.sync(
                    client=okareo.client,
                    api_key=okareo.api_key,
                    body=body,
                )
        except Exception as e:
            return format_tool_error(e)

        return json.dumps({
            "id": str(_get_attr(response, "id", "")),
            "name": name,
            "description": description,
            "check_type": check_type,
            "output_type": output_type,
            "is_audio": is_audio,
            "tags": tags or [],
            "created": True,
            "message": f"Check '{name}' saved successfully.",
        }, default=str)

    @mcp.tool(
        title="Generate Check",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    @organization_scoped(_SHARED_NOTE)
    def generate_check(
        name: str,
        description: str,
        output_type: str = "pass_fail",
        check_type: str = "model",
        requires_scenario_input: bool = False,
        requires_scenario_result: bool = False,
    ) -> str:
        """Generate a check from a natural language description. Uses AI to create the prompt template (model checks) or Python code (code checks), then saves the check.

        Use this when you only have a description of what to evaluate. When
        you already know the exact prompt template or Python code the check
        should use, call create_or_update_check directly instead. The
        generated prompt/code is returned in the response — review it and
        refine with create_or_update_check if needed.

        Args:
            name: Name for the generated check.
            description: Natural language description of what to evaluate
                (e.g., "check if the response is toxic"). The more specific
                the description, the better the generated check.
            output_type: "pass_fail" (boolean verdict), "score" (numeric), or
                "analysis" (free-form qualitative feedback; model checks only).
            check_type: "model" (LLM judge) or "code" (deterministic Python).
            requires_scenario_input: Set true when the evaluation must compare
                the output against the scenario input. The generated check
                will reference {scenario_input} and only works on runs whose
                scenarios provide it.
            requires_scenario_result: Set true when the evaluation must
                compare the output against the expected result. The generated
                check will reference {scenario_result} and only works on runs
                whose scenarios provide it.
        """
        from okareo_api_client.models.evaluator_spec_request import (
            EvaluatorSpecRequest,
        )

        # Validate inputs
        if check_type not in ("model", "code"):
            return json.dumps({
                "error": "check_type must be 'model' or 'code'.",
            })

        if output_type not in ("pass_fail", "score", "analysis"):
            return json.dumps({
                "error": "output_type must be 'pass_fail', 'score', or 'analysis'.",
            })

        if not name or not name.strip():
            return json.dumps({"error": "name is required and cannot be empty."})

        # Map output_type to SDK's output_data_type
        output_data_type_map = {
            "pass_fail": "bool",
            "score": "int",
            "analysis": "analysis",
        }
        output_data_type = output_data_type_map[output_type]

        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        # Build spec request and generate
        try:
            spec = EvaluatorSpecRequest(
                name=name,
                description=description,
                requires_scenario_input=requires_scenario_input,
                requires_scenario_result=requires_scenario_result,
                output_data_type=output_data_type,
                check_type=check_type,
            )
            generated = okareo.generate_check(spec)
        except Exception as e:
            return format_tool_error(e)

        # Extract generated content
        generated_prompt = _get_attr(generated, "generated_prompt")
        generated_code = _get_attr(generated, "generated_code")
        gen_description = _get_attr(generated, "description", description)

        # Save the generated check
        if check_type == "model" and generated_prompt:
            save_result = create_or_update_check(
                name=name,
                description=gen_description,
                check_type="model",
                output_type=output_type,
                prompt_template=generated_prompt,
            )
        elif check_type == "code" and generated_code:
            save_result = create_or_update_check(
                name=name,
                description=gen_description,
                check_type="code",
                output_type=output_type,
                code_contents=generated_code,
            )
        else:
            return json.dumps({
                "error": "Generation succeeded but no prompt or code was returned.",
                "warning": _get_attr(generated, "warning"),
            })

        # Parse the save result to get the check ID
        save_data = json.loads(save_result)
        if "error" in save_data:
            return json.dumps({
                "error": f"Generated check but failed to save: {save_data['error']}",
            })

        return json.dumps({
            "id": save_data.get("id", ""),
            "name": name,
            "description": gen_description,
            "check_type": check_type,
            "output_type": output_type,
            "generated_prompt": generated_prompt,
            "generated_code": generated_code,
            "message": f"Check '{name}' generated and saved successfully.",
        }, default=str)

    @mcp.tool(
        title="Get Check",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @organization_scoped(_SHARED_NOTE)
    def get_check(name: str, version: Optional[int] = None) -> str:
        """Retrieve the full configuration of a check by name, including its prompt template or code contents.

        Args:
            name: Name of the check to retrieve.
            version: Optional check version number to pin. Omit (or leave null)
                for the most recent version. The response always lists every
                available version under "available_versions".
        """
        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        # Gather every version of this check so the response can advertise
        # what's available — and so an unknown `version` produces a helpful
        # error rather than an opaque one.
        try:
            all_checks = okareo.get_all_checks(all_versions=True)
        except Exception as e:
            return format_tool_error(e)
        matches = [c for c in all_checks if _get_attr(c, "name", "") == name]
        if not matches:
            return json.dumps({
                "error": f"Check '{name}' not found. Use list_checks to see available checks.",
            })
        available_versions = sorted(
            v for v in (_check_version(c) for c in matches) if isinstance(v, int)
        )

        # Fetch the detail. Passing the name (not a resolved UUID) lets the SDK
        # apply version selection — an int pins a version, None is latest.
        try:
            detail = okareo.get_check(name, version=version)
        except ValueError as e:
            # SDK raises ValueError listing available versions on a bad pin.
            return json.dumps({
                "error": str(e),
                "available_versions": available_versions,
            })
        except Exception as e:
            return format_tool_error(e)

        check_config = _serialize_value(_get_attr(detail, "check_config"))
        code_contents = _get_attr(detail, "code_contents", "")

        return json.dumps({
            "id": str(_get_attr(detail, "id", "")),
            "name": _get_attr(detail, "name", ""),
            "description": _get_attr(detail, "description", ""),
            "output_data_type": _get_attr(detail, "output_data_type", ""),
            "version": _check_version(detail),
            "available_versions": available_versions,
            "tags": _check_tags(detail),
            "check_config": check_config,
            "code_contents": code_contents,
            "requires_scenario_input": _get_attr(detail, "requires_scenario_input", False),
            "requires_scenario_result": _get_attr(detail, "requires_scenario_result", False),
            "is_predefined": _get_attr(detail, "is_predefined", False),
            "time_created": str(_get_attr(detail, "time_created", "")),
        }, default=str)

    @mcp.tool(
        title="Delete Check",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @organization_scoped(_SHARED_NOTE)
    def delete_check(name: str) -> str:
        """Permanently delete a check by name.

        Args:
            name: Name of the check to delete.
        """
        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        # Resolve name to ID
        check_id, check_brief = _resolve_check_by_name(okareo, name)
        if check_id is None:
            return json.dumps({
                "error": f"Check '{name}' not found. Use list_checks to see available checks.",
            })

        # Reject predefined checks
        if _get_attr(check_brief, "is_predefined", False):
            return json.dumps({
                "error": f"Cannot delete predefined check '{name}'. Only custom checks can be deleted.",
            })

        # Delete
        try:
            okareo.delete_check(check_id, name)
        except Exception as e:
            return format_tool_error(e)

        return json.dumps({
            "deleted": True,
            "name": name,
            "message": f"Check '{name}' has been deleted.",
        })

    @mcp.tool(
        title="Calibrate Check",
        annotations=ToolAnnotations(
            readOnlyHint=True,  # persists nothing: no check, no run, no scores
            destructiveHint=False,
            idempotentHint=False,  # judge verdicts are not reproducible
            openWorldHint=True,  # every evaluated row makes a real LLM call
        ),
    )
    def calibrate_check(
        test_run_id: str,
        check_type: str,
        prompt_template: Optional[str] = None,
        code_contents: Optional[str] = None,
        output_type: Optional[str] = None,
        is_audio: bool = False,
        inspect_only: bool = False,
        name: str = "draft_check",
    ) -> str:
        """Dry-run a draft check against a finished test run: per-row verdicts plus the exact arguments the check received, saving nothing.

        Use this to tune a check before it exists. Write a draft prompt or
        draft code, calibrate it against a run you already have, read the
        per-row verdicts and the values that went in, revise, repeat. It
        persists nothing — no check, no test run, no datapoints, no scores —
        so iterating leaves no litter in the project. When the verdicts are
        right, save the same draft with create_or_update_check.

        What comes back, one entry per test datapoint (the row id the
        evaluation results UI shows, so a verdict traces back to its
        conversation):

        - result: the score or verdict, the judge's explanation, check
          metadata such as latency and cost, and the row's own error if it
          failed. One row erroring does not fail the batch.
        - arguments: what the check actually received for that row, whole —
          values are never truncated, so a JSON one such as metadata still
          parses. A model draft reports the full substituted variable set; a
          code draft reports only the parameters its evaluate() signature
          declares, as raw values.

        Cost and bounds:

        - Every evaluated row costs a real judge call, exactly like rescoring
          that row. inspect_only=true returns the arguments alone and makes
          zero judge calls — use it first to answer "is this variable even
          populated for these rows", which is the usual reason a check scores
          nonsense.
        - At most 100 rows are evaluated per call. The response reports how
          many rows were eligible and flags the cap when it applied, and the
          same run returns the same rows every time, so two iterations are
          comparable.

        Placeholders for prompt_template — the complete set Okareo
        substitutes at evaluation time:
        - {model_output}: the model output being evaluated. In a multi-turn
          conversation this is ONLY the final assistant message, not the full
          conversation.
        - {scenario_input}: the scenario input / source text.
        - {scenario_result}: the reference/expected output.
        - {model_input}: what was sent to the model (prompt or messages).
        - {message_history}: the full multi-turn conversation — the
          model_input messages plus the assistant's model_output.
        - {tool_calls}: the tool/function calls the model just made.
        - {tools}: the tool definitions/schema available to the model.
        - {model_output_metadata}: metadata attached to the most recent model
          output.
        - {simulation_message_history}: full conversation history
          reconstructed from trace metadata. Only populated for traced
          (ingested) conversations; for simulations and evaluations use
          {message_history}.
        - {user_only_audio}: the user's audio only, for speaker-scoped audio
          checks. Audio checks only — empty on text evaluations.
        Okareo rejects any placeholder that is not listed above. That includes
        the legacy aliases {generation}, {input}, {result}, {audio_messages},
        and {audio_output} — use {model_output}, {scenario_input},
        {scenario_result}, and {user_only_audio} instead. A rejected prompt
        fails on save and fails calibrate_check.

        Audio caveat: is_audio=true calibrates, but the row builder supplies
        no audio for test-run rows, so every row comes back with no score and
        the runtime's "No audio data provided" explanation — exactly what
        re-scoring that run would do today. That is a known platform gap, not
        a fault in your draft.

        Args:
            test_run_id: The finished test run whose rows to calibrate
                against, as a UUID from list_test_runs. A run that is still
                running, or in any non-terminal state, is refused.
            check_type: "model" (an LLM judge driven by prompt_template) or
                "code" (a deterministic Python class in code_contents).
                Supply exactly one of the two — a draft carrying both is
                refused here, before any request is sent.
            prompt_template: Required when check_type="model". The judge
                prompt, in the same shape create_or_update_check takes.
            code_contents: Required when check_type="code". Python source in
                the same shape create_or_update_check takes; see
                get_templates("check_code").
            output_type: Required when check_type="model": "pass_fail",
                "score", or "analysis". Not sent for code drafts — the server
                infers a code check's type from what evaluate() returns.
            is_audio: Set true for an audio/voice draft. Only valid with
                check_type="model". See the audio caveat above.
            inspect_only: True returns the arguments for every row and runs
                no judge at all, at no cost.
            name: Name for the draft, at most 200 characters. It labels the
                returned result column only; nothing is created under it.
        """
        if check_type not in ("model", "code"):
            return json.dumps({
                "error": "check_type must be 'model' or 'code'.",
            })

        if prompt_template and code_contents:
            return json.dumps({
                "error": (
                    "Supply exactly one of prompt_template or code_contents, "
                    "never both. A draft carrying both is ambiguous about "
                    "which kind of check it is, and Okareo refuses it."
                ),
            })

        if check_type == "model" and not prompt_template:
            return json.dumps({
                "error": "prompt_template is required for model-based checks.",
            })

        if check_type == "code" and not code_contents:
            return json.dumps({
                "error": "code_contents is required for code-based checks.",
            })

        if check_type == "model" and output_type not in (
            "pass_fail",
            "score",
            "analysis",
        ):
            return json.dumps({
                "error": (
                    "output_type must be 'pass_fail', 'score', or 'analysis' "
                    "for model-based checks. Without it every row errors "
                    "server-side."
                ),
            })

        if is_audio and check_type == "code":
            return json.dumps({
                "error": "Audio checks are only supported with check_type='model'.",
            })

        if not name or not name.strip():
            return json.dumps({"error": "name cannot be empty."})

        # Sent stripped, and bounded the way the route bounds it, so a name the
        # server would refuse never costs a round-trip.
        name = name.strip()
        if len(name) > _CALIBRATE_NAME_MAX_CHARS:
            return json.dumps({
                "error": (
                    f"name must be at most {_CALIBRATE_NAME_MAX_CHARS} "
                    f"characters; got {len(name)}."
                ),
            })

        try:
            run_id = str(uuid.UUID(str(test_run_id).strip()))
        except (ValueError, AttributeError, TypeError):
            return json.dumps({
                "error": (
                    "test_run_id must be a test run's UUID, not a name. "
                    "Call list_test_runs to find the run and use its id."
                ),
            })

        if check_type == "model":
            check_config: dict[str, Any] = {
                "prompt_template": prompt_template,
                "type": output_type,
                "audio": is_audio,
            }
        else:
            # No inferred "type": the response echoes check_config verbatim,
            # and create_or_update_check sends none for code checks either.
            check_config = {"code_contents": code_contents}

        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        try:
            result = okareo_api_request(
                okareo,
                "post",
                f"/v0/test_runs/{run_id}/calibrate_check",
                json={
                    "check_config": check_config,
                    "check_type": "audio" if is_audio else check_type,
                    "name": name,
                    "inspect_only": inspect_only,
                },
                timeout=_CALIBRATE_TIMEOUT_SECONDS,
            )
        except httpx.HTTPStatusError as e:
            return _calibration_refusal(e)
        except Exception as e:
            return format_tool_error(e)

        payload: dict[str, Any] = (
            dict(result) if isinstance(result, dict) else {"response": result}
        )
        payload["next_step"] = (
            _CALIBRATE_INSPECT_NEXT_STEP if inspect_only else _CALIBRATE_NEXT_STEP
        )
        return json.dumps(payload, default=str)

    return None
