"""Okareo SDK wrapper: client initialization and project resolution.

`get_okareo_client()` is the canonical accessor for tools. In stdio mode it
reads `OKAREO_API_KEY` from the process env (single-tenant). In HTTP mode
(`TRANSPORT=streamable-http`) it reads the per-request `SessionCredential`
set by the auth verifier and (when set by `switch_tenant`) the per-session
`ActiveTenantOverride` — making every tool call transparently scoped to the
caller's organization, with optional cross-tenant routing, and zero tool-side
edits.

The override (FR-024, 2026-05-18 pivot) works by substituting the Okareo
SDK's ``api_key`` with a **new, tenant-scoped Frontegg access token** that
``switch_tenant`` minted via Frontegg's ``/auth/v1/user/token/refresh``
endpoint. The Okareo backend reads ``tenantId`` from the JWT claims — no
backend changes required, no special headers, just a different JWT bound to
the right tenant. The MCP client itself still holds the original
default-tenant JWT and presents it on every request; the override JWT is
used only server-side to form downstream Okareo calls.
"""

import inspect
import os
import sys

from okareo import Okareo

# Session-level cache for project ID, keyed by okareo instance id
_project_id_cache: dict[int, str] = {}


def create_okareo_client(
    api_key: str,
    base_url: str | None = None,
) -> Okareo:
    """Create an Okareo client with the given api_key.

    The Okareo constructor validates the key by calling GET /v0/projects.

    Args:
        api_key: Okareo API key, OR a Frontegg-issued JWT (both are accepted
            by the Okareo backend; the JWT's ``tenantId`` claim is what
            scopes the call).
        base_url: Optional base URL for the Okareo API.

    Returns:
        An authenticated Okareo client instance.

    Raises:
        TypeError: If the api_key is invalid (API returns 401).
    """
    if base_url:
        return Okareo(api_key=api_key, base_path=base_url)
    return Okareo(api_key=api_key)


def get_okareo_client() -> Okareo:
    """Return an Okareo client appropriate for the current transport mode.

    Tools call this once per invocation; it does the right thing in both modes
    without per-tool branching:

    - **HTTP mode**: if a per-request `SessionCredential` is bound to the
      current context (set by `CombinedTokenVerifier`), build a fresh client
      keyed to that credential's API key — the caller's organization. If the
      session has an active-tenant override (set by `switch_tenant`), inject
      the `X-Okareo-Org-Override` header so the backend re-scopes the call.
    - **stdio mode**: no credential is bound; fall back to the
      `OKAREO_API_KEY` env var (existing single-tenant behavior). The override
      mechanism does not apply.

    Returns:
        An authenticated Okareo client.

    Raises:
        ValueError: stdio fallback path with no `OKAREO_API_KEY` set.
    """
    # Local imports keep the dependencies lazy (and avoid any chance of a
    # circular import at server startup).
    from src.auth.context import (
        get_session_credential_optional,
    )

    credential = get_session_credential_optional()
    base_url = os.environ.get("OKAREO_BASE_URL", "https://api.okareo.com/")
    if credential is not None:
        # Per-session tenant override (FR-024, 2026-05-18 pivot): if
        # switch_tenant has run, use the override's tenant-scoped JWT as
        # the SDK api_key — that JWT's `tenantId` claim is what the Okareo
        # backend reads to scope the call. If no override is set, fall back
        # to the credential's original JWT (the JWT default tenant).
        from src.auth import tenant_state

        session_id = _current_session_id()
        override = (
            tenant_state.get_override(session_id) if session_id else None
        )
        effective_key = override.access_token if override is not None else credential.api_key
        # Diagnostic for tenant-override troubleshooting. Logs only the
        # session_id and the active tenant_id; never any JWT or api_key.
        print(
            f"[tenant] session_id={session_id!r} "
            f"override_tenant={(override.tenant_id if override else None)!r}",
            file=sys.stderr, flush=True,
        )
        return create_okareo_client(effective_key, base_url)

    api_key = os.environ.get("OKAREO_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "OKAREO_API_KEY environment variable is not set. "
            "Set it to your Okareo API key to use the MCP server."
        )
    return create_okareo_client(api_key, base_url)


def _current_session_id() -> str | None:
    """Best-effort lookup of the current `Mcp-Session-Id` header value.

    Returns ``None`` in stdio mode or when the FastMCP request context isn't
    available (e.g., test contexts that bypass the transport layer). In HTTP
    mode within a normal request, returns the session id that FastMCP minted
    at `initialize` time and that the client echoes on every subsequent call.

    Note: in ``stateless_http=True`` mode the FastMCP server typically does
    not require a stable `Mcp-Session-Id` header, so this path frequently
    returns ``None`` and we fall back to the (user_sub, process) identifier
    below. That fallback is stable for the lifetime of a single server
    process — which is what the override map needs.
    """
    try:
        # The lowlevel server's request_ctx is the documented ContextVar
        # source-of-truth for the active request. `.request` is the Starlette
        # `Request` for the Streamable HTTP transport.
        from mcp.server.lowlevel.server import request_ctx  # type: ignore[attr-defined]

        ctx = request_ctx.get()  # LookupError if outside a request
        request = getattr(ctx, "request", None)
        if request is not None:
            headers = getattr(request, "headers", None)
            if headers is not None:
                # Starlette headers are case-insensitive. Try both casings
                # defensively in case some intermediate layer normalizes.
                sid = (
                    headers.get("mcp-session-id")
                    or headers.get("Mcp-Session-Id")
                )
                if sid:
                    return str(sid)
    except LookupError:
        # Outside a request scope — fall through to fallback.
        pass
    except Exception:
        # Defensive — never let session-id lookup break the request.
        pass

    # Fallback: best-effort identifier from the SessionCredential. This is
    # NOT the real MCP-Session-Id, but it provides per-(user, process)
    # uniqueness so a single-instance environment (local docker compose, one
    # Cloud Run instance) still gets a stable key for the override map.
    # Documented at FR-024.
    from src.auth.context import get_session_credential_optional

    cred = get_session_credential_optional()
    if cred is None or not cred.subject:
        return None
    return f"sub:{cred.subject}/proc:{os.getpid()}"


def resolve_project_id(okareo: Okareo) -> str:
    """Resolve the 'Global' project ID from the user's Okareo account.

    Caches the result per Okareo instance for the session lifetime.

    Args:
        okareo: An authenticated Okareo client.

    Returns:
        The project ID string for the 'Global' project.

    Raises:
        ValueError: If no project named 'Global' is found.
    """
    cache_key = id(okareo)
    if cache_key in _project_id_cache:
        return _project_id_cache[cache_key]

    projects = okareo.get_projects()
    for project in projects:
        if project.name == "Global":
            _project_id_cache[cache_key] = project.id
            return project.id

    raise ValueError(
        "No project named 'Global' found in your Okareo account. "
        "Verify your project setup at app.okareo.com."
    )


def find_test_runs(okareo: Okareo, payload):
    """Call find_test_runs API with SDK-version-compatible parameter name.

    Handles the parameter rename from ``json_body`` (okareo <= 0.0.121) to
    ``body`` (okareo >= 0.0.122) automatically via signature introspection.

    Args:
        okareo: An authenticated Okareo client.
        payload: A GeneralFindPayload instance.

    Returns:
        List of TestRunItem or raw dicts, or None on error.
    """
    from okareo_api_client.api.default import (
        find_test_run_v0_find_test_runs_post,
    )

    sig = inspect.signature(find_test_run_v0_find_test_runs_post.sync)
    body_key = "json_body" if "json_body" in sig.parameters else "body"
    return find_test_run_v0_find_test_runs_post.sync(
        client=okareo.client,
        api_key=okareo.api_key,
        **{body_key: payload},
    )


def okareo_api_request(
    okareo: Okareo,
    method: str,
    path: str,
    *,
    json: object | None = None,
    params: dict | None = None,
):
    """Issue an authenticated request to the Okareo API via the SDK's client.

    Used for endpoints the published okareo SDK does not yet wrap or expose a
    generated client module for. Reusing ``okareo.client``'s httpx client keeps
    base URL, timeout, and the ``api-key`` auth header — the scheme every
    generated module uses — centralized in one place rather than re-derived
    per tool. See `specs/022-sdk-132-upgrade` research R2.

    Args:
        okareo: An authenticated Okareo client.
        method: HTTP method — "get", "post", "patch", "delete", etc.
        path: API path beginning with "/v0/".
        json: Optional JSON request body.
        params: Optional query parameters.

    Returns:
        The parsed JSON response body, or ``None`` for an empty 2xx response.

    Raises:
        httpx.HTTPStatusError: on a non-2xx response.
    """
    httpx_client = okareo.client.get_httpx_client()
    response = httpx_client.request(
        method,
        path,
        json=json,
        params={k: v for k, v in (params or {}).items() if v is not None},
        headers={"api-key": okareo.api_key},
    )
    response.raise_for_status()
    if not response.content:
        return None
    return response.json()
