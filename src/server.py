"""FastMCP server entry point for the Okareo MCP server.

Supports two transport modes via the TRANSPORT environment variable:

- ``stdio`` (default) — single-tenant; reads ``OKAREO_API_KEY`` from env.
  Used for local copilot installs via ``uvx`` / editable ``pip install -e .``.
- ``streamable-http`` — remote multi-tenant hosted endpoint (feature 020).
  Per-request credential via the MCP authorization spec (OAuth) or an
  ``Authorization: Bearer <api-key>`` header fallback. No env API key required.

The previous ``sse`` mode and its self-hosted Docker image were removed in the
2026-05-15 clarification (see specs/020-remote-mcp/spec.md). There is no
customer-self-hosted Docker MCP anymore; the only Docker image is the
Okareo-hosted streamable-http one.

Error handling strategy: The server always starts, even if the Okareo API is
unreachable or the API key is invalid. Tools attempt lazy re-initialization
on each call, so the server auto-recovers when the API becomes available.
In streamable-http mode there is no startup API key — the auth verifier
rejects every request before it reaches a tool until the credential is valid.
"""

import logging
import os
import sys
import time
from contextlib import asynccontextmanager

# Auto-load .env from cwd if present, before anything else reads env vars.
# Harmless if no .env exists. Does NOT override variables already set in the
# shell, so explicit `export VAR=...` always wins. Applies to both stdio and
# streamable-http modes for dev ergonomics; production deploys use real env.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    # python-dotenv not installed in this env — skip silently. The server
    # still runs as long as the required env vars are exported by the shell.
    pass

from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import AnyHttpUrl

from src.analytics import emit_tool_event, init_analytics, shutdown_analytics
from src.error_handling import format_tool_error
from src.key_registry import scan_provider_keys
from src.okareo_client import create_okareo_client
from src.tools import (
    checks,
    docs,
    insights,
    models,
    scenarios,
    simulations,
    tenants,
    tests,
    voice,
)

_server_ready = False
_analytics_client = None
# Cached Okareo client; None when not yet initialized or init failed.
_okareo_client = None
_key_registry: dict[str, str] = {}

# Module logger for server-lifecycle and per-tool-call traces. Routed through
# `logging` (not raw `print`) so FASTMCP_LOG_LEVEL controls visibility — the
# Docker image pins it to WARNING; local runs inherit the FastMCP INFO default.
_logger = logging.getLogger(__name__)


_TRANSPORT = os.environ.get("TRANSPORT", "stdio")
_HTTP_MODE = _TRANSPORT == "streamable-http"


def _frontegg_issuer() -> str:
    """Resolve the expected JWT ``iss`` claim.

    By default, derived as ``https://${FRONTEGG_DOMAIN}`` — for tenants on
    a Frontegg-native subdomain that's exactly what's in the token.

    When Frontegg serves the tenant through a **custom domain** AND issues
    tokens with the custom domain as `iss` (a common configuration:
    `auth-dev.okareo.com` etc.), but JWKS is only proxied through the
    Frontegg-native subdomain, the two hosts need to differ. Setting
    ``FRONTEGG_ISSUER`` (full URL, e.g. ``https://auth-dev.okareo.com``)
    overrides the iss expectation while leaving the JWKS fetch URL
    bound to ``FRONTEGG_DOMAIN``.

    Returns an empty string when neither var is set (server will reject
    every request — safe failure mode).
    """
    override = os.environ.get("FRONTEGG_ISSUER", "").strip()
    if override:
        return override.rstrip("/")
    domain = os.environ.get("FRONTEGG_DOMAIN", "").strip()
    return f"https://{domain}" if domain else ""


def _required_scopes_from_env() -> list[str]:
    """Return the configured scope list (empty by default — opt-in)."""
    value = os.environ.get("MCP_REQUIRED_SCOPE", "").strip()
    return [value] if value else []


def _build_auth_settings() -> AuthSettings:
    """Construct ``AuthSettings`` for streamable-http mode.

    Post the 2026-05-16 OAuth Proxy redesign, ``issuer_url`` here points at
    the **MCP server itself** (the OAuth Proxy IS the AS to MCP clients).
    The Frontegg issuer URL is still used by ``CombinedTokenVerifier`` for
    JWT ``iss`` validation; that's a separate constructor arg on the
    verifier and is not affected by this AuthSettings field.

    ``required_scopes`` is driven by ``MCP_REQUIRED_SCOPE`` env (empty by
    default; opt-in). The SDK enforces this scope at the auth-middleware
    level (returning 403 if a presented token lacks it); we keep it aligned
    with the verifier's scope check so the two layers agree.
    """
    resource = os.environ.get("MCP_RESOURCE_SERVER_URL", "")
    if not _frontegg_issuer() or not resource:
        # We deliberately don't crash here — the lifespan will print a
        # diagnostic and the placeholder verifier will reject every request,
        # which is the safe failure mode.
        _logger.warning(
            "FRONTEGG_DOMAIN and MCP_RESOURCE_SERVER_URL must both be set in "
            "streamable-http mode. Server will start but every request will "
            "be rejected by the auth layer."
        )
    return AuthSettings(
        # PRM's `authorization_servers` lists this URL — point clients at us.
        issuer_url=AnyHttpUrl(resource) if resource else AnyHttpUrl("https://example.invalid"),
        resource_server_url=AnyHttpUrl(resource) if resource else None,
        required_scopes=_required_scopes_from_env(),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            # Keep `okareo:use` advertised in DCR responses for future-proofing —
            # this is the scope MCP clients will request once we enforce it.
            valid_scopes=["okareo:use"],
            default_scopes=["okareo:use"],
        ),
    )


@asynccontextmanager
async def lifespan(server: FastMCP):
    """Initialize the server at startup. Never crashes — always yields.

    In **HTTP mode** there is no startup ``OKAREO_API_KEY``: each request
    carries its own credential (JWT or bearer-API-key) which the verifier
    converts to a per-request ``SessionCredential``. Tools build their own
    Okareo SDK client via ``get_okareo_client()`` (transport-aware). So we
    skip the global Okareo-client initialization entirely and only load
    provider keys + analytics.

    In **stdio mode** we still initialize the shared ``_okareo_client`` from
    ``OKAREO_API_KEY`` since that's the single-tenant install model.
    """
    global _server_ready, _okareo_client, _key_registry, _analytics_client

    base_url = os.environ.get("OKAREO_BASE_URL", "https://api.okareo.com/")
    _logger.info("Base URL: %s", base_url)

    if _HTTP_MODE:
        # HTTP mode — per-request credentials. No shared Okareo client.
        _key_registry = scan_provider_keys()
        if _key_registry:
            provider_names = ", ".join(sorted(_key_registry.keys()))
            _logger.info("Provider keys loaded: %s", provider_names)
        else:
            _logger.info("No provider API keys configured.")

        # Cross-layer-mistake guard (021-embedded-login). NEXT_PUBLIC_* vars
        # are build-time only — they get baked into the static web bundle
        # at `next build`. Injecting them at runtime in production has no
        # effect on the running page and almost certainly indicates someone
        # got the CI deploy pipeline wrong (mixed build-args with
        # --set-env-vars). Scoped to Cloud Run contexts only: in local
        # docker-compose with the consolidated .env pattern, the same file
        # legitimately drives BOTH build-args AND env_file, so these keys
        # are expected at runtime and the warning would be noise.
        # Detection: Cloud Run injects K_SERVICE, K_REVISION, K_CONFIGURATION.
        _in_cloud_run = bool(
            os.environ.get("K_SERVICE") or os.environ.get("K_CONFIGURATION")
        )
        if _in_cloud_run:
            _next_public_at_runtime = sorted(
                k for k in os.environ if k.startswith("NEXT_PUBLIC_")
            )
            if _next_public_at_runtime:
                _logger.warning(
                    "NEXT_PUBLIC_* env vars set at runtime in Cloud Run have "
                    "no effect on the served page (they're inlined at build "
                    "time). Found: %s. Move them to docker build --build-arg "
                    "in the CI pipeline; the deploy step should NOT include "
                    "them in --set-env-vars.",
                    _next_public_at_runtime,
                )

        port = os.environ.get("PORT", os.environ.get("FASTMCP_PORT", "8080"))
        _logger.info(
            "Okareo MCP server started successfully. "
            "Transport: streamable-http (multi-tenant), Port: %s",
            port,
        )
        _analytics_client = init_analytics()

        _server_ready = True
        try:
            yield {
                "okareo": None,
                "key_registry": _key_registry,
                "analytics": _analytics_client,
            }
        finally:
            _server_ready = False
            await shutdown_analytics(_analytics_client)
            _analytics_client = None
        return

    # ---- stdio mode below (single-tenant) ----
    api_key = os.environ.get("OKAREO_API_KEY", "").strip()

    if not api_key:
        _logger.warning(
            "OKAREO_API_KEY environment variable is not set. "
            "Get your API key from app.okareo.com and configure it "
            "in your MCP server settings."
        )
        # Server starts in degraded mode; tools will report the error.
        try:
            yield {"okareo": None, "key_registry": {}, "analytics": None}
        finally:
            pass
        return

    okareo = None
    # Redirect stdout to stderr during SDK init so any SDK prints
    # (e.g. validation errors) don't corrupt the MCP stdio protocol.
    _real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        okareo = create_okareo_client(api_key, base_url)
    except Exception as e:
        # Catch ALL exceptions — never crash during startup.
        _logger.warning(
            "Okareo client initialization failed (%s): %s. "
            "Server will attempt to reconnect on first tool call.",
            type(e).__name__,
            e,
        )
    finally:
        sys.stdout = _real_stdout

    if okareo is None:
        # Degraded mode: tools will attempt lazy re-initialization.
        try:
            yield {"okareo": None, "key_registry": {}, "analytics": None}
        finally:
            pass
        return

    _okareo_client = okareo
    _key_registry = scan_provider_keys()
    if _key_registry:
        provider_names = ", ".join(sorted(_key_registry.keys()))
        _logger.info("Provider keys loaded: %s", provider_names)
    else:
        _logger.info("No provider API keys configured.")

    transport = os.environ.get("TRANSPORT", "stdio")
    port = os.environ.get("PORT", "8000")
    _logger.info(
        "Okareo MCP server started successfully. Transport: %s, Port: %s",
        transport,
        port,
    )
    _analytics_client = init_analytics()

    _server_ready = True
    try:
        yield {
            "okareo": okareo,
            "key_registry": _key_registry,
            "analytics": _analytics_client,
        }
    finally:
        _server_ready = False
        await shutdown_analytics(_analytics_client)
        _analytics_client = None


_INSTRUCTIONS = (
    "Use the Okareo tools proactively whenever the user is working with AI evaluation, "
    "LLM quality testing, scenario management, or model registration.\n\n"
    "TOOL DOMAINS:\n"
    "- Scenarios: save_scenario, list_scenarios, get_scenario, create_scenario_version, "
    "preview_delete_scenario, delete_scenario\n"
    "- Generation Models: list_available_llms, register_generation_model, "
    "list_generation_models, get_generation_model, update_generation_model, "
    "delete_generation_model\n"
    "- Tests & Checks: list_checks, run_test, list_test_runs, get_test_run_results, "
    "get_conversation_transcript, reevaluate_test_run, create_or_update_check, "
    "generate_check, get_check, delete_check\n"
    "- Simulations: create_or_update_target, get_target, list_targets, delete_target, "
    "create_or_update_driver, get_driver, list_drivers, list_driver_voices, "
    "run_simulation, list_simulations\n"
    "- Voice Monitoring: ingest_conversations, connect_voice_integration, "
    "list_voice_integrations, get_voice_integration, update_voice_integration, "
    "rotate_voice_integration_secret, delete_voice_integration, get_voice_webhook_url\n"
    "- Analytics & Dashboards: query_analytics, list_dashboards, get_dashboard, "
    "save_dashboard, reorder_dashboards, delete_dashboard\n"
    "- Documentation: get_docs, get_templates\n\n"
    "KEY WORKFLOWS:\n"
    "1. Evaluate a model: list_scenarios and list_generation_models to discover existing "
    "resources → save_scenario to create test data → register_generation_model to register "
    "the model → list_checks to see available quality checks → run_test to evaluate → "
    "get_test_run_results to retrieve scores.\n"
    "2. Simulate multi-turn conversations: create_or_update_target to define the AI system "
    "under test (supports SSE streaming via 'streaming' config in next_message_params) → "
    "create_or_update_driver to define a simulated user persona → save_scenario "
    "for test cases → run_simulation (returns promptly: status 'finished' with results "
    "for short runs, or status 'running' with the test_run_id for longer runs that "
    "finish on their own) → get_test_run_results for scores → "
    "get_conversation_transcript to inspect individual conversation transcripts.\n"
    "3. Create custom checks: get_templates for check prompt/code templates → "
    "create_or_update_check to register a check → or generate_check to "
    "AI-generate a check from a description → list_checks to verify.\n"
    "4. Learn about Okareo: get_docs for conceptual explanations → get_templates for starter "
    "templates (check prompts, driver personas, scenarios).\n"
    "5. Monitor voice agents: ingest_conversations to submit completed voice calls "
    "(transcript or audio) for monitoring → connect_voice_integration + "
    "get_voice_webhook_url to wire a provider (Retell/Twilio/VAPI/ElevenLabs) for "
    "automatic ingestion.\n"
    "6. Simulate voice agents: list_driver_voices to discover voices/profiles/"
    "languages → create_or_update_driver with voice settings → run_simulation.\n"
    "7. Inspect trends: query_analytics for evaluation metrics → save_dashboard / "
    "list_dashboards to organize panels.\n\n"
    "BEST PRACTICES:\n"
    "- When the user wants to test or evaluate an AI model, start by listing existing "
    "scenarios and models to avoid creating duplicates.\n"
    "- Always call list_checks before run_test so you can suggest appropriate quality checks.\n"
    "- Use get_docs when the user asks conceptual questions about Okareo (e.g., 'what is a "
    "scenario?', 'how do checks work?').\n"
    "- Use get_templates when the user wants to create custom checks, driver personas, or "
    "configure SSE streaming for custom endpoint targets.\n"
    "- run_test and run_simulation return promptly without blocking: they respond with "
    "status 'finished' (results ready) for short runs, or status 'running' with a "
    "test_run_id (run continues on its own; longer runs include estimated_runtime) for "
    "long runs. Poll get_test_run_results with the test_run_id to retrieve scores.\n"
    "- For list_simulations, default detail_level='summary' returns compact results; use "
    "'detailed' to include model_metrics (limit capped to 5).\n"
    "- For simulation results, use get_test_run_results for score summaries first (transcripts "
    "excluded by default). Use get_conversation_transcript to inspect individual conversation "
    "transcripts by scenario_index.\n"
    "- Use generate_check when the user describes what they want to evaluate in plain language — "
    "it creates the check automatically without manual prompt or code authoring.\n"
    "- Use get_check to inspect a check's full configuration (prompt template or code). "
    "Use create_or_update_check to create checks with specific prompts or code.\n"
    "- get_check accepts an optional version; list_checks(all_versions=true) shows the "
    "full version history. reevaluate_test_run re-scores a finished run against "
    "checks without re-running the model — omit `checks` to reuse the run's own.\n"
    "- For voice ingestion, each conversation needs a call_id plus a transcript or an "
    "audio reference; invalid ones are reported in a 'rejected' list, not failed wholesale.\n\n"
    "TENANT SELECTION (OAuth sessions only):\n"
    "- If a user is associated with more than one Okareo organization (Frontegg "
    "tenant), call list_tenants to see their options and switch_tenant(tenant_id) "
    "to change the active one. The selection is SESSION-SCOPED: if this "
    "conversation is being resumed after the MCP transport was closed (e.g., the "
    "copilot restarted), inspect the conversation transcript for the most recent "
    "switch_tenant call and re-issue it BEFORE the next tenant-scoped tool call. "
    "Every list_tenants response carries active_tenant_id and "
    "active_tenant_source; use these to confirm the override is in effect before "
    "assuming so. On Bearer-API-key sessions both tenant tools return "
    "tenant_selection_requires_oauth — the API-key bearer path is single-org."
)


def _default_host_port() -> tuple[str, int]:
    """Pick host/port defaults appropriate to the transport mode.

    - streamable-http: bind 0.0.0.0:$PORT (Cloud Run conventions; PORT
      defaults to 8080). FASTMCP_HOST/FASTMCP_PORT still take precedence
      if explicitly set, for local dev override.
    - stdio / sse: keep the legacy 127.0.0.1:8000 defaults.
    """
    if _HTTP_MODE:
        host = os.environ.get("FASTMCP_HOST", "0.0.0.0")
        port = int(os.environ.get("PORT", os.environ.get("FASTMCP_PORT", "8080")))
    else:
        host = os.environ.get("FASTMCP_HOST", "127.0.0.1")
        port = int(os.environ.get("FASTMCP_PORT", "8000"))
    return host, port


_host, _port = _default_host_port()

_fastmcp_kwargs: dict = {
    "instructions": _INSTRUCTIONS,
    "lifespan": lifespan,
    "host": _host,
    "port": _port,
}

def _audience_aliases() -> list[str]:
    """Audience values the JWT verifier and handoff accept.

    The MCP resource URL (with and without trailing slash) is canonical;
    Frontegg-issued tokens carry ``aud = <vendor_id>`` by default so we
    also accept ``FRONTEGG_VENDOR_ID`` when set.
    """
    resource = os.environ.get("MCP_RESOURCE_SERVER_URL", "").rstrip("/")
    aliases: list[str] = []
    if resource:
        aliases.extend([resource, resource + "/"])
    vendor_id = os.environ.get("FRONTEGG_VENDOR_ID", "").strip()
    if vendor_id:
        aliases.append(vendor_id)
    return aliases


def _build_real_verifier(jwks_cache) -> TokenVerifier:
    """Construct the production ``CombinedTokenVerifier`` from env config."""
    from src.auth.api_key_verifier import OkareoAPIKeyVerifier
    from src.auth.verifier import CombinedTokenVerifier

    issuer = _frontegg_issuer()
    resource = os.environ.get("MCP_RESOURCE_SERVER_URL", "")
    okareo_base = os.environ.get("OKAREO_BASE_URL", "https://api.okareo.com/")

    additional_audiences: list[str] = []
    vendor_id = os.environ.get("FRONTEGG_VENDOR_ID", "").strip()
    if vendor_id:
        additional_audiences.append(vendor_id)

    # Scope enforcement is opt-in. Default: no required scope (Frontegg
    # doesn't issue MCP-specific scopes by default and we don't yet do
    # per-tool scope gating). Set MCP_REQUIRED_SCOPE=okareo:use (or whatever)
    # once the Frontegg token template emits it and we're ready to enforce.
    required_scopes_list = _required_scopes_from_env()
    required_scope = required_scopes_list[0] if required_scopes_list else ""

    api_key_verifier = OkareoAPIKeyVerifier(base_url=okareo_base)

    return CombinedTokenVerifier(
        issuer_url=issuer,
        resource_server_url=resource,
        jwks_cache=jwks_cache,
        api_key_resolver=api_key_verifier.verify,
        additional_audiences=additional_audiences,
        required_scope=required_scope,
    )


if _HTTP_MODE:
    # Single shared JWKSCache instance: used by both the runtime verifier
    # (on every /mcp request) and the embedded-login handoff endpoint
    # (validates the Frontegg JWT before populating the pending record,
    # feature 021-embedded-login).
    from src.auth.jwks_cache import JWKSCache as _JWKSCache

    _jwks_cache = _JWKSCache(_frontegg_issuer())

    # AuthSettings + the real verifier wire in the spec-mandated auth
    # boundary: SDK auto-mounts /.well-known/oauth-protected-resource,
    # emits WWW-Authenticate on every 401, and validates Bearer credentials
    # via CombinedTokenVerifier (JWT or API-key fallback).
    _fastmcp_kwargs["token_verifier"] = _build_real_verifier(_jwks_cache)
    _fastmcp_kwargs["auth"] = _build_auth_settings()
    _fastmcp_kwargs["stateless_http"] = True
    _fastmcp_kwargs["json_response"] = True

mcp = FastMCP("okareo-mcp", **_fastmcp_kwargs)

if _HTTP_MODE:
    # OAuth Proxy wiring (2026-05-16 redesign):
    # We act as a full OAuth 2.1 AS to MCP clients. State lives in a
    # per-instance OAuthStateStore that's shared between the DCR endpoint
    # (`/register`) and the four OAuth Proxy endpoints (/oauth/*).
    from starlette.requests import Request as _StarletteRequest

    from src.auth.dcr_proxy import build_dcr_app
    from src.auth.oauth_proxy import (
        ProxyConfig,
        register_oauth_proxy_routes,
    )
    from src.auth.oauth_state import OAuthStateStore

    # Stateless DCR signing key (FR-021/FR-022). If unset, generate an
    # ephemeral per-instance key — handy in dev, but every restart and every
    # cross-instance hop invalidates existing client_ids. Production deploys
    # MUST set this to a stable, ≥256-bit value from the environment (or a
    # secret manager).
    _dcr_signing_key = os.environ.get("MCP_DCR_SIGNING_KEY", "").strip()
    if not _dcr_signing_key:
        import secrets as _secrets

        _dcr_signing_key = _secrets.token_urlsafe(43)
        _logger.warning(
            "MCP_DCR_SIGNING_KEY is not set. Using an ephemeral "
            "per-instance signing key — all DCR-issued client_ids will be "
            "invalidated on this container's next restart, and cross-instance "
            "OAuth flows will fail. Set MCP_DCR_SIGNING_KEY=<32+ random bytes> "
            "for production."
        )
    elif len(_dcr_signing_key.encode("utf-8")) < 32:
        # FR-022 mandates ≥32 bytes (≥256 bits) of entropy. Shorter keys are
        # honored (don't break the server) but produce a loud warning so
        # operators know they're outside the documented threshold.
        _logger.warning(
            "MCP_DCR_SIGNING_KEY is shorter than the recommended 32 bytes "
            "(%d bytes supplied). FR-022 recommends ≥256 bits of entropy; "
            "the current value is below that threshold and weakens "
            "HMAC-forgery resistance. Replace with a longer value before "
            "production.",
            len(_dcr_signing_key.encode("utf-8")),
        )
    _oauth_state = OAuthStateStore(dcr_signing_key=_dcr_signing_key)
    _proxy_config = ProxyConfig(
        resource_server_url=os.environ.get("MCP_RESOURCE_SERVER_URL", "").rstrip("/")
        or "http://localhost:8080",
        frontegg_domain=os.environ.get("FRONTEGG_DOMAIN", "").strip(),
        frontegg_client_id=os.environ.get("FRONTEGG_CLIENT_ID", "").strip(),
    )

    # Mount the four OAuth Proxy routes (AS metadata + /oauth/authorize +
    # /oauth/callback + /oauth/token).
    register_oauth_proxy_routes(mcp, _oauth_state, _proxy_config)

    # Mount /register (DCR) sharing the same state.
    _dcr_app = build_dcr_app(_oauth_state)

    @mcp.custom_route("/register", methods=["POST"])
    async def _dcr_register(request: _StarletteRequest):  # noqa: ARG001
        for route in _dcr_app.router.routes:
            if getattr(route, "path", None) == "/register":
                return await route.endpoint(request)
        from starlette.responses import JSONResponse

        return JSONResponse(
            {"error": "server_error", "error_description": "DCR proxy misconfigured"},
            status_code=500,
        )

    # Embedded login handoff (feature 021-embedded-login). Same OAuthStateStore
    # as the OAuth Proxy; shared JWKSCache from the verifier. CORS preflight
    # is handled inside the route factory (dev mode only).
    from src.auth.embedded_handoff import make_handoff_route

    _handoff_handler = make_handoff_route(
        _oauth_state,
        _proxy_config,
        _jwks_cache,
        audience_aliases=_audience_aliases(),
    )

    @mcp.custom_route("/oauth/handoff", methods=["POST", "OPTIONS"])
    async def _oauth_handoff(request: _StarletteRequest):
        return await _handoff_handler(request)

    # Mount the embedded login page (Next.js static export bundled at
    # /app/web by the Docker web-builder stage). Stdio mode never reaches
    # this code path, so the static files are not loaded in airgapped runs.
    # When /app/web is absent (e.g., running outside the container without
    # a prior `next build`), log a warning and skip — /login will 404 but
    # the server still boots so other dev workflows remain unaffected.
    #
    # Disk layout: Next.js static export with `basePath: '/login'` writes
    # `out/index.html`, `out/_next/static/*`, etc. — the basePath rewrites
    # in-page asset URLs to `/login/_next/...` but the on-disk paths stay
    # at the export root. We mount that root at /login here so the URL
    # space lines up with the basePath.
    _web_root = os.environ.get("MCP_EMBEDDED_LOGIN_WEB_ROOT", "/app/web")
    if os.path.isdir(_web_root) and os.path.isfile(
        os.path.join(_web_root, "index.html")
    ):
        from starlette.staticfiles import StaticFiles

        _static_mount = StaticFiles(directory=_web_root, html=True)

        @mcp.custom_route("/login", methods=["GET", "HEAD"])
        async def _login_index(request: _StarletteRequest):
            return await _static_mount.get_response("index.html", request.scope)

        @mcp.custom_route("/login/{path:path}", methods=["GET", "HEAD"])
        async def _login_asset(request: _StarletteRequest):
            sub = request.path_params.get("path", "")
            try:
                return await _static_mount.get_response(sub, request.scope)
            except Exception:
                # SPA fallback — serve index.html for unknown sub-paths.
                return await _static_mount.get_response("index.html", request.scope)

        # llms.txt convention (llmstxt.org): served at the domain root so agents
        # can discover the MCP server's tool surface at
        # https://tools.okareo.com/llms.txt. Bundled into the web export from
        # web/public/llms.txt → /app/web/llms.txt.
        if os.path.isfile(os.path.join(_web_root, "llms.txt")):

            @mcp.custom_route("/llms.txt", methods=["GET", "HEAD"])
            async def _llms_txt(request: _StarletteRequest):
                return await _static_mount.get_response("llms.txt", request.scope)

        # Brand assets served at the domain root (e.g. for the Anthropic
        # Connector Directory logo URL, which expects a top-level public URL,
        # not /login/…). The basePath rewrite only covers in-page references,
        # so root requests need explicit routes like llms.txt above. Each is
        # bundled from web/public/<name> → /app/web/<name> at build time.
        for _brand_asset in ("okareo-logo.svg", "okareo-mark.svg"):
            if not os.path.isfile(os.path.join(_web_root, _brand_asset)):
                continue

            def _make_brand_route(_name: str):
                async def _serve(request: _StarletteRequest):
                    return await _static_mount.get_response(_name, request.scope)

                return _serve

            mcp.custom_route(f"/{_brand_asset}", methods=["GET", "HEAD"])(
                _make_brand_route(_brand_asset)
            )

        _logger.info("Embedded login mounted at /login (root=%s)", _web_root)
    else:
        _logger.warning(
            "Embedded login web root not found at %s; /login will return 404. "
            "Build the web/ subproject (`cd web && yarn build`) or set "
            "MCP_EMBEDDED_LOGIN_WEB_ROOT to point at the built output.",
            _web_root,
        )

    # Security-headers middleware (feature 021-embedded-login). Applied to
    # /login* and /oauth/handoff only — other routes preserve their 020-era
    # response shape unchanged.
    try:
        from src.auth.security_headers import EmbeddedLoginSecurityHeadersMiddleware

        _underlying_app = mcp.streamable_http_app() if hasattr(
            mcp, "streamable_http_app"
        ) else None
        if _underlying_app is not None and hasattr(_underlying_app, "add_middleware"):
            _underlying_app.add_middleware(
                EmbeddedLoginSecurityHeadersMiddleware,
                frontegg_domain=os.environ.get("FRONTEGG_DOMAIN", "").strip(),
            )
        else:
            _logger.warning(
                "Embedded-login security headers middleware NOT attached: "
                "FastMCP did not expose an add_middleware hook. Headers will "
                "be missing on /login* and /oauth/handoff. Investigate before "
                "production deploy."
            )
    except Exception as _sec_err:  # pragma: no cover — defensive
        _logger.warning(
            "Security headers middleware setup raised %s; continuing without it.",
            type(_sec_err).__name__,
        )

    # Cheap liveness endpoint for Docker/Cloud Run healthchecks. Distinct
    # from `/.well-known/oauth-protected-resource` so the healthcheck doesn't
    # generate misleading "PRM access" traffic at every interval. Combined
    # with the access-log filter below, healthcheck hits are silent.
    @mcp.custom_route("/health", methods=["GET", "HEAD"])
    async def _health(request: _StarletteRequest):  # noqa: ARG001
        from starlette.responses import JSONResponse

        return JSONResponse({"status": "ok"})

    # Silence uvicorn's access log for /health to keep docker logs scannable.
    # We do NOT silence /.well-known/oauth-protected-resource because that's
    # a real client-traffic endpoint and we want to see it in logs.
    class _SuppressHealthAccessLogs(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            # uvicorn.access's format: "%s - "%s" %d" (client, request, status).
            # The request string is record.args[2] in the default config.
            try:
                request_line = str(record.args[2]) if record.args else ""
            except (IndexError, TypeError):
                return True
            return "/health" not in request_line

    logging.getLogger("uvicorn.access").addFilter(_SuppressHealthAccessLogs())

# Register all tools
tests.register_tools(mcp)
scenarios.register_tools(mcp)
models.register_tools(mcp)
simulations.register_tools(mcp)
checks.register_tools(mcp)
docs.register_tools(mcp)
voice.register_tools(mcp)
insights.register_tools(mcp)
# Tenant-management tools (FR-023..FR-029). Both no-op on stdio mode because
# they immediately check credential.kind and require an OAuth session.
tenants.register_tools(mcp)


if _HTTP_MODE:
    # FR-028 dual-eviction backstop: TTL is load-bearing; if the SDK happens
    # to expose a session-end / disconnect callback, hook into it for an
    # opportunistic fast-path cleanup. We probe at startup and skip silently
    # if the surface isn't there — server boot MUST NOT fail on this path.
    try:
        from src.auth import frontegg_user_info as _frontegg_user_info
        from src.auth import tenant_state as _tenant_state

        # Different mcp[cli] minor versions name this differently. Try a few.
        _hook_attached = False
        for attr in ("on_session_end", "on_disconnect", "on_session_close"):
            cb = getattr(mcp, attr, None)
            if callable(cb):
                def _cleanup(session_id: str) -> None:
                    _tenant_state.clear_session(session_id)
                    _frontegg_user_info.invalidate_cache(session_id)

                cb(_cleanup)  # type: ignore[misc]
                _hook_attached = True
                _logger.info("Registered session-end hook via mcp.%s", attr)
                break
        if not _hook_attached:
            _logger.info(
                "No FastMCP session-end hook exposed; relying on "
                "tenant_state TTL eviction (default 30 min) for cleanup."
            )
    except Exception as _hook_err:  # pragma: no cover — defensive only
        _logger.warning(
            "Session-end hook probe raised %s; continuing without hook "
            "(TTL eviction still active).",
            type(_hook_err).__name__,
        )

# Instrument tool calls with analytics and graceful error handling
_original_call_tool = mcp.call_tool


def _tool_log(line: str) -> None:
    """Emit a single tool-call log line at INFO.

    Routed through `logging` so FASTMCP_LOG_LEVEL governs visibility — the
    Docker image pins WARNING (so these are hidden in production logs),
    while local runs keep the FastMCP INFO default and see them.
    """
    _logger.info(line)


def _current_org_id() -> str | None:
    """Best-effort lookup of the calling org_id (HTTP mode only)."""
    if not _HTTP_MODE:
        return None
    try:
        from src.auth.context import get_session_credential_optional

        cred = get_session_credential_optional()
        return cred.org_id if cred else None
    except Exception:  # pragma: no cover — defensive
        return None


# Per-credential throttle (FR-013 / SC-007). Instantiated once per server
# process; one TokenBucket per org_id, lazily created. Disabled in stdio
# mode (single-tenant has no use for per-credential throttling).
if _HTTP_MODE:
    from src.auth.throttle import PerCredentialThrottle

    _throttle = PerCredentialThrottle()
else:
    _throttle = None  # type: ignore[assignment]


def _error_content(message: str) -> CallToolResult:
    """Wrap a pre-formatted error/throttle JSON string as an MCP error result.

    Must be a full ``CallToolResult`` with ``isError=True``, not bare content:
    every tool here is annotated ``-> str``, so FastMCP advertises an
    outputSchema, and the low-level CallTool handler rejects any non-error
    result lacking structuredContent ("Output validation error: outputSchema
    defined but no structured output returned") — masking the real error.
    A CallToolResult is passed through verbatim, skipping that validation,
    which matches how stock FastMCP surfaces tool exceptions.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        isError=True,
    )


async def _instrumented_call_tool(name, arguments):
    global _okareo_client, _key_registry

    # Throttle (HTTP mode only). Reject early with a 429-shaped tool error
    # so the abusive credential doesn't reach the Okareo backend at all.
    if _HTTP_MODE and _throttle is not None and _throttle.enabled:
        org_id = _current_org_id()
        if org_id:
            allowed, retry_after = _throttle.try_acquire(org_id)
            if not allowed:
                import json
                return _error_content(
                    json.dumps(
                        {
                            "error": {
                                "code": "rate_limited",
                                "message": (
                                    "Too many requests for this credential. "
                                    f"Retry after ~{retry_after:.1f}s."
                                ),
                                "data": {
                                    "retry_after_seconds": retry_after,
                                },
                            }
                        }
                    )
                )

    # Lazy re-initialization (stdio mode only — HTTP mode constructs a
    # per-request Okareo client inside each tool via get_okareo_client(),
    # which reads the SessionCredential ContextVar; the shared _okareo_client
    # global is never populated in HTTP mode and shouldn't be checked).
    if not _HTTP_MODE and _okareo_client is None:
        api_key = os.environ.get("OKAREO_API_KEY", "").strip()
        if not api_key:
            return _error_content(
                format_tool_error(
                    ValueError(
                        "OKAREO_API_KEY is not set. "
                        "Get your key at app.okareo.com and add it to "
                        "your MCP server configuration."
                    ),
                    _key_registry,
                )
            )
        base_url = os.environ.get("OKAREO_BASE_URL", "https://api.okareo.com/")
        try:
            _okareo_client = create_okareo_client(api_key, base_url)
            _key_registry = scan_provider_keys()
        except Exception as e:
            return _error_content(format_tool_error(e, _key_registry))

    org_id = _current_org_id()
    started_at = time.monotonic()
    _tool_log(f"[tool] CALL  name={name} org={org_id or '-'}")

    success = True
    error_summary = ""
    try:
        result = await _original_call_tool(name, arguments)
        return result
    except Exception as e:
        success = False
        error_summary = type(e).__name__
        return _error_content(format_tool_error(e, _key_registry))
    finally:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        outcome = "OK" if success else f"FAIL({error_summary})"
        _tool_log(
            f"[tool] DONE  name={name} org={org_id or '-'} "
            f"outcome={outcome} duration_ms={duration_ms}"
        )
        # Analytics: fire-and-forget, never blocks tool execution
        try:
            if _analytics_client is not None:
                emit_tool_event(_analytics_client, tool_name=name, success=success)
        except Exception:
            pass


# Route tool dispatch through the instrumented wrapper (analytics, throttling,
# graceful error formatting). FastMCP binds its own ``call_tool`` into the
# low-level server's request_handlers during __init__ (_setup_handlers), so
# assigning ``mcp.call_tool`` here would be a no-op the dispatcher never reads.
# Re-register the CallToolRequest handler instead. ``validate_input=False``
# matches FastMCP's own registration (input validation happens in the tool
# manager via convert_result); ``_instrumented_call_tool`` delegates to the
# original ``_original_call_tool`` captured above, so behavior on the success
# path is unchanged.
mcp._mcp_server.call_tool(validate_input=False)(_instrumented_call_tool)


def main():
    """CLI entry point for okareo-mcp."""
    transport = _TRANSPORT

    if transport == "streamable-http":
        # FastMCP handles the listener; host/port were passed at construction.
        mcp.run(transport="streamable-http")
    else:
        # stdio (default) and any other value falls through to mcp.run().
        mcp.run(transport=transport)


if __name__ == "__main__":
    main()
