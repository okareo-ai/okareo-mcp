"""Provider API key registry for the Okareo MCP server.

Scans environment variables for third-party model provider keys
(e.g., OPENAI_API_KEY, ANTHROPIC_API_KEY) and provides them to
tools at execution time without exposing them to the LLM.
"""

import os

# Provider names accepted by the Okareo API for api_keys.
_SUPPORTED_PROVIDERS: set[str] = {
    "generation",
    "cohere",
    "openai",
    "openai_assistant",
    "pinecone",
    "qdrant",
    "custom",
    "custom_batch",
    "custom_target",
    "custom_target_async",
    "driver",
    "custom_endpoint",
    "voice",
}


def scan_provider_keys() -> dict[str, str]:
    """Scan environment variables for provider API keys.

    Reads all environment variables matching the pattern *_API_KEY,
    excluding OKAREO_API_KEY. Derives the provider name by stripping
    the _API_KEY suffix and lowercasing. Only keys whose derived
    provider name is in the set accepted by the Okareo API are included.

    Empty values are treated as missing and skipped.

    Returns:
        A dict mapping provider name (lowercase) to API key value.
        E.g., {"openai": "sk-...", "generation": "sk-..."}.
    """
    registry: dict[str, str] = {}
    suffix = "_API_KEY"

    for key, value in os.environ.items():
        if not key.endswith(suffix):
            continue
        if key == "OKAREO_API_KEY":
            continue
        value = value.strip()
        if not value:
            continue
        provider = key[: -len(suffix)].lower()
        if provider not in _SUPPORTED_PROVIDERS:
            continue
        registry[provider] = value

    return registry


def sanitize_error(message: str, key_registry: dict[str, str]) -> str:
    """Strip any provider API key values from an error message.

    Defense-in-depth: ensures that even if the Okareo backend or SDK
    includes a key value in an error response, it never reaches the LLM.

    Args:
        message: The error message string to sanitize.
        key_registry: The provider key registry (provider -> key value).

    Returns:
        The message with all key values replaced by [REDACTED].
    """
    for key_value in key_registry.values():
        if key_value and key_value in message:
            message = message.replace(key_value, "[REDACTED]")
    return message
