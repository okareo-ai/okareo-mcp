"""Voice tools for the Okareo MCP server.

Covers voice conversation monitoring (US3 — ``ingest_conversations``) and voice
provider integrations (US6). These call the Okareo API through
``okareo_api_request`` because the published ``okareo`` 0.0.132 SDK does not
wrap the ``/v0/conversations/ingest`` or ``/v0/voice/integration*`` endpoints
(see ``specs/022-sdk-132-upgrade`` research R2).
"""

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from src.error_handling import format_tool_error
from src.okareo_client import (
    get_okareo_client,
    okareo_api_request,
    resolve_project_id,
)

# Audio-source keys; a conversation needs at least one to be ingestable.
_AUDIO_SOURCE_KEYS = ("transcript", "audio", "recording_url", "recording_bytes_b64")

_VOICE_PROVIDERS = ("retell", "twilio", "vapi", "elevenlabs")


def register_tools(mcp: FastMCP) -> None:
    """Register voice monitoring and integration tools with the FastMCP server."""

    @mcp.tool()
    def ingest_conversations(
        conversations: list[dict],
        project_id: Optional[str] = None,
        mut_id: Optional[str] = None,
    ) -> str:
        """Submit completed voice conversations to Okareo for monitoring.

        Each conversation's turns become evaluable data points and any
        configured monitors run their checks automatically. Use this to feed
        production voice traffic (Retell, Twilio, VAPI, ElevenLabs, or a custom
        source) into Okareo monitoring.

        Conversations are validated individually: valid ones are ingested and
        invalid ones are returned in a "rejected" list — the batch is not
        all-or-nothing.

        Args:
            conversations: List of conversation objects. Each MUST include a
                "call_id" and at least one of: "transcript" (a list of
                {role, content, timestamp_ms} turns), "audio"
                ({"type": "url"|"voice_file_id"|"inline_b64", ...}),
                "recording_url", or "recording_bytes_b64". Optional per
                conversation: "context_token", "metadata", "tags" (tags drive
                monitor/filter-group matching), "diarization", "first_turn".
                When both a transcript and audio are supplied, the transcript
                takes precedence.
            project_id: Okareo project ID. Defaults to the account's project.
            mut_id: Optional model-under-test ID. Omit for pure monitoring —
                data points are then matched to monitors by their tags only.
        """
        if not conversations or not isinstance(conversations, list):
            return json.dumps({"error": "conversations must be a non-empty list."})

        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        if not project_id:
            try:
                project_id = resolve_project_id(okareo)
            except Exception as e:
                return format_tool_error(e)

        # Per-conversation pre-validation. Invalid conversations are reported
        # individually; the valid remainder is still ingested (FR-016a).
        valid: list[dict] = []
        rejected: list[dict] = []
        for idx, conv in enumerate(conversations):
            if not isinstance(conv, dict):
                rejected.append({"index": idx, "reason": "conversation must be an object."})
                continue
            if not conv.get("call_id"):
                rejected.append({"index": idx, "reason": "missing required 'call_id'."})
                continue
            if not any(conv.get(k) for k in _AUDIO_SOURCE_KEYS):
                rejected.append({
                    "index": idx,
                    "reason": (
                        "must supply a 'transcript' or an audio reference "
                        "('audio', 'recording_url', or 'recording_bytes_b64')."
                    ),
                })
                continue
            valid.append(conv)

        if not valid:
            return json.dumps({
                "accepted": 0,
                "rejected": rejected,
                "message": "No valid conversations to ingest.",
            })

        payload: dict = {"project_id": str(project_id), "conversations": valid}
        if mut_id:
            payload["mut_id"] = str(mut_id)

        try:
            result = okareo_api_request(
                okareo, "post", "/v0/conversations/ingest", json=payload
            )
        except Exception as e:
            return format_tool_error(e)

        return json.dumps({
            "accepted": len(valid),
            "rejected": rejected,
            "result": result,
            "message": (
                f"Ingested {len(valid)} conversation(s); "
                f"{len(rejected)} rejected."
            ),
        }, default=str)

    # -- US6: voice provider integrations ---------------------------------
    # The Okareo API identifies integrations by id (there is no name field);
    # tools therefore take an integration_id rather than a name.

    @mcp.tool()
    def connect_voice_integration(
        provider: str,
        webhook_auth_type: str,
        secrets: dict,
        metadata: Optional[dict] = None,
    ) -> str:
        """Connect a voice provider so its traffic flows into Okareo monitoring.

        Creates a provider integration. The returned integration carries an id
        and a public_id — pass the provider + public_id to get_voice_webhook_url
        to obtain the inbound webhook endpoint to paste into the provider's
        console.

        Args:
            provider: Voice platform — one of: retell, twilio, vapi, elevenlabs.
            webhook_auth_type: Webhook authentication type expected by Okareo
                for this provider (provider-specific — see Okareo docs).
            secrets: Provider-specific secret values (opaque pass-through; the
                response never echoes raw secrets, only a summary).
            metadata: Optional free-form metadata object.
        """
        if provider not in _VOICE_PROVIDERS:
            return json.dumps({
                "error": f"provider must be one of: {', '.join(_VOICE_PROVIDERS)}.",
            })
        if not secrets or not isinstance(secrets, dict):
            return json.dumps({"error": "secrets must be a non-empty object."})

        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        body: dict = {
            "project_id": str(project_id),
            "provider": provider,
            "webhook_auth_type": webhook_auth_type,
            "secrets": secrets,
        }
        if metadata is not None:
            body["metadata"] = metadata

        try:
            result = okareo_api_request(
                okareo, "post", "/v0/voice/integration", json=body
            )
        except Exception as e:
            return format_tool_error(e)
        return json.dumps({
            "integration": result,
            "message": f"Voice integration for '{provider}' created.",
        }, default=str)

    @mcp.tool()
    def list_voice_integrations(limit: int = 20) -> str:
        """List the voice provider integrations in your Okareo project.

        Args:
            limit: Maximum number of integrations to return (default 20). Use 0
                for no limit.
        """
        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        try:
            integrations = okareo_api_request(
                okareo, "get", "/v0/voice/integrations",
                params={"project_id": project_id},
            )
        except Exception as e:
            return format_tool_error(e)

        integrations = integrations if isinstance(integrations, list) else []
        total = len(integrations)
        if limit and limit > 0:
            integrations = integrations[:limit]
        return json.dumps({
            "integrations": integrations,
            "count": len(integrations),
            "total": total,
        }, default=str)

    @mcp.tool()
    def get_voice_integration(integration_id: str) -> str:
        """Retrieve a voice provider integration by id, including its status.

        Args:
            integration_id: The integration's id (from list_voice_integrations).
        """
        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)
        try:
            result = okareo_api_request(
                okareo, "get", f"/v0/voice/integration/{integration_id}"
            )
        except Exception as e:
            return format_tool_error(e)
        return json.dumps({"integration": result}, default=str)

    @mcp.tool()
    def update_voice_integration(integration_id: str, metadata: dict) -> str:
        """Update a voice provider integration's metadata.

        Args:
            integration_id: The integration's id.
            metadata: The new metadata object.
        """
        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)
        try:
            result = okareo_api_request(
                okareo, "patch", f"/v0/voice/integration/{integration_id}",
                json={"metadata": metadata},
            )
        except Exception as e:
            return format_tool_error(e)
        return json.dumps({
            "integration": result,
            "message": "Voice integration updated.",
        }, default=str)

    @mcp.tool()
    def rotate_voice_integration_secret(
        integration_id: str, secrets: dict
    ) -> str:
        """Rotate a voice provider integration's secrets.

        Args:
            integration_id: The integration's id.
            secrets: The new provider secret values. The response returns only
                a secret summary, never raw secret values.
        """
        if not secrets or not isinstance(secrets, dict):
            return json.dumps({"error": "secrets must be a non-empty object."})
        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)
        try:
            result = okareo_api_request(
                okareo, "post",
                f"/v0/voice/integration/{integration_id}/rotate",
                json={"secrets": secrets},
            )
        except Exception as e:
            return format_tool_error(e)
        return json.dumps({
            "integration": result,
            "message": "Voice integration secrets rotated.",
        }, default=str)

    @mcp.tool()
    def delete_voice_integration(integration_id: str) -> str:
        """Delete a voice provider integration by id.

        Args:
            integration_id: The integration's id.
        """
        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)
        try:
            okareo_api_request(
                okareo, "delete", f"/v0/voice/integration/{integration_id}"
            )
        except Exception as e:
            return format_tool_error(e)
        return json.dumps({
            "deleted": True,
            "integration_id": integration_id,
            "message": "Voice integration deleted.",
        })

    @mcp.tool()
    def get_voice_webhook_url(provider: str, public_id: Optional[str] = None) -> str:
        """Get the inbound webhook endpoint for a voice provider.

        Paste the returned URL into the provider's console so its call traffic
        reaches Okareo monitoring.

        Args:
            provider: Voice platform — one of: retell, twilio, vapi, elevenlabs.
            public_id: The integration's public_id (from connect_voice_integration
                or get_voice_integration). Required for retell and twilio.
        """
        if provider not in _VOICE_PROVIDERS:
            return json.dumps({
                "error": f"provider must be one of: {', '.join(_VOICE_PROVIDERS)}.",
            })
        # Per-provider inbound webhook paths.
        if provider in ("retell", "twilio"):
            if not public_id:
                return json.dumps({
                    "error": f"public_id is required for the '{provider}' webhook URL.",
                })
            path = f"/v0/voice/{provider}/monitor/{public_id}"
        elif provider == "vapi":
            path = "/v0/voice/vapi/monitor"
        else:  # elevenlabs
            path = "/v0/voice/elevenlabs/webhook"

        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)
        base = str(okareo.client.get_httpx_client().base_url).rstrip("/")
        return json.dumps({
            "provider": provider,
            "webhook_url": f"{base}{path}",
        })
