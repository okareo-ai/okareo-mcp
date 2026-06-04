"""Simulation tools for the Okareo MCP server.

Provides nine MCP tools for the multi-turn simulation workflow:

- create_or_update_target: Create or update a Target (Generation, Custom Endpoint, or Voice)
    by name (upsert). Supports optional auth_params for custom_endpoint targets.
- get_target: Retrieve a Target's configuration by name (all types)
- list_targets: List all simulation targets (voice and custom_endpoint) in the project
- delete_target: Remove a simulation target and all its related test data
- create_or_update_driver: Create or update a Driver persona by name (upsert)
- get_driver: Retrieve a Driver's configuration by name
- list_drivers: List all Drivers in the project
- run_simulation: Run a multi-turn simulation (or rerun an existing one with overrides)
- list_simulations: List past simulation runs (MULTI_TURN type)
"""

import json
import os
import re
from typing import Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from src.error_handling import format_tool_error
from src.okareo_client import (
    find_test_runs,
    get_okareo_client,
    okareo_api_request,
    resolve_project_id,
)


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


_RESERVED_TEMPLATE_VARS = frozenset({
    "scenario_input",
    "scenario_result",
    "session_id",
    "scenario_row_run_guid",
    "message_history",
    "latest_message",
    "access_token",
})


def _prefix_template_vars(prompt_template: str) -> str:
    """Auto-prefix bare mustache references with ``scenario_input.``.

    Scans *prompt_template* for ``{name}`` patterns and rewrites any bare
    reference (i.e. not a reserved variable and not already dot-prefixed with
    ``scenario_input.`` or ``scenario_result.``) to
    ``{scenario_input.name}``.
    """

    def _replace(match: re.Match) -> str:
        var = match.group(1).strip()
        if var in _RESERVED_TEMPLATE_VARS:
            return match.group(0)
        if var.startswith("scenario_input.") or var.startswith("scenario_result."):
            return match.group(0)
        return "{scenario_input." + var + "}"

    return re.sub(r"\{([^}]+)\}", _replace, prompt_template)


TWILIO_SENSITIVE_FIELDS = [
    "apikey",
    "authorization",
    "clientid",
    "clientsecret",
    "password",
    "secret",
    "token",
    "accesstoken",
    "refreshtoken",
]


def _build_custom_endpoint_sensitive_fields(
    auth_params: dict, caller_sensitive: list | None = None
) -> list[str]:
    """Build merged sensitive_fields list for custom_endpoint targets with auth.

    Auto-generates 'auth_params.<key>' for every top-level key in auth_params,
    then merges with any caller-supplied deep dot-paths. Returns a deduplicated,
    sorted list.
    """
    auto = [f"auth_params.{k}" for k in auth_params.keys()]
    merged = set(auto)
    if caller_sensitive:
        merged.update(caller_sensitive)
    return sorted(merged)


def _build_streaming_config(streaming_dict: dict | None):
    """Build a StreamingConfig from a user-provided dict, or return None.

    Returns a StreamingConfig on success, None if input is None/empty,
    or a string error message if validation fails.
    """
    if not streaming_dict:
        return None

    from okareo.model_under_test import (
        StreamingConfig,
        StreamingSelectCondition,
        StreamingStopCondition,
    )

    stop_conditions = []
    for i, sc in enumerate(streaming_dict.get("stop") or []):
        if not isinstance(sc, dict) or "value" not in sc:
            return f"streaming.stop[{i}] must have a 'value' key."
        stop_conditions.append(
            StreamingStopCondition(value=sc["value"], path=sc.get("path"))
        )

    select_conditions = []
    for i, sc in enumerate(streaming_dict.get("select") or []):
        if not isinstance(sc, dict) or "path" not in sc or "value" not in sc:
            return f"streaming.select[{i}] must have both 'path' and 'value' keys."
        select_conditions.append(
            StreamingSelectCondition(path=sc["path"], value=sc["value"])
        )

    return StreamingConfig(
        stop=stop_conditions or None,
        select=select_conditions or None,
    )


def _build_auth_config(auth_params: dict):
    """Build an AuthConfig from a user-provided auth_params dict.

    Returns an AuthConfig instance.
    """
    from okareo.model_under_test import AuthConfig

    kwargs = {
        "url": auth_params["url"],
        "method": auth_params.get("method", "POST"),
        "response_access_token_path": auth_params.get("response_access_token_path", ""),
    }
    if auth_params.get("headers"):
        kwargs["headers"] = auth_params["headers"]
    if auth_params.get("body"):
        body = auth_params["body"]
        if isinstance(body, dict):
            body = json.dumps(body)
        kwargs["body"] = body
    if auth_params.get("status_code") is not None:
        kwargs["status_code"] = auth_params["status_code"]

    return AuthConfig(**kwargs)


# Keys a driver-voice catalog entry may use to name itself. The
# /v0/driver_voices endpoint returns free-form objects, so candidate voice
# identifiers are collected defensively across these keys.
_VOICE_ID_KEYS = ("id", "name", "voice", "voice_id", "slug")


def _fetch_voice_catalog(okareo) -> dict:
    """Fetch the available voices, voice profiles, and languages.

    Sourced from /v0/driver_voices and /v0/driver_profiles. Raises on a
    transport error so callers can surface an unavailable-catalog message.
    """
    voices = okareo_api_request(okareo, "get", "/v0/driver_voices") or []
    profiles = okareo_api_request(okareo, "get", "/v0/driver_profiles") or []
    languages: list[str] = []
    seen = set()
    for v in voices:
        lang = v.get("language") if isinstance(v, dict) else None
        if isinstance(lang, str) and lang not in seen:
            seen.add(lang)
            languages.append(lang)
    return {
        "voices": voices,
        "voice_profiles": profiles,
        "languages": sorted(languages),
    }


def _voice_id_set(voices) -> set:
    """Collect candidate voice identifiers from a /v0/driver_voices payload."""
    ids: set = set()
    for v in voices:
        if isinstance(v, dict):
            for k in _VOICE_ID_KEYS:
                val = v.get(k)
                if isinstance(val, str):
                    ids.add(val)
        elif isinstance(v, str):
            ids.add(v)
    return ids


def _profile_name_set(profiles) -> set:
    """Collect voice-profile names from a /v0/driver_profiles payload."""
    names: set = set()
    for p in profiles:
        name = p.get("profile_name") if isinstance(p, dict) else p
        if isinstance(name, str):
            names.add(name)
    return names


# --- Long-running simulation buffering (spec 025-long-running-simulations) ---
# run_test executes synchronously on the backend and *survives client
# disconnect*: the backend handler runs to completion in its own threadpool
# thread regardless of the caller, and the run is finalized inside that still-
# open request (validated empirically; see specs/025-long-running-simulations/
# research.md). So we run the blocking submission in a background thread and,
# after giving the run a moment to register, look it up via find_test_runs to
# hand the co-pilot a pollable run id — a faux-async handoff that keeps the tool
# call well under the client timeout while the run finishes on its own.

_SIM_BUFFER_SECONDS_DEFAULT = 25
_SIM_POLL_INTERVAL_SECONDS = 2.0


def _sim_buffer_seconds() -> float:
    """Buffer window (seconds) to wait inline before handing off. Tunable via
    OKAREO_SIM_BUFFER_SECONDS; kept safely under the co-pilot tool-call timeout."""
    raw = os.environ.get("OKAREO_SIM_BUFFER_SECONDS", "").strip()
    if raw:
        try:
            val = float(raw)
            if val >= 0:
                return val
        except ValueError:
            pass
    return float(_SIM_BUFFER_SECONDS_DEFAULT)


def _estimate_runtime_seconds(row_count, max_turns, repeats, is_voice) -> int:
    """Rough, advisory wall-clock estimate for a simulation run.

    Voice runs are materially slower per turn than text. Directional only —
    used to set co-pilot expectations / polling cadence, never to gate results.
    """
    conversations = max(1, int(row_count or 1)) * max(1, int(repeats or 1))
    per_turn = 18 if is_voice else 4
    per_conversation = max(1, int(max_turns or 1)) * per_turn + 6
    backend_concurrency = 2 if is_voice else 4
    return int(per_conversation * conversations / backend_concurrency)


def _format_duration(seconds) -> str:
    seconds = max(1, int(seconds))
    if seconds < 90:
        return f"~{seconds}s"
    return f"~{round(seconds / 60)} min"


def _find_runs(okareo, project_id, scenario_set_id, types) -> dict:
    """Best-effort ``{run_id: {"name":, "app_link":}}`` for runs of a scenario,
    filtered to the given test-run ``types``. Never raises — returns ``{}`` on any
    error so discovery failures degrade to a handoff without an id rather than
    breaking the tool."""
    try:
        from okareo_api_client.errors import UnexpectedStatus
        from okareo_api_client.models.general_find_payload import GeneralFindPayload

        payload = GeneralFindPayload(
            project_id=project_id,
            scenario_set_id=scenario_set_id,
            types=list(types),
            return_model_metrics=False,
        )
        try:
            runs = find_test_runs(okareo, payload)
        except UnexpectedStatus as ue:
            runs = json.loads(ue.content) if ue.status_code == 200 else None
        out: dict = {}
        if runs and not isinstance(runs, Exception):
            for r in runs:
                rid = r.get("id") if isinstance(r, dict) else _get_attr(r, "id")
                if not rid:
                    continue
                out[str(rid)] = {
                    "name": r.get("name") if isinstance(r, dict) else _get_attr(r, "name"),
                    "app_link": (
                        r.get("app_link") if isinstance(r, dict)
                        else _get_attr(r, "app_link")
                    ) or "",
                }
        return out
    except Exception:
        return {}


def _buffered_submit(
    submit_thunk, *, okareo, project_id, scenario_set_id, name, types=None
):
    """Run a blocking run/simulation submission in a background thread; return
    early as a faux-async handoff if it exceeds the buffer window.

    ``types`` filters run discovery (defaults to MULTI_TURN for simulations).

    Returns ``(status, payload, run_id, app_link)``:
      - ``("finished", result, run_id, app_link)`` — completed within the window
      - ``("running",  None,   run_id, app_link)`` — handed off, still running
      - ``("failed",   exception, run_id, app_link)`` — raised before the window
    """
    import threading
    import time

    if types is None:
        from okareo_api_client.models.test_run_type import TestRunType
        types = [TestRunType.MULTI_TURN]

    holder: dict = {}

    def _runner():
        try:
            holder["result"] = submit_thunk()
        except BaseException as exc:  # capture everything to surface inline
            holder["error"] = exc

    # Snapshot existing runs first so the newly created one is identifiable.
    pre_existing = set(
        _find_runs(okareo, project_id, scenario_set_id, types).keys()
    )

    thread = threading.Thread(
        target=_runner, name=f"okareo-sim-{name}"[:64], daemon=True
    )
    thread.start()

    deadline = time.monotonic() + _sim_buffer_seconds()
    run_id = None
    app_link = ""

    while True:
        # Returns as soon as the run finishes, else after one poll interval.
        thread.join(timeout=_SIM_POLL_INTERVAL_SECONDS)
        if not thread.is_alive():
            break
        if run_id is None:
            current = _find_runs(okareo, project_id, scenario_set_id, types)
            new_ids = [rid for rid in current if rid not in pre_existing]
            match = [rid for rid in new_ids if current[rid].get("name") == name]
            chosen = match[0] if match else (new_ids[0] if len(new_ids) == 1 else None)
            if chosen:
                run_id = chosen
                app_link = current[chosen].get("app_link", "")
        if time.monotonic() >= deadline:
            break

    if not thread.is_alive():
        if "error" in holder:
            return ("failed", holder["error"], run_id, app_link)
        return ("finished", holder["result"], run_id, app_link)
    return ("running", None, run_id, app_link)


def _build_handoff_response(
    status, result, run_id, app_link, *,
    name, project_id, estimate_seconds, based_on_run_id, extra,
    noun="Simulation", transcript_hint=True,
):
    """Assemble the run/simulation response for a finished or running handoff.

    ``noun`` and ``transcript_hint`` adapt the human message for the multi-turn
    simulation tool vs. the single-turn test tool.
    """
    if status == "finished":
        rid = _get_attr(result, "id", "") or run_id or ""
        finished_msg = (
            f"{noun} complete. Retrieve scores with get_test_run_results "
            "using the test_run_id"
        )
        finished_msg += (
            " (full transcripts via get_conversation_transcript)."
            if transcript_hint else "."
        )
        response = {
            "test_run_id": rid,
            "name": _get_attr(result, "name", name),
            "app_link": _get_attr(result, "app_link", "") or app_link,
            "status": "finished",
            "message": finished_msg,
        }
    else:  # running — faux-async handoff
        link = app_link or (
            f"https://app.okareo.com/project/{project_id}/eval/{run_id}"
            if run_id else ""
        )
        response = {
            "test_run_id": run_id or "",
            "name": name,
            "app_link": link,
            "status": "running",
            "message": (
                f"{noun} started successfully and is still running; it will "
                "continue to completion on its own. Poll get_test_run_results "
                "with the test_run_id for status and scores, or open the app_link."
            ),
        }
        if estimate_seconds:
            response["estimated_runtime"] = _format_duration(estimate_seconds)
            response["estimated_runtime_seconds"] = int(estimate_seconds)
        if not run_id:
            response["message"] += (
                " The run id was not confirmed within the buffer window; use "
                "list_simulations (most recent) to locate it by name."
            )
    if extra:
        response.update(extra)
    if based_on_run_id:
        response["based_on_run_id"] = based_on_run_id
    return response


def register_tools(mcp: FastMCP) -> None:
    """Register all simulation tools with the FastMCP server."""

    @mcp.tool(
        title="Create or Update Target",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def create_or_update_target(
        name: str,
        type: str,
        # Generation target fields
        model_id: Optional[str] = None,
        temperature: float = 0.0,
        system_prompt_template: Optional[str] = None,
        user_prompt_template: Optional[str] = None,
        dialog_template: Optional[str] = None,
        tools: Optional[list] = None,
        # Custom endpoint fields (nested params)
        next_message_params: Optional[dict] = None,
        start_session_params: Optional[dict] = None,
        end_session_params: Optional[dict] = None,
        # Custom endpoint auth (optional)
        auth_params: Optional[dict] = None,
        sensitive_fields: Optional[list] = None,
        max_parallel_requests: Optional[int] = None,
        # Voice fields
        edge_type: Optional[str] = None,
        model: Optional[str] = None,
        output_voice: Optional[str] = None,
        instructions: Optional[str] = None,
        # Voice Twilio fields
        to_phone_number: Optional[str] = None,
        account_sid: Optional[str] = None,
        auth_token: Optional[str] = None,
        from_phone_number: Optional[str] = None,
    ) -> str:
        """Create or update a Target — the AI system you want to evaluate in a simulation.

        Calling create_or_update_target with the same name as an existing Target will
        **fully replace** its configuration — caller must re-specify all desired fields.
        Supported types: 'generation' (foundation model), 'custom_endpoint' (your own
        REST API), and 'voice' (voice-based targets via OpenAI, Deepgram, or Twilio).

        **Cloning workflow**: this tool accepts the same key structure that `get_target`
        returns, so you can read an existing Target, change `name`, swap in real values
        for any field whose value is `"***REDACTED***"`, and pass the result here as
        kwargs. Calls that still contain the redaction sentinel are rejected with an
        error naming each offending path; the sentinel is never forwarded to the backend.

        Args:
            name: Unique name for this target.
            type: Target type — 'generation', 'custom_endpoint', or 'voice'.

            model_id: (generation targets) Foundation model ID, e.g. 'gpt-4o-mini'.
            temperature: (generation targets) Response randomness, default 0.
            system_prompt_template: (generation targets) System instructions; mustache
                syntax supported, e.g. '{scenario_input}'.
            user_prompt_template: (generation targets) User prompt template.
            dialog_template: (generation targets) Dialog formatting template.
            tools: (generation targets) Tool definitions for function calling.

            next_message_params: (custom_endpoint) Nested HTTP config for each
                conversation turn. Required keys: 'url', 'method'. Optional:
                'headers', 'body', 'status_code', 'response_message_path',
                'response_session_id_path', 'response_tool_calls_path'.
                All response path values MUST use dot-path notation starting with
                'response.' — e.g., 'response.message', 'response.choices[0].message.content',
                'response.choices[0].tool_calls'. Never use bare property names.
                For SSE/streaming endpoints, include a 'streaming' object with:
                  - 'stop': array of stop conditions (OR semantics — any match ends
                    the stream). Each has 'value' (required) and optional 'path'
                    (dot-path into JSON chunk). Without 'path', matches raw SSE data.
                  - 'select': array of select conditions (AND semantics — all must
                    match for a chunk's content to be extracted). Each requires
                    'path' and 'value'.
                When streaming, set response_message_path to the chunk field
                (e.g., 'response.choices[0].delta.content').
            start_session_params: (custom_endpoint, optional) Nested HTTP config to
                initialise a session. Required key: 'url'. Optional: 'method',
                'headers', 'body', 'status_code', 'response_session_id_path'
                (dot-path starting with 'response.', e.g. 'response.id'),
                'response_message_path'. Supports 'streaming' object (same
                structure as next_message_params.streaming).
            end_session_params: (custom_endpoint, optional) Nested HTTP config to
                close a session after the last turn.
            auth_params: (custom_endpoint, optional) Token-based authorization config.
                Required keys when provided: 'url', 'method', 'response_access_token_path'
                (dot-path starting with 'response.', e.g. 'response.access_token').
                Optional: 'headers', 'body', 'status_code'.
            sensitive_fields: (custom_endpoint, optional) List of dot-path strings for
                secret fields within auth_params (e.g., 'auth_params.body.client_id').
                The MCP auto-generates entries for top-level auth_params keys; use this
                for deeper paths. To remove auth from an existing target, call
                create_or_update_target again without auth_params.
            max_parallel_requests: (custom_endpoint, twilio) Concurrency limit. This is
                the same setting the Okareo web UI labels "max concurrency".

            edge_type: (voice targets) Voice provider — 'openai', 'deepgram',
                or 'twilio'.
            model: (voice openai/deepgram) Model identifier.
            output_voice: (voice openai/deepgram) Voice identifier.
            instructions: (voice openai/deepgram) Voice interaction instructions.

            to_phone_number: (voice twilio) Destination phone number (required).
            account_sid: (voice twilio, custom only) Twilio account SID. If provided,
                auth_token and from_phone_number are also required (all-or-nothing).
                Omit for generic Twilio targets using Okareo's managed integration.
            auth_token: (voice twilio, custom only) Twilio auth token. Required with
                account_sid and from_phone_number.
            from_phone_number: (voice twilio, custom only) Caller phone number. Required
                with account_sid and auth_token.
        """
        from src.target_redaction import find_sentinel_paths

        # --- Redaction-sentinel preflight (spec 023-tool-fixes FR-010, SC-004) ---
        # Values pasted unchanged from get_target carry "***REDACTED***" at
        # paths the user must replace before send. Reject early; never forward
        # the sentinel to the backend.
        _user_payload = {
            k: v for k, v in {
                "next_message_params": next_message_params,
                "start_session_params": start_session_params,
                "end_session_params": end_session_params,
                "auth_params": auth_params,
                "sensitive_fields": sensitive_fields,
                "max_parallel_requests": max_parallel_requests,
                "model_id": model_id,
                "system_prompt_template": system_prompt_template,
                "user_prompt_template": user_prompt_template,
                "dialog_template": dialog_template,
                "tools": tools,
                "model": model,
                "output_voice": output_voice,
                "instructions": instructions,
                "edge_type": edge_type,
                "to_phone_number": to_phone_number,
                "account_sid": account_sid,
                "auth_token": auth_token,
                "from_phone_number": from_phone_number,
            }.items()
            if v is not None
        }
        _sentinel_paths = find_sentinel_paths(_user_payload)
        if _sentinel_paths:
            return json.dumps({
                "error": (
                    "Redaction sentinel still present at: "
                    + ", ".join(_sentinel_paths)
                    + ". Replace these values with real secrets before "
                    "calling create_or_update_target."
                ),
                "sentinel_paths": _sentinel_paths,
            })

        # --- Streaming validation: only allowed on custom_endpoint ---
        has_streaming = (
            (next_message_params and isinstance(next_message_params, dict) and "streaming" in next_message_params)
            or (start_session_params and isinstance(start_session_params, dict) and "streaming" in start_session_params)
        )
        if has_streaming and type != "custom_endpoint":
            return json.dumps({
                "error": "streaming configuration is only supported for custom_endpoint targets.",
            })

        # --- Per-type validation (FR-017) ---
        if type not in ("generation", "custom_endpoint", "voice"):
            return json.dumps({
                "error": "type must be 'generation', 'custom_endpoint', or 'voice'.",
            })

        if type == "generation":
            if not model_id:
                return json.dumps({
                    "error": "model_id is required for generation targets.",
                })

        elif type == "custom_endpoint":
            if not next_message_params or not isinstance(next_message_params, dict):
                return json.dumps({
                    "error": "next_message_params with 'url' and 'method' is required for custom_endpoint targets.",
                })
            if "url" not in next_message_params or "method" not in next_message_params:
                return json.dumps({
                    "error": "next_message_params with 'url' and 'method' is required for custom_endpoint targets.",
                })
            # auth_params validation (FR-017)
            if auth_params is not None:
                if not isinstance(auth_params, dict):
                    return json.dumps({
                        "error": "auth_params must be a JSON object.",
                    })
                required_auth_keys = ["url", "method", "response_access_token_path"]
                missing = [k for k in required_auth_keys if not auth_params.get(k)]
                if missing:
                    return json.dumps({
                        "error": (
                            "auth_params requires 'url', 'method', and "
                            f"'response_access_token_path'. Missing: {missing}"
                        ),
                    })

        elif type == "voice":
            if not edge_type:
                return json.dumps({
                    "error": "edge_type is required for voice targets.",
                })
            if edge_type in ("openai", "deepgram"):
                if not model or not output_voice:
                    return json.dumps({
                        "error": "model and output_voice are required for openai/deepgram voice targets.",
                    })
            elif edge_type == "twilio":
                if not to_phone_number:
                    return json.dumps({
                        "error": "to_phone_number is required for twilio voice targets.",
                    })
                if max_parallel_requests is None or max_parallel_requests < 1:
                    return json.dumps({
                        "error": "max_parallel_requests (integer >= 1) is required for twilio voice targets.",
                    })
                # Credential triple: all-or-nothing
                cred_values = [account_sid, auth_token, from_phone_number]
                cred_provided = [bool(v) for v in cred_values]
                if any(cred_provided) and not all(cred_provided):
                    return json.dumps({
                        "error": "Custom Twilio requires account_sid, auth_token, and from_phone_number together.",
                    })
            else:
                return json.dumps({
                    "error": "edge_type must be 'openai', 'deepgram', or 'twilio'.",
                })

        # --- Build target implementation ---
        from okareo.model_under_test import (
            CustomEndpointTarget,
            DeepgramVoiceTarget,
            EndSessionConfig,
            GenerationModel,
            OpenAIVoiceTarget,
            SessionConfig,
            Target,
            TurnConfig,
            TwilioVoiceTarget,
        )

        if type == "generation":
            gen_kwargs = dict(
                model_id=model_id,
                temperature=temperature,
            )
            if system_prompt_template is not None:
                gen_kwargs["system_prompt_template"] = system_prompt_template
            if user_prompt_template is not None:
                gen_kwargs["user_prompt_template"] = user_prompt_template
            if dialog_template is not None:
                gen_kwargs["dialog_template"] = dialog_template
            if tools is not None:
                gen_kwargs["tools"] = tools
            target_impl = GenerationModel(**gen_kwargs)

        elif type == "custom_endpoint":
            nmp = next_message_params
            # Auto-convert dict bodies to JSON strings
            nmp_body = nmp.get("body")
            if isinstance(nmp_body, dict):
                nmp_body = json.dumps(nmp_body)

            # Build streaming config for next_message_params
            nmp_streaming = _build_streaming_config(nmp.get("streaming"))
            if isinstance(nmp_streaming, str):
                return json.dumps({"error": f"next_message_params.{nmp_streaming}"})

            turn_config = TurnConfig(
                url=nmp["url"],
                method=nmp["method"],
                headers=nmp.get("headers"),
                body=nmp_body or "{}",
                status_code=nmp.get("status_code"),
                response_message_path=nmp.get("response_message_path", ""),
                response_session_id_path=nmp.get("response_session_id_path", ""),
                response_tool_calls_path=nmp.get("response_tool_calls_path", ""),
                streaming=nmp_streaming,
            )

            session_config = None
            if start_session_params and isinstance(start_session_params, dict):
                ssp = start_session_params
                ssp_body = ssp.get("body")
                if isinstance(ssp_body, dict):
                    ssp_body = json.dumps(ssp_body)

                # Build streaming config for start_session_params
                ssp_streaming = _build_streaming_config(ssp.get("streaming"))
                if isinstance(ssp_streaming, str):
                    return json.dumps({"error": f"start_session_params.{ssp_streaming}"})

                session_config = SessionConfig(
                    url=ssp["url"],
                    method=ssp.get("method", "POST"),
                    headers=ssp.get("headers"),
                    body=ssp_body or "{}",
                    status_code=ssp.get("status_code"),
                    response_session_id_path=ssp.get("response_session_id_path", ""),
                    response_message_path=ssp.get("response_message_path", ""),
                    streaming=ssp_streaming,
                )

            end_config = None
            if end_session_params and isinstance(end_session_params, dict):
                esp = end_session_params
                esp_body = esp.get("body")
                if isinstance(esp_body, dict):
                    esp_body = json.dumps(esp_body)
                end_config = EndSessionConfig(
                    url=esp["url"],
                    method=esp.get("method", "POST"),
                    headers=esp.get("headers"),
                    body=esp_body or "{}",
                    status_code=esp.get("status_code"),
                    response_session_id_path=esp.get("response_session_id_path", ""),
                )

            # Build auth config if provided (native SDK AuthConfig)
            auth_config = None
            if auth_params:
                auth_config = _build_auth_config(auth_params)

            target_impl = CustomEndpointTarget(
                start_session=session_config,
                next_turn=turn_config,
                end_session=end_config,
                auth=auth_config,
                max_parallel_requests=max_parallel_requests,
            )

        elif type == "voice":
            if edge_type == "openai":
                target_impl = OpenAIVoiceTarget(
                    model=model,
                    output_voice=output_voice,
                    instructions=instructions or "Be brief and helpful.",
                )
            elif edge_type == "deepgram":
                target_impl = DeepgramVoiceTarget(
                    model=model,
                    output_voice=output_voice,
                    instructions=instructions or "Be brief and helpful.",
                )
            elif edge_type == "twilio":
                target_impl = TwilioVoiceTarget(
                    to_phone_number=to_phone_number,
                    account_sid=account_sid or "",
                    auth_token=auth_token or "",
                    from_phone_number=from_phone_number or "",
                    max_parallel_requests=max_parallel_requests,
                )

        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        try:
            target = Target(name=name, target=target_impl)
            create_kwargs = {}
            if type == "voice" and edge_type == "twilio":
                create_kwargs["sensitive_fields"] = TWILIO_SENSITIVE_FIELDS
            elif type == "custom_endpoint" and auth_params:
                create_kwargs["sensitive_fields"] = _build_custom_endpoint_sensitive_fields(
                    auth_params, sensitive_fields
                )
            result = okareo.create_or_update_target(target, **create_kwargs)
        except Exception as e:
            return format_tool_error(e)

        response = {
            "target_id": _get_attr(result, "id", ""),
            "name": _get_attr(result, "name", name),
            "type": type,
            "created": True,
            "message": f"Target '{name}' saved.",
        }
        if type == "voice" and edge_type:
            response["edge_type"] = edge_type
        if type == "custom_endpoint" and auth_params:
            all_sensitive = create_kwargs.get("sensitive_fields", [])
            response["has_auth"] = True
            response["sensitive_fields_count"] = len(all_sensitive)
        return json.dumps(response, default=str)

    @mcp.tool(
        title="Get Target",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def get_target(name: str) -> str:
        """Check the current configuration of a test target.

        Retrieves a Target by name. Works for all target types (Generation,
        Custom Endpoint, and Voice).

        For **custom_endpoint** Targets, the response is a flat envelope whose
        keys mirror the kwargs accepted by `create_or_update_target`, so a
        copilot can read the result, swap in a new name + secrets, and feed it
        back to create to clone the Target. Fields the backend keeps secret
        (those listed in `sensitive_fields`) appear with the literal value
        `"***REDACTED***"` — these MUST be replaced with real values before
        calling `create_or_update_target`, which rejects payloads still
        containing the sentinel.

        The `max_parallel_requests` field on custom_endpoint Targets is the
        same setting the Okareo web UI labels "max concurrency".

        For generation and voice Targets, the response shape is unchanged
        (kept stable for existing callers).

        Args:
            name: Name of the target to retrieve.
        """
        from okareo_api_client.api.default import (
            get_all_models_under_test_v0_models_under_test_get,
        )
        from okareo_api_client.errors import UnexpectedStatus

        from src.target_redaction import apply_redaction

        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        try:
            muts = get_all_models_under_test_v0_models_under_test_get.sync(
                client=okareo.client,
                project_id=project_id,
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
                "error": (
                    f"Target '{name}' not found. "
                    "Use create_or_update_target to create it first."
                ),
            })

        # Find by name
        for m in muts:
            if isinstance(m, dict):
                mut_name = m.get("name", "")
            else:
                mut_name = _get_attr(m, "name", "")
            if mut_name == name:
                if isinstance(m, dict):
                    target_id = m.get("id", "")
                    models_dict = m.get("models", {})
                    mut_sensitive_fields = m.get("sensitive_fields") or []
                    mut_tags = m.get("tags") or []
                else:
                    target_id = _get_attr(m, "id", "")
                    models_dict = _serialize_value(_get_attr(m, "models")) or {}
                    mut_sensitive_fields = (
                        _get_attr(m, "sensitive_fields", None) or []
                    )
                    mut_tags = _get_attr(m, "tags", None) or []

                # Determine target type and extract config
                target_type = None
                target_config = {}
                if isinstance(models_dict, dict):
                    for ttype in ("voice", "custom_endpoint", "generation"):
                        if ttype in models_dict:
                            target_type = ttype
                            raw = models_dict[ttype]
                            target_config = _serialize_value(raw) if raw else {}
                            break

                if target_type == "custom_endpoint":
                    # Build a flat envelope whose keys mirror
                    # create_or_update_target kwargs (FR-001, FR-011).
                    sensitive_fields = list(
                        mut_sensitive_fields
                        or target_config.get("sensitive_fields")
                        or []
                    )
                    response: dict = {
                        "target_id": target_id,
                        "name": mut_name,
                        "type": target_type,
                    }
                    if mut_tags:
                        response["tags"] = list(mut_tags)
                    # Flatten known custom_endpoint kwargs from target_config.
                    # Omit keys whose value is missing / empty so callers
                    # don't accidentally introduce, e.g., an auth flow that
                    # wasn't there (FR-002, FR-008 edge case).
                    for key in (
                        "next_message_params",
                        "start_session_params",
                        "end_session_params",
                        "auth_params",
                        "max_parallel_requests",
                    ):
                        value = target_config.get(key)
                        if value not in (None, {}, ""):
                            response[key] = value
                    if sensitive_fields:
                        response["sensitive_fields"] = sensitive_fields
                    # Substitute REDACTION_SENTINEL at every sensitive path.
                    response = apply_redaction(response, sensitive_fields)
                    return json.dumps(response, default=str)

                # Generation / voice: response shape unchanged (FR-013).
                return json.dumps({
                    "target_id": target_id,
                    "name": mut_name,
                    "type": target_type,
                    "target": target_config,
                }, default=str)

        return json.dumps({
            "error": (
                f"Target '{name}' not found. "
                "Use create_or_update_target to create it first."
            ),
        })

    @mcp.tool(
        title="List Targets",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def list_targets() -> str:
        """Browse all simulation targets available in this project.

        Returns all simulation targets (voice and custom_endpoint types)
        created via create_or_update_target. Does not include generation models
        registered via register_generation_model — use list_generation_models
        for those.
        """
        from okareo_api_client.api.default import (
            get_all_models_under_test_v0_models_under_test_get,
        )
        from okareo_api_client.errors import UnexpectedStatus

        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        try:
            muts = get_all_models_under_test_v0_models_under_test_get.sync(
                client=okareo.client,
                project_id=project_id,
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
                "targets": [],
                "count": 0,
                "message": "No simulation targets found. Use create_or_update_target to create one.",
            })

        target_type_keys = {"voice", "custom_endpoint"}
        result = []
        for m in muts:
            if isinstance(m, dict):
                models_dict = m.get("models", {})
            else:
                models_dict = _serialize_value(_get_attr(m, "models")) or {}

            if not isinstance(models_dict, dict):
                continue

            matched_types = target_type_keys.intersection(models_dict.keys())
            if not matched_types:
                continue

            target_type = next(iter(matched_types))
            if isinstance(m, dict):
                result.append({
                    "target_id": m.get("id", ""),
                    "name": m.get("name", ""),
                    "type": target_type,
                    "time_created": str(m.get("time_created", "")),
                })
            else:
                result.append({
                    "target_id": _get_attr(m, "id", ""),
                    "name": _get_attr(m, "name", ""),
                    "type": target_type,
                    "time_created": str(_get_attr(m, "time_created", "")),
                })

        if not result:
            return json.dumps({
                "targets": [],
                "count": 0,
                "message": "No simulation targets found. Use create_or_update_target to create one.",
            })

        return json.dumps({"targets": result, "count": len(result)}, default=str)

    @mcp.tool(
        title="Delete Target",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    def delete_target(name: str) -> str:
        """Remove a simulation target and all its related test data.

        Permanently deletes the target and cascades to associated
        test runs and test data points. This cannot be undone.

        Args:
            name: Name of the target to delete.
        """
        from okareo_api_client.api.default import (
            delete_model_under_test_v0_models_under_test_mut_id_delete,
        )

        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        # Look up target by name to get ID
        try:
            mut = okareo.get_model(name=name)
            mut_id = _get_attr(mut, "mut_id", "")
        except Exception:
            return json.dumps({
                "error": f"Target '{name}' not found. "
                "Use list_targets to see available targets.",
            })

        if not mut_id:
            return json.dumps({
                "error": f"Target '{name}' not found. "
                "Use list_targets to see available targets.",
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
                f"Deleted target '{name}'. Associated test runs and "
                "data points were also deleted."
            ),
        })

    @mcp.tool(
        title="Create or Update Driver",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def create_or_update_driver(
        name: str,
        prompt_template: str,
        model_id: Optional[str] = None,
        temperature: float = 0.6,
        voice_instructions: Optional[str] = None,
        voice_profile: Optional[str] = None,
        voice: Optional[str] = None,
        language: Optional[str] = None,
    ) -> str:
        """Define a simulated user persona that will interact with your target.

        Creates or updates a Driver by name (upsert). The prompt_template is the full
        persona definition — include the persona's role, objectives, tactics, and
        behavioural constraints.

        For voice agents, configure how the simulated user speaks with `voice`,
        `voice_profile`, `voice_instructions`, and `language`. Call
        list_driver_voices first to discover valid voice and profile values.

        Args:
            name: Unique name for this driver.
            prompt_template: Full driver persona prompt describing the simulated user's
                role, primary objectives, conversational tactics, and hard rules.
            model_id: Foundation model to power the driver (defaults to project default).
            temperature: Response randomness, default 0.6.
            voice_instructions: Free-text speaking instructions for voice simulations
                (tone, pace, accent). Not validated against the voice catalog.
            voice_profile: Voice profile name for voice simulations. Validated
                against the catalog from list_driver_voices.
            voice: Voice identifier for voice simulations. Validated against the
                catalog from list_driver_voices.
            language: Language for voice simulations (e.g. "en-US", "es-ES").
        """
        if not name:
            return json.dumps({"error": "name is required."})
        if not prompt_template:
            return json.dumps({"error": "prompt_template is required."})

        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        # Validate voice / voice_profile against the catalog so a typo is
        # caught here rather than producing an unusable driver (FR-032). The
        # catalog being unavailable is non-fatal — validation is skipped and
        # the value passes through.
        if voice or voice_profile:
            try:
                catalog = _fetch_voice_catalog(okareo)
            except Exception:
                catalog = None
            if catalog is not None:
                voice_ids = _voice_id_set(catalog["voices"])
                if voice and voice_ids and voice not in voice_ids:
                    return json.dumps({
                        "error": f"Unknown voice '{voice}'.",
                        "available_voices": sorted(voice_ids),
                    })
                profile_names = _profile_name_set(catalog["voice_profiles"])
                if (
                    voice_profile
                    and profile_names
                    and voice_profile not in profile_names
                ):
                    return json.dumps({
                        "error": f"Unknown voice_profile '{voice_profile}'.",
                        "available_voice_profiles": sorted(profile_names),
                    })

        # Auto-prefix bare mustache references with scenario_input.
        prompt_template = _prefix_template_vars(prompt_template)

        # POST /v0/driver directly: the published SDK's Driver dataclass has no
        # `language` field, so it cannot carry voice language through
        # create_or_update_driver (see specs/022-sdk-132-upgrade research R13).
        # The endpoint itself performs the upsert-by-name.
        driver_body: dict = {
            "name": name,
            "prompt_template": prompt_template,
            "temperature": float(temperature),
        }
        if model_id is not None:
            driver_body["model_id"] = model_id
        if voice_instructions is not None:
            driver_body["voice_instructions"] = voice_instructions
        if voice_profile is not None:
            driver_body["voice_profile"] = voice_profile
        if voice is not None:
            driver_body["voice"] = voice
        if language is not None:
            driver_body["language"] = language

        try:
            result = okareo_api_request(okareo, "post", "/v0/driver", json=driver_body)
        except Exception as e:
            return format_tool_error(e)

        result = result if isinstance(result, dict) else {}
        return json.dumps({
            "driver_id": result.get("id", ""),
            "name": result.get("name", name),
            "model_id": result.get("model_id", model_id),
            "temperature": result.get("temperature", temperature),
            "time_created": str(result.get("time_created", "")),
            "voice_instructions": result.get("voice_instructions", voice_instructions),
            "voice_profile": result.get("voice_profile", voice_profile),
            "voice": result.get("voice", voice),
            "language": result.get("language", language),
            "created": True,
            "message": f"Driver '{name}' saved.",
        }, default=str)

    @mcp.tool(
        title="Get Driver",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def get_driver(name: str) -> str:
        """Retrieve a driver persona you've already configured.

        Retrieves a Driver by name, returning its full configuration including the
        persona prompt.

        Args:
            name: Name of the driver to retrieve.
        """
        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        try:
            result = okareo.get_driver_by_name(name)
        except Exception as e:
            error_str = str(e)
            if "not found" in error_str.lower() or "404" in error_str:
                return json.dumps({
                    "error": (
                        f"Driver '{name}' not found. "
                        "Use list_drivers to see available drivers."
                    ),
                })
            return format_tool_error(e)

        if result is None:
            return json.dumps({
                "error": (
                    f"Driver '{name}' not found. "
                    "Use list_drivers to see available drivers."
                ),
            })

        return json.dumps({
            "driver_id": _get_attr(result, "id", ""),
            "name": _get_attr(result, "name", name),
            "prompt_template": _get_attr(result, "prompt_template", ""),
            "model_id": _get_attr(result, "model_id"),
            "temperature": _get_attr(result, "temperature", 0.6),
            "time_created": str(_get_attr(result, "time_created", "")),
            "voice_instructions": _get_attr(result, "voice_instructions"),
            "voice_profile": _get_attr(result, "voice_profile"),
            "voice": _get_attr(result, "voice"),
            "language": _get_attr(result, "language"),
        }, default=str)

    @mcp.tool(
        title="List Drivers",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def list_drivers() -> str:
        """See what driver personas are available in this project.

        Returns all Drivers with their names, IDs, model, and temperature.
        """
        from okareo_api_client.api.default import (
            get_all_drivers_v0_drivers_get,
        )

        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        try:
            drivers = get_all_drivers_v0_drivers_get.sync(
                client=okareo.client,
                project_id=project_id,
                api_key=okareo.api_key,
            )
        except Exception as e:
            return format_tool_error(e)

        if not drivers or isinstance(drivers, Exception):
            return json.dumps({
                "drivers": [],
                "count": 0,
                "message": "No drivers found in project.",
            })

        result = []
        for d in drivers:
            if isinstance(d, dict):
                result.append({
                    "driver_id": d.get("id", ""),
                    "name": d.get("name", ""),
                    "model_id": d.get("model_id"),
                    "temperature": d.get("temperature", 0.6),
                    "time_created": d.get("time_created", ""),
                    "voice_instructions": d.get("voice_instructions"),
                    "voice_profile": d.get("voice_profile"),
                    "voice": d.get("voice"),
                    "language": d.get("language"),
                })
            else:
                result.append({
                    "driver_id": _get_attr(d, "id", ""),
                    "name": _get_attr(d, "name", ""),
                    "model_id": _get_attr(d, "model_id"),
                    "temperature": _get_attr(d, "temperature", 0.6),
                    "time_created": str(_get_attr(d, "time_created", "")),
                    "voice_instructions": _get_attr(d, "voice_instructions"),
                    "voice_profile": _get_attr(d, "voice_profile"),
                    "voice": _get_attr(d, "voice"),
                    "language": _get_attr(d, "language"),
                })

        return json.dumps({"drivers": result, "count": len(result)}, default=str)

    @mcp.tool(
        title="List Driver Voices",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def list_driver_voices() -> str:
        """Discover the voices, voice profiles, and languages available for
        configuring voice-capable drivers.

        Call this before create_or_update_driver when building a voice agent
        simulation, so you can pass valid `voice`, `voice_profile`, and
        `language` values.
        """
        try:
            okareo = get_okareo_client()
            catalog = _fetch_voice_catalog(okareo)
        except Exception as e:
            return format_tool_error(e)

        return json.dumps({
            "voices": catalog["voices"],
            "voice_profiles": catalog["voice_profiles"],
            "languages": catalog["languages"],
            "voice_count": len(catalog["voices"]),
            "voice_profile_count": len(catalog["voice_profiles"]),
        }, default=str)

    @mcp.tool(
        title="Run Simulation",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    def run_simulation(
        name: str,
        scenario_name: Optional[str] = None,
        target_name: Optional[str] = None,
        driver_name: Optional[str] = None,
        checks: Optional[list[str]] = None,
        repeats: int = 1,
        max_turns: int = 5,
        first_turn: str = "target",
        based_on_run_id: Optional[str] = None,
        augmentation: Optional[dict] = None,
        turn_transition_time: Optional[int] = None,
        silence_timeout_ms: Optional[int] = None,
        checks_at_every_turn: Optional[bool] = None,
        stop_check: Optional[dict] = None,
        ctx: Context = None,
    ) -> str:
        """Run a multi-turn conversation evaluation of your AI agent.

        Combines a Target (the system under test), a Driver (the simulated user),
        and a Scenario (the test cases) to generate realistic multi-turn conversations
        and evaluate them with quality checks.

        Returns promptly so the call never times out on long runs. Short runs that
        finish within the buffer window return ``status: "finished"`` with results
        ready; longer runs return ``status: "running"`` with the ``test_run_id``,
        ``app_link``, and an ``estimated_runtime`` — the run continues to completion
        on its own. In both cases, poll get_test_run_results with the returned
        test_run_id for scores, and get_conversation_transcript for transcripts.

        To rerun a previous simulation — keeping its configuration but changing one or
        more parameters — pass based_on_run_id with the original run's ID and supply
        only the values you want to override. If scenario_name or target_name are
        omitted and based_on_run_id is provided, they will be resolved from the
        original run.

        **Voice augmentations** — for voice Targets, the `augmentation` parameter
        applies realistic acoustic and conversational effects. Six top-level keys:
        `cap`, `directed_speech`, `secondary_speaker`, `backchannel`, `barge_in`,
        plus the composable `noise`. **Composition rule**: at most one non-noise
        strategy may be active, optionally combined with `noise`. Augmentations
        apply only to voice Targets — calls against generation or custom_endpoint
        Targets with an augmentation block are rejected. Field-level errors
        (out-of-range probability, missing required field, swapped offsets, unknown
        strategy) are returned by the MCP before any backend call.

        Strategy required / optional fields (numeric ranges in brackets):
          - cap: probability [0.0, 1.0] required. pause_ms [0, 10000] optional.
          - directed_speech: probability [0.0, 1.0] required. lpf_cutoff_hz (>0),
            gain_db [-40.0, 0.0], sample_rate (>0), prompt, reverb_preset optional.
          - secondary_speaker: probability [0.0, 1.0] AND secondary_voice (non-empty
            string) required. inter_speaker_pause_ms [0, 5000], lpf_cutoff_hz (>0),
            gain_db [-40.0, 0.0], sample_rate (>0), secondary_prompt,
            secondary_voice_instructions, secondary_reverb_preset optional.
          - backchannel: utterance (non-empty string) required. probability
            [0.0, 1.0], min_offset_ms (>=0), max_offset_ms (>= min_offset_ms),
            seed optional.
          - barge_in: prompt (non-empty string) required. probability [0.0, 1.0],
            min_offset_ms (>=0), max_offset_ms (>= min_offset_ms), seed optional.
          - noise: noise_profile (non-empty string) AND noise_snr_db (number)
            required. seed optional.

        For copy-paste examples and the full reference, call
        `get_templates(["voice_augmentations"])`.

        Args:
            name: Human-readable name for this simulation run.
            scenario_name: Name of the scenario to use. Required unless based_on_run_id
                is provided and the original run's scenario can be resolved.
            target_name: Name of the target to evaluate. Required unless based_on_run_id
                is provided and the original run's target can be resolved.
            driver_name: Name of the driver persona. If omitted, the project default
                driver is used.
            checks: List of check names to apply (from list_checks).
            repeats: Number of times to run each scenario row, default 1.
            max_turns: Maximum conversation turns per simulation, default 5.
            first_turn: Who speaks first — 'target' or 'driver', default 'target'.
            based_on_run_id: ID of a previous simulation run to reuse parameters from.
                Explicitly supplied values override the original run's parameters.
            augmentation: (voice Targets only) Voice augmentation block. See the
                "Voice augmentations" section above for keys and ranges. An empty
                dict is treated as no augmentation.
            turn_transition_time: Milliseconds of pause between turns. Forwarded to
                the backend as-is; SDK default (1000) is used when omitted.
            silence_timeout_ms: Milliseconds of silence before the simulator
                advances. Forwarded to the backend; backend default is used when
                omitted.
            checks_at_every_turn: When True, checks are evaluated per turn (not
                only at end of run).
            stop_check: Early-stop config: `{"check_name": str, "stop_on": <value>}`.
                The run halts as soon as the named check returns `stop_on`.
        """
        from okareo_api_client.api.default import (
            get_scenario_sets_v0_scenario_sets_get,
        )
        from okareo_api_client.errors import UnexpectedStatus
        from okareo_api_client.models.general_find_payload import GeneralFindPayload

        from src.voice_augmentation import (
            AugmentedSimulation,
            validate_augmentation,
            validate_composition,
        )

        # Treat empty augmentation block as no augmentation (FR-020).
        if isinstance(augmentation, dict) and not augmentation:
            augmentation = None

        # === Augmentation preflight (FR-026: no network calls) ===
        # Composition rule first (FR-018), then per-field validation (FR-021..023).
        if augmentation is not None:
            conflicts = validate_composition(augmentation)
            if conflicts:
                return json.dumps({
                    "error": (
                        f"Unsupported augmentation combination: "
                        f"{', '.join(conflicts)}. "
                        "Only noise + one other strategy is composable."
                    ),
                    "conflicting_strategies": conflicts,
                })
            aug_errors = validate_augmentation(augmentation)
            if aug_errors:
                primary = aug_errors[0]
                payload = {
                    "error": primary["error"],
                    "field": primary.get("field"),
                    "strategy": primary.get("strategy"),
                }
                if "known" in primary:
                    payload["known"] = primary["known"]
                if len(aug_errors) > 1:
                    payload["additional_errors"] = aug_errors[1:]
                return json.dumps(payload)

        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        resolved_scenario_name = scenario_name
        resolved_target_name = target_name
        resolved_driver_name = driver_name

        # If rerunning, fetch original run params as defaults
        if based_on_run_id:
            try:
                payload = GeneralFindPayload(
                    id=based_on_run_id,
                    project_id=project_id,
                    return_model_metrics=True,
                )
                try:
                    runs = find_test_runs(okareo, payload)
                except UnexpectedStatus as ue:
                    runs = json.loads(ue.content) if ue.status_code == 200 else None

                if not runs or isinstance(runs, Exception) or len(runs) == 0:
                    return json.dumps({
                        "error": (
                            f"Simulation run '{based_on_run_id}' not found. "
                            "Use list_test_runs to find available runs."
                        ),
                    })

                original = runs[0]
                original_is_dict = isinstance(original, dict)

                # Resolve scenario name from original run if not overridden
                if not resolved_scenario_name:
                    orig_scenario_id = (
                        original.get("scenario_set_id")
                        if original_is_dict
                        else _get_attr(original, "scenario_set_id")
                    )
                    if orig_scenario_id:
                        try:
                            all_scenarios = get_scenario_sets_v0_scenario_sets_get.sync(
                                client=okareo.client,
                                project_id=project_id,
                                api_key=okareo.api_key,
                            )
                            if all_scenarios and not isinstance(all_scenarios, Exception):
                                for s in all_scenarios:
                                    if _get_attr(s, "scenario_id") == orig_scenario_id:
                                        resolved_scenario_name = _get_attr(s, "name")
                                        break
                        except Exception:
                            pass

                # Resolve target name from original run if not overridden
                if not resolved_target_name:
                    orig_mut_id = (
                        original.get("mut_id")
                        if original_is_dict
                        else _get_attr(original, "mut_id")
                    )
                    if orig_mut_id:
                        try:
                            orig_target = okareo.get_target_by_name(
                                original.get("name", "") if original_is_dict else _get_attr(original, "name", "")
                            )
                            if orig_target:
                                resolved_target_name = _get_attr(orig_target, "name")
                        except Exception:
                            pass

                # Resolve driver name from original run if not overridden
                if not resolved_driver_name:
                    orig_driver_id = (
                        original.get("driver_id")
                        if original_is_dict
                        else _get_attr(original, "driver_id")
                    )
                    if orig_driver_id:
                        try:
                            orig_driver = okareo.get_driver_by_name(orig_driver_id)
                            if orig_driver:
                                resolved_driver_name = _get_attr(orig_driver, "name")
                        except Exception:
                            pass

            except Exception as e:
                return format_tool_error(e)

        # Validate required fields after rerun resolution
        if not resolved_scenario_name:
            return json.dumps({
                "error": (
                    "scenario_name is required. "
                    "Provide it directly or via based_on_run_id."
                ),
            })
        if not resolved_target_name:
            return json.dumps({
                "error": (
                    "target_name is required. "
                    "Provide it directly or via based_on_run_id."
                ),
            })

        # Resolve scenario object
        try:
            all_scenarios = get_scenario_sets_v0_scenario_sets_get.sync(
                client=okareo.client,
                project_id=project_id,
                api_key=okareo.api_key,
            )
        except Exception as e:
            return format_tool_error(e)

        resolved_scenario = None
        if all_scenarios and not isinstance(all_scenarios, Exception):
            for s in all_scenarios:
                if _get_attr(s, "name") == resolved_scenario_name:
                    resolved_scenario = s
                    break

        if resolved_scenario is None:
            return json.dumps({
                "error": (
                    f"Scenario '{resolved_scenario_name}' not found. "
                    "Use list_scenarios to see available scenarios."
                ),
            })

        row_count = _get_attr(resolved_scenario, "scenario_count", 0)
        if row_count == 0:
            return json.dumps({
                "error": (
                    f"Scenario '{resolved_scenario_name}' has zero rows. "
                    "Add rows before running a simulation."
                ),
            })

        # Get provider keys from lifespan context (set at server startup by
        # key_registry.scan_provider_keys()).
        key_registry = {}
        if ctx and hasattr(ctx, "request_context"):
            lifespan_ctx = getattr(ctx.request_context, "lifespan_context", None)
            if lifespan_ctx and isinstance(lifespan_ctx, dict):
                key_registry = dict(lifespan_ctx.get("key_registry", {}))

        # The SDK's Simulation dataclass does not carry `augmentation` or
        # `silence_timeout_ms`. When either is set, bypass okareo.run_simulation
        # and call mut.run_test directly with an AugmentedSimulation that
        # emits the extras through the openapi client's additional_properties
        # (see specs/023-tool-fixes/research.md R1, R4).
        use_augmented_path = (
            augmentation is not None or silence_timeout_ms is not None
        )

        if use_augmented_path:
            # Resolve the target to determine its type for voice preflight.
            try:
                target_model = okareo.get_target_by_name(resolved_target_name)
            except Exception as e:
                return format_tool_error(e, key_registry)
            if target_model is None or not getattr(target_model, "target", None):
                return json.dumps({
                    "error": (
                        f"Target '{resolved_target_name}' not found. "
                        "Use create_or_update_target to create it first."
                    ),
                })
            target_dict = target_model.target
            if not isinstance(target_dict, dict):
                target_dict = _serialize_value(target_dict) or {}
            target_type = target_dict.get("type") if isinstance(target_dict, dict) else None

            # Voice-target preflight (FR-025).
            if augmentation is not None and target_type != "voice":
                return json.dumps({
                    "error": (
                        "Augmentations apply only to voice Targets. "
                        f"Target '{resolved_target_name}' is of type "
                        f"'{target_type}'."
                    ),
                    "target_type": target_type,
                })

            # Resolve driver — mirror okareo.run_simulation's behaviour.
            try:
                from okareo.model_under_test import Driver

                if resolved_driver_name:
                    driver_model = okareo.get_driver_by_name(resolved_driver_name)
                else:
                    driver_model = okareo.create_or_update_driver(
                        Driver(name="default_driver")
                    )
            except Exception as e:
                return format_tool_error(e, key_registry)

            # Build AugmentedSimulation. Pass through only knobs the user set
            # so we keep the SDK's defaults for everything else.
            sim_extras: dict = {}
            if turn_transition_time is not None:
                sim_extras["turn_transition_time"] = turn_transition_time
            if checks_at_every_turn is not None:
                sim_extras["checks_at_every_turn"] = checks_at_every_turn
            if stop_check is not None:
                sim_extras["stop_check"] = stop_check
            sim_params = AugmentedSimulation(
                repeats=repeats,
                max_turns=max_turns,
                first_turn=first_turn,
                augmentation=augmentation,
                silence_timeout_ms=silence_timeout_ms,
                **sim_extras,
            )

            # Build the dummy ModelUnderTest exactly as okareo.run_simulation
            # does internally (see SDK okareo.py:1455-1487).
            try:
                import datetime
                from uuid import UUID
                from okareo.model_under_test import ModelUnderTest
                from okareo_api_client.models.model_under_test_response import (
                    ModelUnderTestResponse,
                )
                from okareo_api_client.models.test_run_type import TestRunType

                target_id_raw = target_model.id
                if isinstance(target_id_raw, str):
                    target_uuid = UUID(target_id_raw)
                elif target_id_raw:
                    target_uuid = target_id_raw
                else:
                    target_uuid = UUID(int=0)
                project_uuid = (
                    UUID(project_id) if isinstance(project_id, str)
                    else (project_id if project_id else UUID(int=0))
                )
                dummy_response = ModelUnderTestResponse(
                    id=target_uuid,
                    project_id=project_uuid,
                    name=target_model.name,
                    tags=[],
                    time_created=datetime.datetime.now().isoformat(),
                    version=1,
                )
                mut = ModelUnderTest(
                    client=okareo.client,
                    api_key=okareo.api_key,
                    mut=dummy_response,
                    models={target_dict["type"]: target_dict},
                )

                def submit_thunk(
                    _mut=mut, _sim_params=sim_params,
                    _driver_model=driver_model, _TestRunType=TestRunType,
                ):
                    return _mut.run_test(
                        scenario=resolved_scenario,
                        name=name,
                        api_key=okareo.api_key,
                        api_keys=key_registry or None,
                        metrics_kwargs=None,
                        test_run_type=_TestRunType.MULTI_TURN,
                        calculate_metrics=True,
                        checks=checks or [],
                        simulation_params=_sim_params,
                        driver_id=(
                            str(_driver_model.id)
                            if _driver_model and getattr(_driver_model, "id", None)
                            else None
                        ),
                    )
            except Exception as e:
                return format_tool_error(e, key_registry)
            is_voice = target_type == "voice"
        else:
            # Non-augmented path: use the SDK's high-level helper. Forward any
            # peer simulation_params knobs (US8 — FR-015) that the SDK's
            # Simulation dataclass natively supports.
            try:
                sim_kwargs = dict(
                    name=name,
                    scenario=resolved_scenario,
                    target=resolved_target_name,
                    driver=resolved_driver_name,
                    checks=checks or [],
                    repeats=repeats,
                    max_turns=max_turns,
                    first_turn=first_turn,
                )
                if turn_transition_time is not None:
                    sim_kwargs["turn_transition_time"] = turn_transition_time
                if checks_at_every_turn is not None:
                    sim_kwargs["checks_at_every_turn"] = checks_at_every_turn
                if stop_check is not None:
                    sim_kwargs["stop_check"] = stop_check
                if key_registry:
                    sim_kwargs["api_keys"] = key_registry
                # submit=False -> okareo.run_simulation uses mut.run_test, which
                # runs the simulation synchronously (blocks until the backend
                # marks the run complete) instead of the async submit_test path.
                sim_kwargs["submit"] = False

                def submit_thunk(_kwargs=sim_kwargs):
                    return okareo.run_simulation(**_kwargs)
            except Exception as e:
                return format_tool_error(e, key_registry)

            # Best-effort target type for the runtime estimate (voice vs text).
            is_voice = False
            try:
                _tobj = okareo.get_target_by_name(resolved_target_name)
                _td = _get_attr(_tobj, "target")
                if not isinstance(_td, dict):
                    _td = _serialize_value(_td) or {}
                is_voice = isinstance(_td, dict) and _td.get("type") == "voice"
            except Exception:
                is_voice = False

        # --- Faux-async handoff (spec 025): run the blocking submission in a
        # background thread, discover the run id, and return within the buffer
        # window so the co-pilot is never blocked. The run finishes on its own.
        scenario_set_id = _get_attr(resolved_scenario, "scenario_id")
        estimate_seconds = _estimate_runtime_seconds(
            row_count, max_turns, repeats, is_voice
        )

        status, payload_obj, run_id, app_link = _buffered_submit(
            submit_thunk,
            okareo=okareo,
            project_id=project_id,
            scenario_set_id=scenario_set_id,
            name=name,
        )
        if status == "failed":
            return format_tool_error(payload_obj, key_registry)

        extra = {
            "scenario": resolved_scenario_name,
            "target": resolved_target_name,
            "rows": row_count,
            "max_turns": max_turns,
            "repeats": repeats,
        }
        if resolved_driver_name:
            extra["driver"] = resolved_driver_name

        response = _build_handoff_response(
            status, payload_obj, run_id, app_link,
            name=name,
            project_id=project_id,
            estimate_seconds=estimate_seconds,
            based_on_run_id=based_on_run_id,
            extra=extra,
        )
        return json.dumps(response, default=str)

    @mcp.tool(
        title="List Simulations",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def list_simulations(
        target_name: Optional[str] = None,
        scenario_name: Optional[str] = None,
        limit: int = 10,
        detail_level: str = "summary",
    ) -> str:
        """List past simulation runs in the project.

        Returns simulation run names, IDs, timestamps, and status, sorted by
        most recent first. Defaults to the 10 most recent runs in summary mode.

        Use detail_level="detailed" to include model_metrics and additional
        fields (limit is capped to 5 in detailed mode to prevent overflow).

        Use get_test_run_results with the returned test_run_id to retrieve
        per-row scores (transcripts excluded by default). Then use
        get_conversation_transcript with a scenario_index to inspect
        individual conversation transcripts.

        Args:
            target_name: Optional filter — only show simulation runs using
                this target.
            scenario_name: Optional filter — only show simulation runs using
                this scenario.
            limit: Maximum number of runs to return, sorted by most recent
                first. Defaults to 10. Set to 0 to return all runs.
            detail_level: "summary" (default) returns compact results without
                model_metrics. "detailed" returns full results with metrics
                (limit capped to 5).
        """
        if detail_level not in ("summary", "detailed"):
            return json.dumps({
                "error": f"Invalid detail_level '{detail_level}'. "
                "Valid values: summary, detailed.",
            })

        if detail_level == "detailed" and (limit > 5 or limit == 0):
            limit = 5
        from okareo_api_client.api.default import (
            get_scenario_sets_v0_scenario_sets_get,
        )
        from okareo_api_client.errors import UnexpectedStatus
        from okareo_api_client.models.general_find_payload import GeneralFindPayload
        from okareo_api_client.models.test_run_type import TestRunType

        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        # Build filter payload — always filter to MULTI_TURN (simulations)
        payload_kwargs: dict = {
            "project_id": project_id,
            "return_model_metrics": True,
            "types": [TestRunType.MULTI_TURN],
        }

        # Resolve target_name to mut_id if provided
        if target_name is not None:
            try:
                mut = okareo.get_model(name=target_name)
                payload_kwargs["mut_id"] = mut.mut_id
            except Exception:
                return json.dumps({
                    "error": f"Target '{target_name}' not found. "
                    "Use list_targets to see available targets.",
                })

        # Resolve scenario_name to scenario_set_id if provided
        if scenario_name is not None:
            try:
                scenarios = get_scenario_sets_v0_scenario_sets_get.sync(
                    client=okareo.client,
                    project_id=project_id,
                    api_key=okareo.api_key,
                )
                scenario_id = None
                if scenarios and not isinstance(scenarios, Exception):
                    for s in scenarios:
                        if _get_attr(s, "name") == scenario_name:
                            scenario_id = _get_attr(s, "scenario_id")
                            break
                if scenario_id is None:
                    return json.dumps({
                        "error": f"Scenario '{scenario_name}' not found. "
                        "Use list_scenarios to see available scenarios.",
                    })
                payload_kwargs["scenario_set_id"] = scenario_id
            except Exception as e:
                return format_tool_error(e)

        # Find simulation runs
        try:
            payload = GeneralFindPayload(**payload_kwargs)
            runs = find_test_runs(okareo, payload)
        except UnexpectedStatus as e:
            if e.status_code == 200:
                runs = json.loads(e.content)
            else:
                return format_tool_error(e)
        except Exception as e:
            return format_tool_error(e)

        if not runs or isinstance(runs, Exception):
            return json.dumps({
                "simulations": [],
                "count": 0,
                "message": "No simulation runs found.",
            })

        # Format results — runs may be raw dicts from the low-level API
        detailed = detail_level == "detailed"
        result = []
        for run in runs:
            if isinstance(run, dict):
                entry = {
                    "id": run.get("id", ""),
                    "name": run.get("name", ""),
                    "status": run.get("status", ""),
                    "test_data_point_count": run.get(
                        "test_data_point_count", 0
                    ),
                    "start_time": run.get("start_time"),
                    "app_link": run.get("app_link", ""),
                }
                if detailed:
                    entry["type"] = run.get("type", "")
                    entry["model_metrics"] = run.get("model_metrics")
                    entry["end_time"] = run.get("end_time")
            else:
                entry = {
                    "id": _get_attr(run, "id", ""),
                    "name": _get_attr(run, "name", ""),
                    "status": _get_attr(run, "status", ""),
                    "test_data_point_count": _get_attr(
                        run, "test_data_point_count", 0
                    ),
                    "start_time": str(
                        _get_attr(run, "start_time", "")
                    ),
                    "app_link": _get_attr(run, "app_link", ""),
                }
                if detailed:
                    entry["type"] = _get_attr(run, "type", "")
                    entry["model_metrics"] = _serialize_value(
                        _get_attr(run, "model_metrics")
                    )
                    entry["end_time"] = str(
                        _get_attr(run, "end_time", "")
                    )
            result.append(entry)

        # Sort by start_time descending (most recent first)
        result.sort(
            key=lambda r: r.get("start_time") or "", reverse=True
        )

        # Apply limit (0 = return all)
        if limit > 0:
            result = result[:limit]

        return json.dumps(
            {"simulations": result, "count": len(result)}, default=str
        )

    return None
