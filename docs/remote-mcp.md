# Remote MCP (hosted)

The Okareo MCP is available as a **hosted, multi-tenant endpoint at `https://tools.okareo.com`**. Connect your AI copilot to it without installing Python, `uv`, `uvx`, or any container. Browser sign-in handles auth on first connect; thereafter the copilot stores the OAuth token itself.

The public docs at [docs.okareo.com/mcp/configuration](https://docs.okareo.com/mcp/configuration) cover every client, including GitHub Copilot and Slackbot.

---

## Prerequisites

- An Okareo account at [app.okareo.com](https://app.okareo.com).
- A copilot that supports MCP servers, or Slackbot in Slack. The remote endpoint has been tested with Claude Code, Claude Desktop, Cursor, GitHub Copilot (in VS Code 1.101 or later, and in the Copilot CLI), and Slackbot.

You do **not** need Python, `uv`, `uvx`, the `okareo-mcp` package, or Docker for the remote endpoint.

---

## Per-copilot configuration

Each section below shows the **recommended** (OAuth) snippet first and the **fallback** (API-key in `Authorization` header) second.

### Claude Code

File: `.mcp.json` in your project root (or `~/.claude.json` for a global config).

**Recommended (OAuth):**

```json
{
  "mcpServers": {
    "okareo": {
      "url": "https://tools.okareo.com/mcp"
    }
  }
}
```

Reload Claude Code. A browser tab opens to Okareo sign-in on first connect; after consent the tools appear in the tool list.

**Fallback (Bearer):**

```json
{
  "mcpServers": {
    "okareo": {
      "url": "https://tools.okareo.com/mcp",
      "headers": {
        "Authorization": "Bearer ${env:OKAREO_API_KEY}"
      }
    }
  }
}
```

Set `OKAREO_API_KEY` in your shell environment. Prefer the env-var form over an inline literal.

### Claude Desktop

File: `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows).

**Recommended (OAuth):**

```json
{
  "mcpServers": {
    "okareo": {
      "url": "https://tools.okareo.com/mcp"
    }
  }
}
```

Restart Claude Desktop. Sign-in flow is browser-based.

### Cursor

File: `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (workspace).

**Recommended (OAuth):**

```json
{
  "mcpServers": {
    "okareo": {
      "url": "https://tools.okareo.com/mcp"
    }
  }
}
```

Restart Cursor and reload the workspace.

**Fallback (Bearer):**

```json
{
  "mcpServers": {
    "okareo": {
      "url": "https://tools.okareo.com/mcp",
      "headers": {
        "Authorization": "Bearer ${env:OKAREO_API_KEY}"
      }
    }
  }
}
```

### GitHub Copilot in VS Code (1.101 or later)

File: per-workspace `.vscode/mcp.json`, or your user `mcp.json` (run **MCP: Open User Configuration**). VS Code reads servers from a `servers` key, not `mcpServers`.

**Recommended (OAuth):**

```json
{
  "servers": {
    "okareo": {
      "type": "http",
      "url": "https://tools.okareo.com/mcp"
    }
  }
}
```

Start the server from the Command Palette: **MCP: List Servers** → **okareo** → **Start Server**, then sign in to Okareo in your browser.

**Fallback (Bearer):**

```json
{
  "servers": {
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

### GitHub Copilot CLI

**Recommended (OAuth):**

```bash
copilot mcp add --transport http okareo https://tools.okareo.com/mcp
```

Start `copilot`. The first time, it opens Okareo's sign-in in your browser.

**Fallback (Bearer):**

```bash
copilot mcp add --transport http --header 'Authorization: Bearer ${OKAREO_API_KEY}' okareo https://tools.okareo.com/mcp
```

Set `OKAREO_API_KEY` in your shell first.

### Slackbot

A Slack admin adds Okareo with **Add to Slack** on the Integrations page in [app.okareo.com](https://app.okareo.com), then each person connects it in Slackbot and signs in. Steps are at [docs.okareo.com/mcp/configuration#slackbot](https://docs.okareo.com/mcp/configuration#slackbot).

---

## Tenants — working across multiple Okareo organizations

If your Okareo account belongs to more than one organization (Frontegg tenant), the remote MCP exposes two tools: one shows which organizations you have and which is active, the other tells you how to change it, which is to reconnect and pick another organization at sign-in.

### `list_tenants`

Show every organization you have access to in this session. The response marks which one is currently active:

```jsonc
{
  "tenants": [
    { "id": "fg-tenant-a1b2", "name": "Acme Corp", "is_current": false },
    { "id": "fg-tenant-c3d4", "name": "Globex",    "is_current": true  }
  ],
  "active_tenant_id":     "fg-tenant-c3d4",
  "active_tenant_source": "jwt_default"
}
```

`active_tenant_source` is always `jwt_default`: the active organization is the one the session's token was issued for.

### `switch_tenant(tenant_id)`

Doesn't change the active organization. It returns how to switch, which is to reconnect and pick another organization at sign-in:

```jsonc
{
  "action": "reauthenticate_to_change_tenant",
  "message": "Changing your active Okareo organization now happens during sign-in. ...",
  "docs_url": "...",
  "current_tenant_id": "fg-tenant-c3d4"
}
```

### Restrictions

- **OAuth path only.** On the Bearer-API-key fallback, both tools return `tenant_selection_requires_oauth` — each API key is already pinned to a single organization. To work in another organization with an API key, use a key made in that organization.
- **Read-only.** Tenant CRUD (creating tenants, inviting users, etc.) remains in the Okareo dashboard.

---

## Working with scenario datasets (JSONL)

On the hosted server, `save_scenario` has **no** `file_path` argument — the
server runs in the cloud and cannot read files on your machine, so the option
isn't offered (the local stdio install still has it). Your copilot feeds the
scenario rows directly instead. How you create a scenario from a `.jsonl` file
depends on its size:

- **Under 2,000 rows** — have your copilot read the file and pass its contents to
  `save_scenario` via the `content` argument (raw JSONL text). The server
  validates it and uploads it for you.
- **2,000 rows or more** — **save the file locally and upload it directly to
  Okareo** through the web app, SDK, or CLI. Do **not** ask the copilot to read a
  large file into the conversation: routing thousands of rows through the
  assistant wastes tokens, and `save_scenario` will reject a `content` payload at
  or above the threshold with this guidance.

This keeps large-dataset creation fast and cheap while the small-dataset path
stays fully conversational.

---

## REPS baseline (`get_reps_baseline`) — operations

The `get_reps_baseline` tool serves the REPS agent-evaluation baseline (the
`reps/` tree of [okareo-ai/okareo-tools](https://github.com/okareo-ai/okareo-tools))
directly from its **latest tagged GitHub Release**. Nothing is vendored into
this server; only tagged, gate-validated release content is ever served — never
the main branch.

**How fresh is it?** The server caches the release in memory and re-checks
GitHub on a TTL. A newly published okareo-tools release is picked up
automatically — **no deploy of this server** — within the configured TTL,
which is capped at 1 hour.

**Staleness.** If GitHub is unreachable at refresh time, the server keeps
serving the last cached release with `stale: true` and
`stale_reason: "github_unreachable"` on every response. If an instance cold-starts
while GitHub is down (no cache yet), the tool returns a `baseline_unavailable`
error until GitHub is reachable.

**Provenance.** Every response carries the release tag it was served from
(e.g. `"tag": "v0.5.1"`); consuming skills record it in evaluation reports.

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `OKAREO_REPS_REFRESH_SECONDS` | `900` (15 min) | TTL between GitHub release checks. Clamped to 60–3600; **3600 (1 hour) is the documented maximum pickup delay** for a new release. |
| `OKAREO_REPS_PINNED_TAG` | unset | **Rollback pin.** Set to a known-good tag (e.g. `v0.5.0`) to serve that release instead of the latest — a config-only change, no build. Responses show `pin: true`. Remove the variable to resume latest-release behavior. A pin naming an unretrievable tag is surfaced loudly (stale fallback with `stale_reason: "pinned_tag_unavailable"`, or `baseline_unavailable` naming the pin) — never silently substituted. |
| `GITHUB_TOKEN` | unset | Optional. Raises the GitHub API rate limit (60/h unauthenticated → 5,000/h). Not required at the default TTL. |

Rollback procedure (Cloud Run): set `OKAREO_REPS_PINNED_TAG=<tag>` on the
service (new revision, config-only), confirm responses carry the pinned tag and
`pin: true`; remove the variable to return to latest.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| GitHub Copilot CLI says the server "doesn't support automatic client registration" and asks for OAuth client credentials | Before October 2026 the hosted server named its authorization server with a trailing slash in one discovery document and without it in the other; the CLI compares the two exactly (RFC 8414 §3.3) | Fixed on the server. Add the server again with no headers. The Bearer-header fallback also still works. |
| `save_scenario` rejects an attempt to pass a file path (the hosted tool doesn't list a `file_path` parameter) | The hosted server can't read your local files, so the parameter isn't offered; a client that sends one anyway falls through to the "provide a dataset source" error | For < 2,000 rows, have the copilot read the file and pass its contents as `content`; for ≥ 2,000 rows, upload the file directly to Okareo (web app / SDK / CLI). |
| OAuth browser shows "redirect URI not allowed" | Stale browser session against an older config | Clear browser cookies for `tools.okareo.com` and retry. |
| `list_tenants` returns `tenant_selection_requires_oauth` | The session authenticated via the API-key bearer path | API keys are single-org; either generate a new API key in the desired org, or switch to the OAuth path. |
| Tool calls return a `rate_limited` error | Per-organization throttle (240 tool calls a minute) tripped | Wait for the `retry_after_seconds` window; if persistent, contact support — your traffic profile may warrant a higher limit. |
