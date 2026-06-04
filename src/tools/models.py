"""Generation model management tools for the Okareo MCP server.

Provides six MCP tools for the generation model (MUT) lifecycle:

- list_available_llms: Browse available LLMs from the Okareo registry
- register_generation_model: Register a generation model for testing by selecting an LLM
- list_generation_models: Browse all registered generation models in the project
- get_generation_model: Read detailed information about a registered generation model
- update_generation_model: Change the LLM a registered generation model points to
- delete_generation_model: Remove a registered generation model and its related test data
"""

import json

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from okareo_api_client.errors import UnexpectedStatus

from src.error_handling import format_tool_error
from src.okareo_client import get_okareo_client, resolve_project_id


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


def _get_available_models(okareo):
    """Fetch available target models from the Okareo registry."""
    from okareo_api_client.api.default import (
        get_available_target_models_v0_available_target_models_get,
    )

    result = get_available_target_models_v0_available_target_models_get.sync(
        client=okareo.client,
        api_key=okareo.api_key,
    )
    if result and hasattr(result, "available_models"):
        return result.available_models or []
    return []


def register_tools(mcp: FastMCP) -> None:
    """Register all generation model management tools with the FastMCP server."""

    @mcp.tool(
        title="List Available LLMs",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    def list_available_llms() -> str:
        """Browse available LLMs from the Okareo registry.

        Returns all LLMs that can be used when registering a generation model
        for testing. Each entry has a name, display name, and provider. Use a
        model_name from this list when calling register_generation_model.
        """
        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        try:
            models = _get_available_models(okareo)
        except Exception as e:
            return format_tool_error(e)

        if not models:
            return json.dumps({
                "models": [],
                "count": 0,
                "message": "No LLMs available in the registry.",
            })

        result = []
        for m in models:
            result.append({
                "model_name": _get_attr(m, "model_name", ""),
                "model_display_name": _get_attr(m, "model_display_name", ""),
                "provider_display_name": _get_attr(m, "provider_display_name", ""),
            })

        return json.dumps({"models": result, "count": len(result)})

    @mcp.tool(
        title="Register Generation Model",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    def register_generation_model(name: str, model_name: str) -> str:
        """Register a generation model for testing by selecting an LLM from the registry.

        Creates a generation model (Model Under Test) that points to a specific LLM
        (e.g., 'azure/gpt-4o-mini'). Use list_available_llms to see available
        LLMs. The registered generation model can then be used with run_test.

        Args:
            name: A human-readable name for this generation model (e.g., 'my-chatbot').
            model_name: The LLM from the registry (from list_available_llms).
        """
        from okareo.model_under_test import GenerationModel

        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        # Validate target model exists in registry
        try:
            available = _get_available_models(okareo)
            available_names = {_get_attr(m, "model_name", "") for m in available}
        except Exception as e:
            return format_tool_error(e)

        if model_name not in available_names:
            return json.dumps({
                "error": f"Model '{model_name}' not found in available LLMs. "
                "Use list_available_llms to see available options.",
            })

        # Register the generation model
        try:
            mut = okareo.register_model(
                name=name,
                model=[GenerationModel(model_id=model_name)],
                project_id=project_id,
            )
        except Exception as e:
            return format_tool_error(e)

        return json.dumps({
            "id": _get_attr(mut, "mut_id", ""),
            "name": _get_attr(mut, "name", name),
            "model_name": model_name,
            "app_link": _get_attr(mut, "app_link", ""),
        }, default=str)

    @mcp.tool(
        title="List Generation Models",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def list_generation_models() -> str:
        """Browse all registered generation models in the project.

        Returns generation model names, IDs, target LLM configurations, and
        creation timestamps. Use this to see what generation models are
        available for testing.
        """
        from okareo_api_client.api.default import (
            get_all_models_under_test_v0_models_under_test_get,
        )

        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        try:
            muts = get_all_models_under_test_v0_models_under_test_get.sync(
                client=okareo.client,
                project_id=project_id,
                version="latest",
                api_key=okareo.api_key,
            )
        except UnexpectedStatus as e:
            if e.status_code == 200:
                muts = json.loads(e.content)
            else:
                return format_tool_error(e)
        except Exception as e:
            return format_tool_error(e)

        if not muts or isinstance(muts, Exception):
            return json.dumps({
                "models": [],
                "count": 0,
                "message": "No generation models registered in project.",
            })

        result = []
        for m in muts:
            if isinstance(m, dict):
                # Extract model_name from the models dict
                model_name = ""
                models_dict = m.get("models")
                if isinstance(models_dict, dict):
                    for model_type_data in models_dict.values():
                        if isinstance(model_type_data, dict):
                            model_name = model_type_data.get("model_id", "")
                            break
                result.append({
                    "id": m.get("id", ""),
                    "name": m.get("name", ""),
                    "model_name": model_name,
                    "time_created": str(m.get("time_created", "")),
                    "app_link": m.get("app_link", ""),
                })
            else:
                # Extract model_name from the models dict
                model_name = ""
                models_data = _get_attr(m, "models")
                if models_data:
                    models_dict = _serialize_value(models_data)
                    if isinstance(models_dict, dict):
                        for model_type_data in models_dict.values():
                            if isinstance(model_type_data, dict):
                                model_name = model_type_data.get("model_id", "")
                                break
                result.append({
                    "id": _get_attr(m, "id", ""),
                    "name": _get_attr(m, "name", ""),
                    "model_name": model_name,
                    "time_created": str(_get_attr(m, "time_created", "")),
                    "app_link": _get_attr(m, "app_link", ""),
                })

        return json.dumps({"models": result, "count": len(result)}, default=str)

    @mcp.tool(
        title="Get Generation Model",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def get_generation_model(name: str) -> str:
        """Read detailed information about a registered generation model.

        Returns the generation model's target LLM configuration, tags, creation
        time, and any warnings (e.g., if the target LLM has been deprecated).

        Args:
            name: Name of the registered generation model.
        """
        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        try:
            mut = okareo.get_model(name=name)
        except Exception:
            return json.dumps({
                "error": f"Generation model '{name}' not found. "
                "Use list_generation_models to see registered generation models.",
            })

        models_config = _serialize_value(_get_attr(mut, "models"))

        response = {
            "id": _get_attr(mut, "mut_id", ""),
            "name": _get_attr(mut, "name", name),
            "models": models_config,
            "tags": _get_attr(mut, "tags", []),
            "time_created": str(_get_attr(mut, "time_created", "")),
            "app_link": _get_attr(mut, "app_link", ""),
        }

        warning = _get_attr(mut, "warning")
        if warning:
            response["warning"] = warning

        return json.dumps(response, default=str)

    @mcp.tool(
        title="Update Generation Model",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    def update_generation_model(name: str, model_name: str) -> str:
        """Change the LLM that a registered generation model points to.

        Updates the generation model to use a different LLM from the registry.
        Use list_available_llms to see available LLMs.

        Args:
            name: Name of the registered generation model to update.
            model_name: The new LLM from the registry.
        """
        from okareo.model_under_test import GenerationModel

        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        # Verify the generation model exists
        try:
            okareo.get_model(name=name)
        except Exception:
            return json.dumps({
                "error": f"Generation model '{name}' not found. "
                "Use list_generation_models to see registered generation models.",
            })

        # Validate new target model exists
        try:
            available = _get_available_models(okareo)
            available_names = {_get_attr(m, "model_name", "") for m in available}
        except Exception as e:
            return format_tool_error(e)

        if model_name not in available_names:
            return json.dumps({
                "error": f"LLM '{model_name}' not found in available LLMs. "
                "Use list_available_llms to see available options.",
            })

        # Update the generation model
        try:
            mut = okareo.register_model(
                name=name,
                model=[GenerationModel(model_id=model_name)],
                project_id=project_id,
                update=True,
            )
        except Exception as e:
            return format_tool_error(e)

        return json.dumps({
            "id": _get_attr(mut, "mut_id", ""),
            "name": _get_attr(mut, "name", name),
            "model_name": model_name,
            "updated": True,
            "app_link": _get_attr(mut, "app_link", ""),
        }, default=str)

    @mcp.tool(
        title="Delete Generation Model",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    def delete_generation_model(name: str) -> str:
        """Remove a registered generation model and all its related test data.

        Permanently deletes the generation model and cascades to associated
        test runs and test data points. This cannot be undone.

        Args:
            name: Name of the registered generation model to delete.
        """
        from okareo_api_client.api.default import (
            delete_model_under_test_v0_models_under_test_mut_id_delete,
        )

        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        # Look up generation model by name to get ID
        try:
            mut = okareo.get_model(name=name)
            mut_id = _get_attr(mut, "mut_id", "")
        except Exception:
            return json.dumps({
                "error": f"Generation model '{name}' not found. "
                "Use list_generation_models to see registered generation models.",
            })

        if not mut_id:
            return json.dumps({
                "error": f"Generation model '{name}' not found. "
                "Use list_generation_models to see registered generation models.",
            })

        # Delete
        try:
            delete_model_under_test_v0_models_under_test_mut_id_delete.sync(
                mut_id=mut_id,
                client=okareo.client,
                api_key=okareo.api_key,
            )
        except Exception as e:
            return format_tool_error(e)

        return json.dumps({
            "deleted": True,
            "name": name,
            "message": (
                f"Deleted generation model '{name}'. Associated test runs and "
                "data points were also deleted."
            ),
        })

    return None
