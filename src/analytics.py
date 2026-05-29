"""Anonymous product analytics for tool usage tracking.

Emits lightweight events to PostHog via the HTTP Capture API on each tool
invocation. Uses httpx (already a project dependency) -- no posthog-python
library. Analytics never block tool execution (fire-and-forget via
asyncio.create_task) and failures are silently suppressed.

Privacy: Only an explicit allow-list of properties is sent. Tool arguments,
responses, API keys, and error messages are never transmitted.
"""

import asyncio
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

# Logs to stderr by default (Python logging default), which is safe in stdio
# mode — stdout is reserved for the MCP protocol. Set OKAREO_ANALYTICS_DEBUG=1
# (or the root log level to DEBUG) to surface why events do/don't reach PostHog.
_logger = logging.getLogger("okareo.analytics")

# Strong references to in-flight fire-and-forget analytics sends. asyncio keeps
# only a weak reference to a bare ``create_task()`` result, so without this set
# a send can be garbage-collected mid-flight before it reaches PostHog. Each
# task removes itself via ``add_done_callback`` once it completes.
_background_tasks: set = set()


def is_truthy(value: Optional[str]) -> bool:
    """Parse a string environment variable as a boolean.

    Returns True for "true", "True", "TRUE", "1", "yes".
    Returns False for everything else (including empty string and None).
    """
    if value is None:
        return False
    return value.strip().lower() in ("true", "1", "yes")


def _get_server_version() -> str:
    """Retrieve the package version from metadata, with fallback."""
    try:
        from importlib.metadata import version

        return version("okareo-mcp")
    except Exception:
        return "unknown"


@dataclass
class AnalyticsClient:
    """Encapsulates analytics state for the lifetime of the server process."""

    http_client: Optional[httpx.AsyncClient]
    distinct_id: str
    transport_type: str
    server_version: str
    enabled: bool
    api_key: str = ""


def init_analytics() -> AnalyticsClient:
    """Initialize the analytics subsystem. Called once during server lifespan startup.

    Reads OKAREO_ANALYTICS_OPT_IN, DEV, and AIRGAP from environment.
    Analytics are disabled by default and require explicit opt-in.
    PostHog key and host are hard-coded.
    Generates a per-process uuid4() as distinct_id (no file I/O).

    Never raises. Returns a disabled client on any initialization failure.
    """
    try:
        opt_in = is_truthy(os.environ.get("OKAREO_ANALYTICS_OPT_IN"))
        posthog_api_key = os.environ.get("POSTHOG_API_KEY", "")
        ph_key_available = bool(posthog_api_key)
        dev_mode = is_truthy(os.environ.get("DEV"))
        airgap = is_truthy(os.environ.get("AIRGAP"))
        enabled = ph_key_available and opt_in and not dev_mode and not airgap

        transport_type = os.environ.get("TRANSPORT", "stdio")
        server_version = _get_server_version()
        distinct_id = str(uuid.uuid4())
        http_client = None
        if enabled:
            http_client = httpx.AsyncClient(timeout=5.0)

        return AnalyticsClient(
            http_client=http_client,
            distinct_id=distinct_id,
            transport_type=transport_type,
            server_version=server_version,
            enabled=enabled,
            api_key=posthog_api_key,
        )
    except Exception:
        _logger.exception("Analytics init failed; returning disabled client")
        return AnalyticsClient(
            http_client=None,
            distinct_id=str(uuid.uuid4()),
            transport_type="stdio",
            server_version="unknown",
            enabled=False,
        )


async def shutdown_analytics(client: Optional[AnalyticsClient]) -> None:
    """Close the HTTP client. Called during server lifespan teardown.

    Never raises. Logs to stderr on failure. Tolerates ``client is None``
    so it's safe to call defensively on partial initialization paths or
    re-entrant shutdown sequences.
    """
    if client is None:
        return
    try:
        if client.http_client is not None:
            await client.http_client.aclose()
    except Exception as e:
        print(f"Analytics shutdown error: {e}", file=sys.stderr)


def emit_tool_event(
    client: AnalyticsClient, tool_name: str, success: bool
) -> None:
    """Emit a tool call event to PostHog. Fire-and-forget via asyncio.create_task.

    Principal id selection (T030 / FR-007 / SC-005):
        - **HTTP mode**: the calling session's ``org_id`` (read from the
          per-request ``SessionCredential`` via the existing context helper).
          This keeps events grouped per Okareo organization rather than per
          server-process — which is the right granularity for a multi-tenant
          remote endpoint. Falls back to the process uuid4 if no credential
          is bound (e.g., the tool was called outside a request, which would
          be a bug elsewhere but we don't want analytics to mask it).
        - **stdio mode**: the per-process anonymous uuid4() on the
          AnalyticsClient (unchanged single-tenant behavior).

    Never logs, persists, or transmits the JWT, API key, or any derivable
    secret — only ``org_id`` (public-by-design) is sent.

    Never raises. Silently drops events on any error.
    """
    if not client.enabled or client.http_client is None:
        _logger.debug(
            "Skipping analytics for tool=%s (enabled=%s, http_client=%s)",
            tool_name,
            client.enabled,
            client.http_client is not None,
        )
        return

    distinct_id = client.distinct_id
    # HTTP mode: prefer the per-request org_id as the analytics principal.
    if client.transport_type == "streamable-http":
        try:
            from src.auth.context import get_session_credential_optional

            cred = get_session_credential_optional()
            if cred is not None and cred.org_id:
                distinct_id = cred.org_id
        except Exception:
            # Defensive — analytics MUST NEVER break tool execution.
            pass

    payload = {
        "api_key": client.api_key,
        "distinct_id": distinct_id,
        "event": "okareo_mcp_tool_call",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "properties": {
            "tool_name": tool_name,
            "transport_type": client.transport_type,
            "server_version": client.server_version,
            "tool_call_success": success,
            "$process_person_profile": False,
        },
    }

    try:
        task = asyncio.create_task(_send_event(client.http_client, payload))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except Exception:
        _logger.exception(
            "Failed to schedule analytics send for tool=%s", tool_name
        )


async def _send_event(
    http_client: httpx.AsyncClient, payload: dict
) -> None:
    """POST a single event to the PostHog Capture API.

    Failures never propagate (analytics must not break tool execution), but
    they ARE logged so a non-2xx response or transport error is diagnosable.
    PostHog returns 200 with ``{"status": 1}`` on accept; a 401 indicates a
    bad/missing project ``api_key``.
    """
    event = payload.get("event", "?")
    try:
        resp = await http_client.post(
            "https://us.i.posthog.com/capture/",
            json=payload,
        )
        if resp.status_code >= 300:
            _logger.warning(
                "PostHog capture rejected event=%s: HTTP %s body=%s",
                event,
                resp.status_code,
                resp.text[:500],
            )
        else:
            _logger.debug(
                "PostHog capture accepted event=%s: HTTP %s body=%s",
                event,
                resp.status_code,
                resp.text[:200],
            )
    except Exception:
        _logger.exception("PostHog capture send failed for event=%s", event)
