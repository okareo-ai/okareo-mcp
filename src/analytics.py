"""Anonymous product analytics for tool usage tracking.

Emits lightweight events to PostHog via the HTTP Capture API on each tool
invocation. Uses httpx (already a project dependency) -- no posthog-python
library. Analytics never block tool execution (fire-and-forget via
asyncio.create_task) and failures are silently suppressed.

Organization attribution: every hosted-session event carries ``org_id`` and a
``$groups`` association. Display names are resolved fire-and-forget from
Frontegg on first sight of an unknown org, cached per process, and published
once as ``$groupidentify``. Flat ``org_id``/``org_name`` properties satisfy
attribution without depending on PostHog's paid Group Analytics add-on.

Call context: tools contribute via ``src.analytics_context.annotate()``; the
wrapper passes the resulting dict as ``annotations`` to ``emit_tool_event``.
Privacy is enforced at that boundary — only allow-listed scalar keys enter.
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

# (distinct_id, email) pairs already sent to PostHog `$identify` this process.
# Identify is idempotent server-side, but re-sending it on every tool call
# would double our event volume for nothing.
_identified: set = set()

# org_id → display name, or None = lookup attempted and failed (negative sentinel).
_org_names: dict[str, str | None] = {}
# org_ids with a background name resolution currently in flight.
_org_name_inflight: set[str] = set()
# org_ids for which `$groupidentify` has already been sent this process.
_groupidentified: set[str] = set()

_ORG_NAME_CACHE_BOUND = 512

# Annotation keys that must never overwrite wrapper-owned properties.
_RESERVED_PROPERTIES = frozenset({
    "tool_name",
    "transport_type",
    "server_version",
    "tool_call_success",
    "org_id",
    "org_name",
    "$groups",
    "$process_person_profile",
})


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


def remember_org_names(tenants) -> None:
    """Populate the org-name cache from an already-fetched tenant list.

    Called by ``list_tenants`` so a user who lists tenants names their
    organization at zero extra Frontegg cost. Never raises.
    """
    try:
        for tenant in tenants:
            org_id = getattr(tenant, "id", None)
            name = getattr(tenant, "name", None)
            if org_id and name:
                _store_org_name(str(org_id), str(name))
    except Exception:
        pass


def _store_org_name(org_id: str, name: str | None) -> None:
    if len(_org_names) >= _ORG_NAME_CACHE_BOUND:
        _org_names.clear()
        _org_name_inflight.clear()
    _org_names[org_id] = name


def _schedule_org_name_resolution(org_id: str, jwt: str) -> None:
    """Fire-and-forget Frontegg lookup for an unknown organization name."""
    if org_id in _org_names or org_id in _org_name_inflight:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return

    _org_name_inflight.add(org_id)

    async def _resolve() -> None:
        try:
            # Function-local import keeps stdio airgapped-clean.
            from src.auth.frontegg_user_info import get_user_tenants

            tenants = await get_user_tenants(
                jwt=jwt,
                session_id=f"analytics-org:{org_id}",
                frontegg_domain=os.environ.get("FRONTEGG_DOMAIN", "").strip(),
            )
            matched = None
            for tenant in tenants:
                if tenant.id == org_id:
                    matched = tenant.name
                    break
            _store_org_name(org_id, matched)
        except Exception:
            _logger.debug(
                "Org name resolution failed for org_id=%s", org_id, exc_info=True
            )
            _store_org_name(org_id, None)
        finally:
            _org_name_inflight.discard(org_id)

    try:
        task = asyncio.create_task(_resolve())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except Exception:
        _org_name_inflight.discard(org_id)


def emit_tool_event(
    client: AnalyticsClient,
    tool_name: str,
    success: bool,
    annotations: dict | None = None,
) -> None:
    """Emit a tool call event to PostHog. Fire-and-forget via asyncio.create_task.

    Principal id selection (T030 / FR-007 / SC-005):
        - **HTTP mode, OAuth session with a user identity**: the JWT's
          ``sub`` (Frontegg user id). On the first event for a given
          (subject, email) pair this process also emits a PostHog
          ``$identify`` carrying the user's email, so events show up
          under the real person instead of a bare UUID. ``org_id`` is
          kept as an event property for per-organization breakdowns.
        - **HTTP mode, API-key session**: the session's ``org_id``
          (no user identity exists on this path).
        - Falls back to the process uuid4 if no credential is bound
          (e.g., the tool was called outside a request, which would be
          a bug elsewhere but we don't want analytics to mask it).
        - **stdio mode**: the per-process anonymous uuid4() on the
          AnalyticsClient (unchanged single-tenant behavior).

    Never logs, persists, or transmits the JWT, API key, or any derivable
    secret — only ``org_id``, ``sub``, and ``email`` (identity metadata,
    not credentials) are sent.

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
    email = None
    org_id = None
    cred = None
    # HTTP mode: prefer the per-request user (or org) as the principal.
    if client.transport_type == "streamable-http":
        try:
            from src.auth.context import get_session_credential_optional

            cred = get_session_credential_optional()
            if cred is not None:
                org_id = cred.org_id or None
                if cred.subject and cred.email:
                    distinct_id = cred.subject
                    email = cred.email
                elif cred.org_id:
                    distinct_id = cred.org_id
        except Exception:
            # Defensive — analytics MUST NEVER break tool execution.
            pass

    properties = {
        "tool_name": tool_name,
        "transport_type": client.transport_type,
        "server_version": client.server_version,
        "tool_call_success": success,
        # Person profiles only for identified users; anonymous events stay
        # cheap and profile-less.
        "$process_person_profile": bool(email),
    }
    if org_id:
        properties["org_id"] = org_id
        properties["$groups"] = {"organization": org_id}
        org_name = _org_names.get(org_id)
        if org_name:
            properties["org_name"] = org_name
        elif (
            org_id not in _org_names
            and client.transport_type == "streamable-http"
            and cred is not None
            and cred.kind == "oauth"
            and cred.api_key
        ):
            _schedule_org_name_resolution(org_id, cred.api_key)

    if annotations:
        for key, value in annotations.items():
            if key in _RESERVED_PROPERTIES:
                continue
            if value is None:
                continue
            properties[key] = value

    payload = {
        "api_key": client.api_key,
        "distinct_id": distinct_id,
        "event": "okareo_mcp_tool_call",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "properties": properties,
    }

    payloads: list = []

    org_name_for_group = properties.get("org_name")
    if (
        org_id
        and org_name_for_group
        and org_id not in _groupidentified
    ):
        _groupidentified.add(org_id)
        payloads.append({
            "api_key": client.api_key,
            "distinct_id": distinct_id,
            "event": "$groupidentify",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "properties": {
                "$group_type": "organization",
                "$group_key": org_id,
                "$group_set": {"name": org_name_for_group},
                "$process_person_profile": False,
            },
        })

    if email and (distinct_id, email) not in _identified:
        _identified.add((distinct_id, email))
        set_props = {"email": email}
        if org_id:
            set_props["org_id"] = org_id
        payloads.append({
            "api_key": client.api_key,
            "distinct_id": distinct_id,
            "event": "$identify",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "properties": {"$set": set_props},
        })

    payloads.append(payload)

    # Fire-and-forget needs a running event loop. In a sync context (e.g.
    # tests) there is none, so check first — otherwise the _send_event(...)
    # coroutine would be constructed, create_task would raise, and the
    # orphaned coroutine would emit a "never awaited" RuntimeWarning.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _logger.debug(
            "No running event loop; skipping analytics for tool=%s", tool_name
        )
        return

    try:
        if len(payloads) == 1:
            task = asyncio.create_task(_send_event(client.http_client, payloads[0]))
        else:
            # Sequence $groupidentify / $identify before the tool event so
            # group and person records exist by the time the first event lands.
            task = asyncio.create_task(
                _send_events(client.http_client, payloads)
            )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except Exception:
        _logger.exception(
            "Failed to schedule analytics send for tool=%s", tool_name
        )


async def _send_events(
    http_client: httpx.AsyncClient, payloads: list
) -> None:
    """POST several events in order. Failures never propagate."""
    for payload in payloads:
        await _send_event(http_client, payload)


def _reset_for_tests() -> None:
    """Test helper: forget which principals have been identified.

    Production code MUST NOT call this — the dedupe set is process-scoped
    by design.
    """
    _identified.clear()
    _org_names.clear()
    _org_name_inflight.clear()
    _groupidentified.clear()


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
            "https://e.okareo.com/capture/",
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
