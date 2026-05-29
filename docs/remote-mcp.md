# Remote MCP (hosted)

The Okareo MCP is available as a **hosted, multi-tenant endpoint at `https://tools.okareo.com`**. Connect your AI copilot to it without installing Python, `uv`, `uvx`, or any container. Browser sign-in handles auth on first connect; thereafter the copilot stores the OAuth token itself.

This page is the source of truth that `docs.okareo.com` imports for the public docs.

---

## Prerequisites

- An Okareo account at [app.okareo.com](https://app.okareo.com).
- A copilot that supports MCP servers. The remote endpoint has been tested with Claude Code, Claude Desktop, Cursor, and VS Code (1.101 or later).

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

### VS Code (1.101 or later)

File: `~/.config/Code/User/mcp.json` (Linux/macOS) or per-workspace `.vscode/mcp.json`.

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

Reload the window. The first tool invocation kicks off the OAuth flow.

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

---

## Tenants — working across multiple Okareo organizations

If your Okareo account belongs to more than one organization (Frontegg tenant), the remote MCP exposes two conversational tools so you don't have to leave the copilot to pick the right org.

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

The `active_tenant_source` field tells you whether the active tenant comes from your default sign-in (`jwt_default`) or from a previous `switch_tenant` call in this session (`override`).

### `switch_tenant(tenant_id)`

Change the active organization for subsequent tool calls in the current MCP session:

```jsonc
{
  "active_tenant_id":   "fg-tenant-a1b2",
  "active_tenant_name": "Acme Corp",
  "previous_tenant_id": "fg-tenant-c3d4",
  "resume_hint": "Session-scoped only — re-call switch_tenant('fg-tenant-a1b2') at the start of any resumed conversation."
}
```

After this call, every tenant-scoped tool (`list_scenarios`, `run_test`, `run_simulation`, etc.) in this MCP session operates against `Acme Corp`.

### Resume behavior (important)

The selection is **session-scoped** — it lasts as long as the MCP transport stays connected. If you close and reopen your copilot, the new MCP session starts on whatever Frontegg has as your default tenant (typically your last-used).

For continuity in a resumed chat, ask the LLM to re-issue `switch_tenant` from the conversation history. Well-aligned models that read MCP `instructions` will do this automatically; smaller models may need a nudge ("we were working on Acme — please switch back"). The `active_tenant_id` field on every `list_tenants` response makes it easy to verify which tenant you're actually on.

### Restrictions

- **OAuth path only.** On the Bearer-API-key fallback, both tools return `tenant_selection_requires_oauth` — each API key is already pinned to a single organization.
- **No persistence.** `switch_tenant` does NOT change your Frontegg default tenant. Your next sign-in starts on whatever Frontegg's default is.
- **Read-only.** Tenant CRUD (creating tenants, inviting users, etc.) remains in the Okareo dashboard.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Copilot prompts for "OAuth client_id" | Copilot doesn't yet implement MCP OAuth discovery | Use the fallback Bearer-header config instead. |
| OAuth browser shows "redirect URI not allowed" | Stale browser session against an older config | Clear browser cookies for `tools.okareo.com` and retry. |
| `list_tenants` returns `tenant_selection_requires_oauth` | The session authenticated via the API-key bearer path | API keys are single-org; either generate a new API key in the desired org, or switch to the OAuth path. |
| Tools return data for the wrong organization after resume | LLM didn't re-issue `switch_tenant` on conversation resume | Call `list_tenants` to confirm `active_tenant_id`; then `switch_tenant` to the right org. |
| Tool calls return 429 | Per-credential throttle (60 req/min/org by default) tripped | Wait for the `retry_after` window; if persistent, contact support — your traffic profile may warrant a higher limit. |
