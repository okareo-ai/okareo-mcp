"""Centralized error handling for the Okareo MCP server.

Classifies exceptions into user-friendly categories and formats
consistent, sanitized error responses for MCP tool calls.
"""

import json
import os
import re

from src.key_registry import sanitize_error


# Bearer-token shapes we redact wherever they appear in error text. The
# substring "Authorization:" is dropped verbatim; tokens captured by the
# regex are replaced with "[REDACTED]". This is intentionally permissive —
# false-positive redactions are safer than leaking a credential to an LLM
# transcript. (US3 / FR-007 / SC-005.)
_BEARER_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9._\-+/=_]{20,}")
_AUTHZ_HEADER_PATTERN = re.compile(r"Authorization:\s*\S+", re.IGNORECASE)


# Where new Projects come from. The MCP is read-only over Projects (FR-025),
# so any surface that could read as "ask the server to make one" must say
# where creation actually happens (FR-026).
PROJECT_CREATION_NOTE = "New projects are created in the Okareo web application."


class ProjectError(Exception):
    """Base for project-resolution failures carrying a machine-readable code.

    Callers must be able to tell these apart from an artifact-not-found
    (FR-030): "exists but belongs to another project" and "you have not
    chosen a project" are different problems with different remedies.
    """

    code = "project_error"
    suggestion = "Specify a project and try again."

    def __init__(
        self,
        message: str,
        projects: list[dict[str, str]] | None = None,
        **data: object,
    ) -> None:
        super().__init__(message)
        self.projects = projects or []
        self.data = data


class ProjectNotSelected(ProjectError):
    """No project resolved and the organization has more than one (FR-020)."""

    code = "project_not_selected"
    suggestion = (
        "Ask the user which project they want, then pass it as `project` on "
        "the call. Record their choice and reuse it in later conversations."
    )


class ProjectNotFound(ProjectError):
    """A named project does not exist or is not accessible (FR-006)."""

    code = "project_not_found"
    suggestion = (
        "Check the name or id against the available projects. The previously "
        "active project is unchanged."
    )


class ProjectMisconfigured(ProjectError):
    """A connection pin names a project that cannot be resolved (US4 sc. 3).

    Split from ProjectNotFound because the remedy differs: a conversational
    error is fixed by answering, a pin error only by editing the connection
    configuration.
    """

    code = "project_misconfigured"
    suggestion = (
        "Fix the project pinned in your MCP connection configuration — this "
        "cannot be corrected from the conversation."
    )


# NOTE (FR-030a): a generic not-found must NOT be dressed up as a project
# problem. An earlier revision appended "not found in project X — it may
# belong to another project" to every 404 whenever a project was resolved,
# which is nearly every project-scoped call. It asserted a cause it had never
# established, and misdiagnosed a same-project failure by sending the user to
# look for a project that was not the problem. Only ArtifactNotInProject below
# may make that claim: it is raised by the name resolver, which has just
# listed the acting project and therefore knows.


class ArtifactNotInProject(ProjectError):
    """A named artifact does not exist in the acting project (FR-001a).

    Distinct from ProjectNotFound, which is about the *project* not existing.
    This one is raised only by the name resolver, which has just listed the
    project and therefore knows — the generic error path must never guess at
    this (FR-030a).
    """

    code = "artifact_not_in_project"
    suggestion = (
        "Use one of the artifacts available in this project, or switch to the "
        "project that contains the one you want."
    )


def _format_project_error(exc: ProjectError) -> str:
    """Render a ProjectError through the standard envelope, plus its code."""
    payload: dict[str, object] = {
        "category": "validation",
        "code": exc.code,
        "message": str(exc),
        "suggestion": exc.suggestion,
    }
    if exc.projects:
        payload["projects"] = exc.projects
    payload.update(exc.data)
    if exc.code == "project_not_selected":
        payload["note"] = PROJECT_CREATION_NOTE
    return json.dumps({"error": payload})


def _redact_credentials(text: str) -> str:
    """Strip bearer tokens and Authorization-header values from ``text``.

    Operates on the rendered message only — never on the original exception
    object, so call sites that re-raise still see the unredacted form for
    operator-side diagnosis (server logs).

    Order matters: redact bearer tokens FIRST (so the credential token is
    gone), then collapse the "Authorization:" header to a single redacted
    value (in case the upstream string had "Authorization: <opaque>" with
    no "Bearer" prefix, e.g., a raw API key).
    """
    if not text:
        return text
    text = _BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    text = _AUTHZ_HEADER_PATTERN.sub("Authorization: [REDACTED]", text)
    return text


def classify_error(exc: Exception) -> str:
    """Map an exception to an error category.

    Categories:
        connectivity  — network, DNS, SSL, timeout errors
        authentication — invalid API key, 401/403 responses
        validation     — invalid inputs, missing fields
        server_error   — API 500s, unexpected SDK errors, unknown errors

    Args:
        exc: The caught exception.

    Returns:
        One of: "connectivity", "authentication", "validation", "server_error".
    """
    exc_type = type(exc).__name__
    exc_module = type(exc).__module__ or ""

    # Connectivity: httpx and stdlib network/timeout errors
    if exc_type in (
        "ConnectError",
        "ConnectTimeout",
        "TimeoutException",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ProxyError",
        "NetworkError",
    ) or (exc_module.startswith("httpx") and "timeout" in exc_type.lower()):
        return "connectivity"

    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        if isinstance(exc, OSError) and not isinstance(
            exc, (ConnectionError, TimeoutError)
        ):
            # Only OS errors related to networking
            err = str(exc).lower()
            if any(
                w in err
                for w in ("refused", "reset", "unreachable", "network", "dns")
            ):
                return "connectivity"
            return "server_error"
        return "connectivity"

    # Authentication: TypeError from Okareo SDK (invalid key) or HTTP 401/403
    if isinstance(exc, TypeError):
        return "authentication"

    if exc_type == "HTTPStatusError":
        status = getattr(getattr(exc, "response", None), "status_code", 0)
        if status in (401, 403):
            return "authentication"
        if status in (400, 422):
            return "validation"
        return "server_error"

    if exc_type == "UnexpectedStatus":
        status = getattr(exc, "status_code", 0)
        if status in (401, 403):
            return "authentication"
        if status in (400, 422):
            return "validation"
        return "server_error"

    # Validation: ValueError from input checking
    if isinstance(exc, ValueError):
        return "validation"

    return "server_error"


_SUGGESTIONS = {
    "connectivity": "Check your network connection and try again.",
    "authentication": (
        "Verify your OKAREO_API_KEY at app.okareo.com and update "
        "your MCP server configuration."
    ),
    "validation": "Check the input parameters and try again.",
    "server_error": "The Okareo API encountered an error. Please try again later.",
}


_DETAIL_MAX_LEN = 600


def _extract_http_detail(exc: Exception) -> str | None:
    """Pull the backend error reason out of an HTTP error response body.

    FastAPI returns ``{"detail": "..."}`` for raised HTTPExceptions and
    ``{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`` for request
    validation failures. Summarize either into a short string. Returns ``None``
    when no usable detail is present. Never raises.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        body = response.json()
    except Exception:
        text = getattr(response, "text", None)
        return text.strip()[:_DETAIL_MAX_LEN] if text else None

    detail = body.get("detail") if isinstance(body, dict) else None
    if detail is None:
        return None
    if isinstance(detail, str):
        return detail[:_DETAIL_MAX_LEN]
    if isinstance(detail, list):
        parts: list[str] = []
        for item in detail:
            if isinstance(item, dict):
                loc = item.get("loc") or []
                field = ".".join(str(p) for p in loc[1:]) or ".".join(
                    str(p) for p in loc
                )
                msg = item.get("msg") or item.get("type") or "invalid"
                parts.append(f"{field}: {msg}" if field else str(msg))
            else:
                parts.append(str(item))
        return "; ".join(parts)[:_DETAIL_MAX_LEN] if parts else None
    return str(detail)[:_DETAIL_MAX_LEN]


def _http_status(exc: Exception) -> int | None:
    """Return the HTTP status from an SDK or httpx error, if any."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status


def _build_message(exc: Exception, category: str) -> str:
    """Build a user-readable error message from the exception."""
    exc_type = type(exc).__name__

    if category == "connectivity":
        if "timeout" in exc_type.lower() or isinstance(exc, TimeoutError):
            return "Request to the Okareo API timed out."
        return "Cannot connect to the Okareo API."

    if category == "authentication":
        if isinstance(exc, TypeError):
            return "Invalid API key format."
        return "Authentication failed."

    status = _http_status(exc)
    detail = _extract_http_detail(exc)

    if category == "validation":
        # ValueError from local input checks carries its own message; HTTP
        # 400/422 from the backend carries the reason in the response body.
        if status is not None:
            base = f"The Okareo API rejected the request (HTTP {status})."
            return f"{base} {detail}" if detail else base
        return str(exc)

    # server_error
    if status:
        base = f"The Okareo API returned an error (HTTP {status})."
        return f"{base} {detail}" if detail else base

    return f"An unexpected error occurred: {exc_type}"


def format_tool_error(
    exc: Exception, key_registry: dict[str, str] | None = None
) -> str:
    """Format an exception as a consistent, sanitized JSON error response.

    Args:
        exc: The caught exception.
        key_registry: Provider key registry for sanitization.
            If None, only OKAREO_API_KEY is sanitized.

    Returns:
        A JSON string: {"error": {"category": ..., "message": ..., "suggestion": ...}}
    """
    if isinstance(exc, ProjectError):
        return _format_project_error(exc)

    if key_registry is None:
        key_registry = {}

    category = classify_error(exc)
    message = _build_message(exc, category)
    suggestion = _SUGGESTIONS[category]

    # Capability-availability hint (FR-027, SC-008): a 404/501 from a newer
    # endpoint usually means the capability is not enabled for this account or
    # project, not a genuine "not found". Steer the caller accordingly.
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status in (404, 501):
        suggestion = (
            "This capability may not be enabled for your Okareo account or "
            "project. Verify at app.okareo.com or contact Okareo support."
        )


    # Sanitize: strip provider keys
    message = sanitize_error(message, key_registry)
    suggestion = sanitize_error(suggestion, key_registry)

    # Also strip the OKAREO_API_KEY value itself
    okareo_key = os.environ.get("OKAREO_API_KEY", "").strip()
    if okareo_key and okareo_key in message:
        message = message.replace(okareo_key, "[REDACTED]")

    # Strip generic bearer tokens / Authorization header values (FR-007,
    # T029). Tools can pass network errors verbatim from httpx, which may
    # include "Authorization: Bearer ..." in the rendered exception string.
    message = _redact_credentials(message)
    suggestion = _redact_credentials(suggestion)

    return json.dumps(
        {
            "error": {
                "category": category,
                "message": message,
                "suggestion": suggestion,
            }
        }
    )
