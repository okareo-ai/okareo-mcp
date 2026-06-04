"""Check management tools for the Okareo MCP server.

Provides four MCP tools for creating, generating, reading, and deleting checks:

- create_or_update_check: Create or update a quality check by name (upsert)
- generate_check: Generate a check from a natural language description
- get_check: Retrieve the full configuration of a check by name
- delete_check: Permanently delete a check by name
"""

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from src.error_handling import format_tool_error
from src.okareo_client import get_okareo_client


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

        Args:
            name: Unique name for the check.
            description: What the check evaluates.
            check_type: "model" (prompt-based) or "code" (Python class).
            output_type: "pass_fail", "score", or "analysis".
            tags: Optional list of string tags to organize the check. Tags are
                stored with the check and returned by get_check.
            prompt_template: Required when check_type="model". Prompt with placeholders:
                {generation}, {scenario_input}, {scenario_result}, {model_input},
                {model_output}, {message_history}, {tool_calls}, {tools},
                {model_output_metadata}, {simulation_message_history}.
            code_contents: Required when check_type="code". Python source code
                implementing class Check(CodeBasedCheck) with a @staticmethod
                evaluate() method returning CheckResponse. Available parameters
                (all optional): model_output, scenario_input, scenario_result,
                metadata, model_input.
            is_audio: Set to true for audio/voice evaluation. Only valid with
                check_type="model".
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
                # use low-level API directly
                code_output_type = "bool" if output_type == "pass_fail" else "int"
                config_dict = {
                    "code_contents": code_contents,
                    "type": code_output_type,
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
    def generate_check(
        name: str,
        description: str,
        output_type: str = "pass_fail",
        check_type: str = "model",
        requires_scenario_input: bool = False,
        requires_scenario_result: bool = False,
    ) -> str:
        """Generate a check from a natural language description. Uses AI to create the prompt template (model checks) or Python code (code checks), then saves the check.

        Args:
            name: Name for the generated check.
            description: Natural language description of what to evaluate
                (e.g., "check if the response is toxic").
            output_type: "pass_fail", "score", or "analysis". If the server does not
                support generating a particular output type, the error is returned
                gracefully.
            check_type: "model" or "code".
            requires_scenario_input: Whether the check needs scenario input.
            requires_scenario_result: Whether the check needs expected result.
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

    return None
