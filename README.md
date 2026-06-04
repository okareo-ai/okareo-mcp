# Okareo MCP Server

The Okareo MCP server exposes Okareo's evaluation capabilities as MCP tools, allowing AI coding assistants to create and manage scenarios, register models, run evaluations, and execute multi-turn simulations directly from your editor.

For detailed documentation, see the [Okareo MCP docs](https://docs.okareo.com/docs/mcp/introduction).

## Prerequisites

- An Okareo account at [app.okareo.com](https://app.okareo.com)
- A copilot that supports MCP servers (Claude Code, Cursor, or VS Code)
- Python 3.10–3.12 (only for the local install modes; not needed for remote)

---

## Remote MCP (hosted) — recommended

The fastest way to get started. No local install, no Python, no API key in `.mcp.json`. Browser sign-in handles auth on first connect.

### Recommended (OAuth — Claude Code, Claude Desktop, Cursor, VS Code 1.101+)

Add to your copilot's MCP config (typically `.mcp.json`):

```json
{
  "mcpServers": {
    "okareo": {
      "type": "http",
      "url": "https://tools.okareo.com/mcp"
    }
  }
}
```

Reload the copilot. It will open a browser to Okareo sign-in once; thereafter the copilot stores the token itself. Your `.mcp.json` contains no secrets.

### Fallback (Bearer header — older clients or headless / CI)

For clients that haven't shipped the MCP OAuth flow yet, paste your API key as a bearer header. Prefer the env-var form over an inline literal:

```json
{
  "mcpServers": {
    "okareo": {
      "type": "http",
      "url": "https://tools.okareo.com/mcp",
      "headers": {
        "Authorization": "Bearer ${env:OKAREO_API_KEY}"
      }
    }
  }
}
```

### Working across multiple Okareo organizations

If your Okareo account belongs to more than one organization (tenant), the remote MCP exposes two conversational tools so you don't have to leave the copilot to switch:

- `list_tenants` — show every organization you have access to. Marks which one this session is currently operating against.
- `switch_tenant(tenant_id)` — change the active organization for subsequent tool calls in this session.

The selection is session-scoped: it lasts as long as the MCP connection stays alive. If you close and reopen the copilot, ask the LLM to re-switch (or just re-issue `switch_tenant` from the resumed conversation). Tenant selection requires the OAuth path; on the API-key fallback path both tools return `tenant_selection_requires_oauth` because each API key is already pinned to one organization.

---

## Local install (alternative)

Run the MCP server on your own machine. Useful for offline / airgapped environments and for development.

> Prefer the **[Remote MCP](#remote-mcp-hosted--recommended)** section above unless you have a specific reason to install locally (airgapped, custom build, development on this repo). The remote endpoint requires no install and stays current automatically.

For multi-org users, the remote endpoint also exposes [tenant management tools](docs/remote-mcp.md#tenants--working-across-multiple-okareo-organizations) (`list_tenants`, `switch_tenant`).

### Step 1: Set Your API Key

```bash
export OKAREO_API_KEY="your-api-key"
```

Add this to your `~/.zshrc` or `~/.bash_profile` for persistence.

### Step 2: Configure Your Copilot

#### Claude Code

Add to `.mcp.json`:

```json
{
  "mcpServers": {
    "okareo": {
      "command": "uvx",
      "args": ["okareo-mcp"],
      "env": {
        "OKAREO_API_KEY": "${OKAREO_API_KEY}"
      }
    }
  }
}
```

No pre-install needed — `uvx` handles it automatically.

#### Cursor

Add to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "okareo": {
      "command": "uvx",
      "args": ["okareo-mcp"]
    }
  }
}
```

Cursor inherits `OKAREO_API_KEY` from your shell environment.

#### Alternative: pip install

If you don't have `uv` installed:

```bash
pip install okareo-mcp
```

Then use `"command": "okareo-mcp"` instead of `"command": "uvx"` with `"args": ["okareo-mcp"]`.

---

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `OKAREO_API_KEY` | *(required)* | Your Okareo API key |
| `OKAREO_BASE_URL` | `https://api.okareo.com` | Override for on-prem Okareo backend |
| `TRANSPORT` | `stdio` | Transport: `stdio` (local) or `sse` (Docker) |
| `PORT` | `8000` | Port for SSE transport |

---

## Available Tools

### Scenarios

| Tool | Description |
|------|-------------|
| `save_scenario` | Save a named scenario from rows of input/result data (idempotent) |
| `list_scenarios` | List all scenarios in the project with names, IDs, and row counts |
| `get_scenario` | Retrieve a scenario's metadata and all data rows by name or ID |
| `create_scenario_version` | Create a new version of an existing scenario with updated data |
| `preview_delete_scenario` | Preview what will be deleted before removing a scenario |
| `delete_scenario` | Permanently delete a scenario and all related test data |

### Generation Models

| Tool | Description |
|------|-------------|
| `list_available_llms` | Browse available LLMs from the Okareo registry |
| `register_generation_model` | Register a generation model for testing by selecting an LLM from the registry |
| `list_generation_models` | List all registered generation models in the project |
| `get_generation_model` | Read detailed information about a registered generation model |
| `update_generation_model` | Change the LLM a registered generation model points to |
| `delete_generation_model` | Remove a registered generation model and all its related test data |

### Tests & Checks

| Tool | Description |
|------|-------------|
| `list_checks` | List available quality checks (built-in and custom) for evaluating model outputs |
| `run_test` | Run a quality test that evaluates a model against a scenario using specified checks |
| `list_test_runs` | List past test runs with optional filters (model, scenario, simulation-only) |
| `get_test_run_results` | Load detailed per-row results of a test run or simulation by ID or name |
| `get_conversation_transcript` | Retrieve the full conversation transcript for a single data point |
| `reevaluate_test_run` | Re-score a completed test run against a (possibly different) set of checks |
| `create_or_update_check` | Create or update a quality check by name — model-based, code-based, or audio (upsert) |
| `generate_check` | Generate a check from a natural-language description, then save it |
| `get_check` | Retrieve a check's full configuration, including its prompt template or code |
| `delete_check` | Permanently delete a check by name |

### Simulations (Multi-Turn)

| Tool | Description |
|------|-------------|
| `create_or_update_target` | Create or update a Target — generation model, custom endpoint, or voice (OpenAI, Deepgram, Twilio) |
| `get_target` | Retrieve a Target's configuration by name (all types) |
| `list_targets` | List all simulation targets (voice and custom_endpoint) in the project |
| `delete_target` | Remove a simulation target and all its related test data |
| `create_or_update_driver` | Define a simulated user persona that will interact with your target |
| `get_driver` | Retrieve a Driver's full configuration including the persona prompt |
| `list_drivers` | List all Driver personas in the project |
| `list_driver_voices` | Discover the voices, voice profiles, and languages available for voice drivers |
| `run_simulation` | Run a multi-turn conversation evaluation (or rerun a previous one with overrides) |
| `list_simulations` | List past simulation runs with optional filters (target, scenario, limit) |

### Voice Monitoring

| Tool | Description |
|------|-------------|
| `ingest_conversations` | Submit completed voice conversations to Okareo for monitoring |
| `connect_voice_integration` | Connect a voice provider so its traffic flows into Okareo monitoring |
| `list_voice_integrations` | List the voice provider integrations in your project |
| `get_voice_integration` | Retrieve a voice provider integration by ID, including its status |
| `update_voice_integration` | Update a voice provider integration's metadata |
| `rotate_voice_integration_secret` | Rotate a voice provider integration's secrets |
| `delete_voice_integration` | Delete a voice provider integration by ID |
| `get_voice_webhook_url` | Get the inbound webhook endpoint for a voice provider |

### Analytics & Dashboards

| Tool | Description |
|------|-------------|
| `query_analytics` | Query Okareo's product analytics to understand evaluation trends |
| `list_dashboards` | List the analytics dashboards in your project |
| `get_dashboard` | Retrieve a dashboard's full configuration by name |
| `save_dashboard` | Create or update an analytics dashboard by name (upsert) |
| `reorder_dashboards` | Set the display order of dashboards |
| `delete_dashboard` | Delete a dashboard by name |

### Tenant Management (remote MCP only)

| Tool | Description |
|------|-------------|
| `list_tenants` | List every Okareo organization you have access to in this MCP session |
| `switch_tenant` | Change which Okareo organization subsequent tool calls operate against |

### Documentation & Templates

| Tool | Description |
|------|-------------|
| `get_docs` | Query the Okareo documentation system for conceptual or user-legible explanations |
| `get_templates` | Retrieve prompt templates for common Okareo patterns (works offline) |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `okareo-mcp: command not found` | Not installed or not in PATH | Run `pip install -e .` (dev) or use `uvx okareo-mcp` (user) |
| Server exits with API key error | `OKAREO_API_KEY` not set | Export it: `export OKAREO_API_KEY="..."` |
| `pip install` fails on Python 3.13+ | Okareo SDK requires Python <3.13 | Use Python 3.10–3.12 |
| Copilot can't connect (Docker) | Wrong URL | Ensure URL ends with `/sse` and port matches |
| Cursor doesn't pick up API key | Cursor launched from Dock, not terminal | Launch Cursor from terminal: `cursor .` |

---

## Contributing

This repository is a curated public mirror; the canonical source is maintained by Okareo. We welcome issues and consider community pull requests — see [CONTRIBUTING.md](CONTRIBUTING.md) for how proposed changes are reviewed and ported.

## License & Trademarks

The Okareo MCP server source code is licensed under the [Apache License 2.0](LICENSE) (see also [NOTICE](NOTICE)).

"Okareo", the Okareo logo, and Okareo product names are trademarks of Okareo, Inc. and are **not** covered by the Apache 2.0 license. See [TRADEMARK.md](TRADEMARK.md) for permitted use.
