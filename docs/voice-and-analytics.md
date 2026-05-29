# Voice, Analytics & SDK 0.0.132 Capabilities

Capabilities added when the MCP moved to Okareo SDK `0.0.132`. All are reachable
as MCP tools from any connected agent (Claude Code, Cursor, …).

## Check versioning & tags

- `get_check(name, version=…)` — retrieve a specific historical version of a
  check, or the latest when `version` is omitted. The response lists every
  `available_versions`.
- `list_checks(all_versions=true)` — see the full version history of every
  check, each entry annotated with its version number.
- `create_or_update_check(…, tags=[…])` — organize checks with tags.

## Re-evaluating a test run

- `reevaluate_test_run(test_run_id, checks=…)` — re-score a finished test run
  against a set of checks without re-running the original model or simulation.
  The original run is left unchanged. Omit `checks` to reuse the run's own
  checks; pass a list of check names/ids to score against a different set.

## Voice monitoring

- `ingest_conversations(conversations, project_id=…, mut_id=…)` — submit
  completed voice calls for monitoring. Each conversation needs a `call_id` and
  either a `transcript` (list of `{role, content, timestamp_ms}` turns) or an
  audio reference (`audio`, `recording_url`, or `recording_bytes_b64`). Tags on
  a conversation drive monitor/filter-group matching. Omit `mut_id` for pure
  monitoring. Invalid conversations are returned in a `rejected` list — the
  batch is not all-or-nothing.
- Voice provider integrations — `connect_voice_integration`,
  `list_voice_integrations`, `get_voice_integration`, `update_voice_integration`,
  `rotate_voice_integration_secret`, `delete_voice_integration`. Supported
  providers: `retell`, `twilio`, `vapi`, `elevenlabs`.
- `get_voice_webhook_url(provider, public_id=…)` — the inbound webhook endpoint
  to paste into the provider's console so call traffic reaches Okareo.

## Voice-configured simulation drivers

- `list_driver_voices()` — discover the available voices, voice profiles, and
  languages.
- `create_or_update_driver(…, voice=…, voice_profile=…, voice_instructions=…,
  language=…)` — define a simulated user that speaks with a specific voice and
  language. Unknown `voice`/`voice_profile` values are rejected with the valid
  options listed.
- Run a simulation with the voice-configured driver via `run_simulation` — each
  driver carries its own audio characteristics.

## Product analytics & dashboards

- `query_analytics(measures, dimensions=…, include_metadata=true)` — query
  Okareo's analytics to understand evaluation trends. `include_metadata` also
  returns the available cubes/dimensions/measures.
- Dashboards — `list_dashboards`, `get_dashboard`, `save_dashboard` (create or
  update by name), `reorder_dashboards`, `delete_dashboard`.

## Notes

- New capabilities depend on the Okareo account/project having them enabled. A
  tool that hits an unavailable capability returns a clear message pointing to
  app.okareo.com or Okareo support rather than an opaque error.
- These endpoints are not yet wrapped by the published `okareo` SDK; the MCP
  reaches them through the SDK's configured HTTP client. See
  `specs/022-sdk-132-upgrade/research.md` (R2).
