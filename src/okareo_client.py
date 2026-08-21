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

import functools
import hashlib
import inspect
import json
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Literal

from okareo import Okareo
from okareo_api_client.models.project_response import ProjectResponse

from src.error_handling import (
    ArtifactNotInProject,
    ProjectMisconfigured,
    ProjectNotFound,
    ProjectNotSelected,
)

# The description attached to every project-scoped tool's `project`
# parameter (FR-016a). It must reach the *parameter schema*, not just the tool
# docstring — FastMCP builds its input schema from the signature and does not
# parse docstring `Args:` blocks, so a docstring-only mention is dropped
# exactly where the co-pilot reads parameters (research R10).
PROJECT_PARAM_DESC = (
    "The Okareo project this operation acts on — a project name or a project "
    "id. This is a user-level preference, not a technical detail: track the "
    "project the user chose and pass it on every project-scoped call, "
    "including in later conversations. This server is stateless and does not "
    "remember it between calls. If you do not know which project the user "
    "wants, call list_projects and ask — never guess."
)

# How a project was determined, reported on every project-scoped response
# (FR-017). Three values, not FR-017's four: a conversational selection and a
# per-operation override both arrive as the same `project` argument, so the
# server reports "explicit" for both rather than claiming a distinction it
# cannot observe (FR-018).
ProjectBasis = Literal["explicit", "pin", "default"]


@dataclass(frozen=True)
class ResolvedProject:
    """The project an operation acts on, plus why."""

    id: str
    name: str
    basis: ProjectBasis

    def as_dict(self) -> dict[str, str]:
        """The ``project`` block stamped onto project-scoped responses."""
        return {"id": self.id, "name": self.name, "basis": self.basis}


# Process-scoped cache: (scope, base_url) → (fetched_at, projects).
# scope is SessionCredential.org_id in HTTP mode, or a short hash of the API
# key in stdio — never the raw credential, and never anything project-derived
# (FR-036). Keyed on the caller, not id(okareo), because HTTP mode constructs
# a fresh client per request and CPython can reuse freed object ids across
# organizations (see FR-018 / research R4).
_project_cache: dict[tuple[str, str], tuple[float, list[ProjectResponse]]] = {}
_PROJECT_CACHE_BOUND = 512

# The TTL is the primary freshness mechanism for out-of-process changes
# (projects created in the Okareo web application). Since 037 superseded
# FR-025, one in-process event creates a Project — clone_project — and it
# MUST call invalidate_projects_cache() afterward, or every project-scoped
# tool serves a list without the new Project until the TTL expires
# (research R4; 037 CR-1).
_PROJECT_CACHE_TTL_SECONDS = 60.0


def _reset_for_tests() -> None:
    """Clear the project cache. Production code must not call this."""
    _project_cache.clear()


def invalidate_projects_cache(okareo: Okareo) -> None:
    """Drop the caller's cached project list after an in-process change.

    The one production caller is ``clone_project`` (037), immediately after
    it creates the destination Project: the clone primes the cache while
    resolving its source, so without this the new Project is invisible to
    every other tool — including the clone report's own follow-on steps
    (register a Target there, select_project) — for up to the TTL.
    """
    base_url = os.environ.get("OKAREO_BASE_URL", "https://api.okareo.com/")
    try:
        _project_cache.pop((_project_cache_scope(okareo), base_url), None)
    except Exception:
        # Never let cache bookkeeping break the operation that did the work;
        # over-clearing is always safe.
        _project_cache.clear()


def _project_cache_scope(okareo: Okareo) -> str:
    """Derive a stable, non-secret cache scope for the current caller."""
    from src.auth.context import get_session_credential_optional

    cred = get_session_credential_optional()
    if cred is not None and cred.org_id:
        return cred.org_id
    return hashlib.sha256(okareo.api_key.encode("utf-8")).hexdigest()[:16]


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
        # The organization a call operates against is whatever the presented
        # credential is scoped to (feature 030): tenant selection happens at
        # sign-in, so the JWT's own `tenantId` claim is authoritative. There is
        # no per-session override to consult.
        return create_okareo_client(credential.api_key, base_url)

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


def get_projects_cached(okareo: Okareo) -> list[ProjectResponse]:
    """Return the caller's projects, cached per organization for 60 seconds.

    Resolution needs the whole list, not just one id: to match a name, to
    decide whether the organization has more than one project (FR-003), and to
    populate the project list inside a "not selected" error (FR-020).

    Args:
        okareo: An authenticated Okareo client.

    Returns:
        Every project accessible to the caller, in backend order. Archive
        state is ignored — an archived project stays selectable (FR-024).
    """
    base_url = os.environ.get("OKAREO_BASE_URL", "https://api.okareo.com/")
    cache_key = (_project_cache_scope(okareo), base_url)

    entry = _project_cache.get(cache_key)
    if entry is not None and (time.monotonic() - entry[0]) < _PROJECT_CACHE_TTL_SECONDS:
        return entry[1]

    projects = list(okareo.get_projects())
    if len(_project_cache) >= _PROJECT_CACHE_BOUND:
        _project_cache.clear()
    _project_cache[cache_key] = (time.monotonic(), projects)
    return projects


def _project_choices(projects: list[ProjectResponse]) -> list[dict[str, str]]:
    """Render projects as the ``{id, name}`` payload carried by errors."""
    return [{"id": str(p.id), "name": str(p.name)} for p in projects]


def _read_connection_pin() -> str | None:
    """Return the project pinned to this connection, if any.

    The pin must travel *per connection*, which differs by transport:

    - **stdio**: the ``OKAREO_PROJECT`` environment variable — one process
      serves one user, so the process env is per connection.
    - **streamable-http**: the ``project`` query parameter on the MCP endpoint
      URL, else the ``X-Okareo-Project`` header. ``OKAREO_PROJECT`` is
      deliberately IGNORED here: one hosted process serves every tenant, so an
      env var would pin all users of the deployment to one project
      (research R3).
    """
    if os.environ.get("TRANSPORT", "stdio") != "streamable-http":
        value = os.environ.get("OKAREO_PROJECT", "").strip()
        return value or None

    try:
        from mcp.server.lowlevel.server import request_ctx

        ctx = request_ctx.get()
        request = getattr(getattr(ctx, "request_context", None), "request", None)
        if request is None:
            request = getattr(ctx, "request", None)
        if request is not None:
            pinned = request.query_params.get("project")
            if pinned and pinned.strip():
                return pinned.strip()
            header = request.headers.get("X-Okareo-Project")
            if header and header.strip():
                return header.strip()
    except LookupError:
        # Outside a request scope — no pin is readable.
        pass
    except Exception:
        # Defensive — never let pin lookup break the request.
        pass
    return None


def _lookup(
    value: str, projects: list[ProjectResponse]
) -> ProjectResponse | None:
    """Match a name or id against ``projects``, or return None.

    Delegates the matching rules to the SDK (``_resolve_project``): a
    UUID-shaped value matches an id only and never falls back to a name, so a
    wrong id fails rather than quietly matching something else; anything else
    matches a name case-insensitively, ignoring surrounding whitespace
    (FR-004).
    """
    try:
        resolved_id = Okareo._resolve_project(value, projects)
    except ValueError:
        return None
    for candidate in projects:
        if str(candidate.id) == str(resolved_id):
            return candidate
    return None


def resolve_project(
    okareo: Okareo, project: str | None = None
) -> ResolvedProject:
    """Resolve which project an operation acts on (FR-002).

    Precedence, highest first:

    1. ``project`` named on this individual operation.
    2. The project pinned in the connection configuration.
    3. The organization's only project, when it has exactly one.
    4. Otherwise: raise, rather than guess.

    A failed lookup at steps 1-2 never falls through to a lower level
    (FR-006, FR-019): a named project that does not exist is an error, not an
    invitation to use a different one. Step 4 never falls back to Global —
    silently relocating a user's work is the failure this feature exists to
    prevent (FR-003, SC-005).

    Also assigns ``okareo.project_id`` as defense in depth, so any SDK call
    this server does not explicitly parameterize inherits the resolved project
    rather than the backend default (research R9).

    Args:
        okareo: An authenticated Okareo client.
        project: Optional project name or id for this operation only.

    Returns:
        The resolved project and the basis on which it was chosen.

    Raises:
        ProjectNotFound: ``project`` names nothing accessible.
        ProjectMisconfigured: the connection pin names nothing accessible.
        ProjectNotSelected: nothing resolved and the org has >1 project.
    """
    projects = get_projects_cached(okareo)

    if project is not None and str(project).strip():
        match = _lookup(str(project), projects)
        if match is None:
            raise ProjectNotFound(
                f"No project named {str(project).strip()!r} is available in "
                "your Okareo organization. Your active project is unchanged.",
                projects=_project_choices(projects),
            )
        return _bind(okareo, match, "explicit")

    pin = _read_connection_pin()
    if pin:
        match = _lookup(pin, projects)
        if match is None:
            raise ProjectMisconfigured(
                f"This connection is pinned to project {pin!r}, which does not "
                "exist in your Okareo organization. Fix the pin in your MCP "
                "connection configuration "
                "(the `project` URL parameter for the hosted server, or the "
                "OKAREO_PROJECT environment variable for a local install).",
                projects=_project_choices(projects),
                pin=pin,
            )
        return _bind(okareo, match, "pin")

    if len(projects) == 1:
        return _bind(okareo, projects[0], "default")

    if not projects:
        raise ProjectNotFound(
            "No projects are available in your Okareo organization. "
            "Verify your account setup at app.okareo.com.",
            projects=[],
        )

    raise ProjectNotSelected(
        "No project has been selected, and your organization has more than "
        "one. Ask the user which project they want, then pass it as `project`.",
        projects=_project_choices(projects),
    )


def _bind(
    okareo: Okareo, project: ProjectResponse, basis: ProjectBasis
) -> ResolvedProject:
    """Attach the resolved project to the client and describe it."""
    resolved = ResolvedProject(
        id=str(project.id), name=str(project.name), basis=basis
    )
    # Defense in depth (research R9): direct assignment rather than
    # set_project(), which would issue a redundant get_projects() call.
    try:
        okareo.project_id = resolved.id
    except Exception:
        pass
    _active_project.set(resolved)
    return resolved


# Set by resolve_project() and read by @project_scoped, both within a single
# tool invocation. This is call-local plumbing, NOT server-held state: the
# decorator clears it before every call and resets it after, so nothing
# survives the request (FR-009).
_active_project: ContextVar[ResolvedProject | None] = ContextVar(
    "okareo_active_project", default=None
)


def resolve_artifact_by_name(
    okareo: Okareo,
    name: str,
    project_id: str,
    kind: str = "artifact",
) -> Any:
    """Resolve an artifact name to its record **within the acting project**.

    Several Okareo lookups take a name and no project, so the backend resolves
    the name in its own default scope — and an artifact that lives only in a
    non-Global project comes back 404 even when the caller is in that project
    (research R13). The listing endpoint *does* accept a project, so the name
    is matched there and the caller addresses the artifact by id thereafter.

    This is server-side work by requirement (FR-001b): the operation already
    knows its project, so making the caller fetch an id first would spend a
    round trip on something already in hand.

    Args:
        okareo: An authenticated Okareo client.
        name: The artifact name the user supplied.
        project_id: The acting project, from :func:`resolve_project`.
        kind: Wording for the error message ("target", "model", ...).

    Returns:
        The matching ``ModelUnderTestResponse`` from the project-filtered
        listing. It already carries id, name, project_id, models, version,
        tags and app_link, so no second call is needed.

    Raises:
        ArtifactNotInProject: No artifact of that name in the acting project.
            Never raised on the strength of another project's contents — this
            function does not look outside the acting project (FR-030).
    """
    from okareo_api_client.api.default import (
        get_all_models_under_test_v0_models_under_test_get,
    )

    muts = get_all_models_under_test_v0_models_under_test_get.sync(
        client=okareo.client,
        project_id=project_id,
        api_key=okareo.api_key,
    )
    if not muts or isinstance(muts, Exception):
        muts = []

    wanted = str(name).strip().casefold()
    available: list[str] = []
    for mut in muts:
        mut_name = mut.get("name") if isinstance(mut, dict) else getattr(mut, "name", None)
        if mut_name is None:
            continue
        available.append(str(mut_name))
        if str(mut_name).strip().casefold() == wanted:
            return mut

    resolved = _active_project.get()
    project_label = resolved.name if resolved else project_id
    raise ArtifactNotInProject(
        f"No {kind} named {str(name).strip()!r} in project {project_label!r}.",
        project=(
            {"id": resolved.id, "name": resolved.name}
            if resolved
            else {"id": project_id, "name": project_id}
        ),
        available=sorted(available),
    )


@contextmanager
def project_resolution_scope():
    """Confine ``_active_project`` to the enclosing block.

    Anything that calls :func:`resolve_project` outside a ``@project_scoped``
    tool must use this, or the resolved project leaks into whatever runs next
    in the same context — which would let an unrelated error report a project
    it never touched (FR-018).
    """
    token = _active_project.set(None)
    try:
        yield
    finally:
        _active_project.reset(token)


def project_scoped(fn: Callable[..., str]) -> Callable[..., str]:
    """Stamp the resolved project onto a tool's JSON response (FR-016/FR-017).

    Applied to every project-scoped tool so the guarantee is structural rather
    than per-return-statement: these tools have 150+ success returns between
    them, and a hand-stamped block would silently go missing the next time
    someone adds an early return.

    Only stamps what ``resolve_project`` actually resolved, so the reported
    project can never disagree with the one acted on (FR-018). Error envelopes
    are left alone — they already carry their own machine-readable code, and
    an operation that failed to resolve did not act on a project.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        with project_resolution_scope():
            result = fn(*args, **kwargs)
            return _stamp_project(result, _active_project.get())

    return wrapper


def organization_scoped(note: str) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Mark a tool's responses as organization-shared (FR-028, FR-035).

    Checks and drivers belong to the organization, not to a project. Saying so
    on every response is what stops a user believing they just created
    something private to the project they happen to be working in.

    Mutually exclusive with :func:`project_scoped`: a response carries
    ``project`` or ``scope``, never both — an operation is either
    project-scoped or it is not (FR-018).
    """

    def decorator(fn: Callable[..., str]) -> Callable[..., str]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            return _stamp_scope(fn(*args, **kwargs), note)

        return wrapper

    return decorator


def _stamp_scope(result: str, note: str) -> str:
    """Add the organization-shared marker to a JSON object response."""
    if not isinstance(result, str):
        return result
    try:
        payload = json.loads(result)
    except (ValueError, TypeError):
        return result
    if not isinstance(payload, dict) or "error" in payload:
        return result
    payload["scope"] = "organization"
    payload["note"] = note
    return json.dumps(payload)


def _stamp_project(result: str, resolved: ResolvedProject | None) -> str:
    """Add the ``project`` block to a JSON object response, if one resolved."""
    if resolved is None or not isinstance(result, str):
        return result
    try:
        payload = json.loads(result)
    except (ValueError, TypeError):
        return result
    if not isinstance(payload, dict) or "error" in payload:
        return result
    payload["project"] = resolved.as_dict()
    return json.dumps(payload)



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
