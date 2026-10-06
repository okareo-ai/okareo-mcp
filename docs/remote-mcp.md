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

For the fallback, use a key from the API Keys page in [app.okareo.com](https://app.okareo.com). The key belongs to one organization, and every call acts in that organization. Okareo checks the key on the server, so a key revoked in the app stops working within about 30 seconds. A tool that can only do OAuth, such as Slackbot, can still use a key through a [shared connection](#shared-connection-one-okareo-identity-for-a-team).

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

To give a whole Slack workspace one shared Okareo identity instead, so members don't need Okareo accounts, set up a [shared connection](#shared-connection-one-okareo-identity-for-a-team) in a Slack app of your own.

### Shared connection (one Okareo identity for a team)

A shared connection lets a team reach Okareo through one API key, without anyone signing in to Okareo. Use it with any tool that connects to MCP servers through OAuth and lets you type in a client ID and client secret yourself. Everyone the tool connects acts as the key, in the key's organization and with the permissions of the person who created it.

1. In [app.okareo.com](https://app.okareo.com), create an API key in the organization the team should use. Its permissions are those of the person who creates it, so a key created by an admin lets everyone through the connection act as an admin.
2. In your tool's OAuth (or "manual OAuth", "custom OAuth") settings, enter:

   | Field | Value |
   |---|---|
   | MCP server URL | `https://tools.okareo.com/mcp` |
   | Client ID | `okareo-shared-connection` |
   | Client secret | the API key from step 1 |
   | Authorization URL | `https://tools.okareo.com/oauth/shared/authorize` |
   | Token URL | `https://tools.okareo.com/oauth/shared/token` |
   | PKCE | on, if the tool offers it (S256) |
   | Scopes, identity URL | leave empty |

   **If the tool's fields are too short for a whole key** (Slack's are, at about 250 characters), open [`https://tools.okareo.com/oauth/shared/setup`](https://tools.okareo.com/oauth/shared/setup) and paste the key there instead. The page gives you a **Client secret** that is just the key's signature (342 characters) and a **Token URL** that carries the rest of the key (about 160 characters). Use those two values in place of the ones in the table; the other fields are the same. The signature never leaves your browser: only the key's claims are sent to the server, and they end up in the Token URL anyway.

   For this to fit, create the key as a user who belongs **only to this organization**. A key carries the list of every organization its creator belongs to, so a key made by someone in many organizations produces a Token URL too long for Slack. The setup page warns you when that happens.

   The tool supplies its own callback (redirect) URL during sign-in. Okareo accepts any `https` callback, so there's nothing to register with Okareo.
3. Connect from the tool. There is no Okareo sign-in page: the connection completes straight away.

**Revoking access.** Revoke the key in app.okareo.com. Within about 30 seconds every member's calls fail, and the tool can no longer renew the connection. To switch to a new key, put it in the tool as the client secret; each member then reconnects once.

**Things to know:**
- Activity through a shared connection is attributed to the connection, not to individual members.
- `list_tenants` and `switch_tenant` don't apply: the key is tied to one organization.
- Operators: shared connections need `MCP_DCR_SIGNING_KEY` to be set, and rotating it disconnects every shared connection.

#### Worked example: a Slack app for your workspace

Okareo's own "Add to Slack" app uses per-person sign-in. For a shared identity, create your own Slack app:

1. At [api.slack.com/apps](https://api.slack.com/apps), create an app in your workspace.
2. Slack's fields can't hold a whole key, so create the key as a user who belongs only to this organization and open [the setup page](https://tools.okareo.com/oauth/shared/setup). Paste the key and copy the values it shows.
3. In the app's MCP server settings, choose **Manual OAuth** and enter those values: the client ID, the authorization URL and the **Token URL from the setup page**. Turn on **Use PKCE**. **Use HTTP Basic Authentication** can be either on or off. Leave scopes and the identity URL empty.
4. Slack doesn't keep the client secret in the app manifest. Add it in the app's MCP Servers settings or with `slack external-auth add-secret`, using the **Client secret from the setup page** (the key's signature, not the whole key).
5. Install the app in your workspace. Members connect it once in Slackbot; no Okareo sign-in appears.

If the connection fails, see [Reading `[shared-oauth]` lines](#reading-shared-oauth-lines).

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

## Health endpoints — operations

The hosted server answers two health checks. Neither needs a credential or counts against the rate limit.

| Endpoint | 200 means | 503 means |
|---|---|---|
| `GET /health` | The server process is up. | Not used. |
| `GET /health/egress` | This instance can reach the identity provider right now. Body: `{"status":"ok"}`. | It cannot. Body: `{"status":"fail","upstream":…}`, where `upstream` is the status the identity provider answered with (e.g. `403`, a refusal), the error that stopped the request (e.g. `ConnectTimeout`, `ConnectError`, `TimeoutError`), or `frontegg_domain_unset` when the server has no identity provider configured. |

`/health/egress` is an egress readiness probe: every request makes one fresh request to the identity provider, with no caching, and answers within about 8 seconds. Use it as a startup probe, so an instance takes traffic only once it can complete sign-ins and token refreshes, or as an uptime check that alerts when the identity provider refuses or cannot be reached. `HEAD` returns the same status with no body. Because every call goes out to the identity provider, poll it no more often than a probe or an uptime check needs to.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| GitHub Copilot CLI says the server "doesn't support automatic client registration" and asks for OAuth client credentials | Before October 2026 the hosted server named its authorization server with a trailing slash in one discovery document and without it in the other; the CLI compares the two exactly (RFC 8414 §3.3) | Fixed on the server. Add the server again with no headers. The Bearer-header fallback also still works. |
| `save_scenario` rejects an attempt to pass a file path (the hosted tool doesn't list a `file_path` parameter) | The hosted server can't read your local files, so the parameter isn't offered; a client that sends one anyway falls through to the "provide a dataset source" error | For < 2,000 rows, have the copilot read the file and pass its contents as `content`; for ≥ 2,000 rows, upload the file directly to Okareo (web app / SDK / CLI). |
| OAuth browser shows "redirect URI not allowed" | Stale browser session against an older config | Clear browser cookies for `tools.okareo.com` and retry. |
| An API-key request gets 401 `"This Okareo API key is not valid (revoked, expired, or unknown)…"` | Okareo refused the key, or the value isn't an Okareo API key (for example an environment variable that wasn't expanded) | Check the key in app.okareo.com, or create a new one. |
| A request gets 503 `temporarily_unavailable` | The hosted server couldn't reach Okareo to check the key. Your key isn't the problem | Retry after a few seconds (`Retry-After: 5`). If it persists, contact support. |
| `list_tenants` returns `tenant_selection_requires_oauth` | The session authenticated via the API-key bearer path | API keys are single-org; either generate a new API key in the desired org, or switch to the OAuth path. |
| A tool configured with client ID `okareo-shared-connection` fails to connect | The shared-connection sign-in refused one of the tool's requests | Search the server logs for `[shared-oauth]`. There is one line per request; the `reason=` field names the check that failed, and `client_auth=`, `pkce=` and `params=` show what the tool sent. See [Reading `[shared-oauth]` lines](#reading-shared-oauth-lines). |
| Tool calls return a `rate_limited` error | Per-organization throttle (240 tool calls a minute) tripped | Wait for the `retry_after_seconds` window; if persistent, contact support — your traffic profile may warrant a higher limit. |

### Reading `[shared-oauth]` lines

Every request to the shared-connection sign-in (`/oauth/shared/authorize` and `/oauth/shared/token`) writes one line to the server's stderr, whatever the log level:

```text
[shared-oauth] route=token status=400 outcome=error error=invalid_grant reason=redirect_uri_missing grant=authorization_code client_auth=post pkce=no params=client_id,client_secret,code,grant_type ua=Slackbot_1.0
```

The line records what the tool sent (parameter names, how it sent its client secret, whether it used PKCE, whether the key came whole from the client secret or from a setup-page Token URL as `key_source=secret|path`, the callback's host, its User-Agent) and how the server answered. The setup page's packing requests write `route=setup_pack` lines. It never contains the API key, the client secret, codes, tokens, `state` or the callback's path. `reason` is one of:

| Route | `reason` | `error` returned | What the client did |
|---|---|---|---|
| both | `disabled` | `temporarily_unavailable` | Nothing wrong: this server has no stable `MCP_DCR_SIGNING_KEY` |
| authorize | `bad_redirect_uri` | (400 page) | Sent no callback, or one that isn't absolute `https` (or `http` on localhost), or has a `#fragment` |
| authorize | `wrong_client_id` | `unauthorized_client` | Client ID isn't `okareo-shared-connection` |
| authorize | `wrong_response_type` | `unsupported_response_type` | `response_type` isn't `code` |
| authorize | `bad_pkce_method` | `invalid_request` | Sent a PKCE challenge with a method other than `S256` |
| token | `unsupported_grant_type` | `unsupported_grant_type` | `grant_type` isn't `authorization_code` or `refresh_token` |
| token | `client_auth` | `invalid_client` | Sent the client secret both as HTTP Basic and in the body, neither, or a malformed Basic header (see `client_auth=`) |
| token | `wrong_client_id` | `invalid_client` | Client ID isn't `okareo-shared-connection` |
| token | `bad_claims_path` | `invalid_client` | The Token URL's path and the client secret don't form an Okareo API key: one of them was copied incompletely, or the whole key was used as the secret with a setup-page Token URL. Generate both again on the setup page |
| token | `bad_code` | `invalid_grant` | Code forged, expired (over 5 minutes) or from another server's signing key |
| token | `redirect_uri_missing` | `invalid_grant` | Didn't repeat the callback on the token request |
| token | `redirect_uri_mismatch` | `invalid_grant` | Repeated a different callback than it sent to authorize |
| token | `code_reused` | `invalid_grant` | Redeemed the same code twice on this instance |
| token | `pkce_missing` | `invalid_grant` | Sent a PKCE challenge to authorize but no verifier with the token request |
| token | `pkce_mismatch` | `invalid_grant` | Verifier doesn't match the challenge |
| token | `key_invalid` | `invalid_client` (code) / `invalid_grant` (refresh) | okareo-server refused the API key: revoked, expired, unknown or mistyped |
| token | `key_unavailable` | `temporarily_unavailable` | Nothing wrong with the client: okareo-server couldn't be reached |
| token | `bad_refresh_token` | `invalid_grant` | Refresh token forged, expired (90 days unused), or sealed with an older signing key |
| token | `secret_changed` | `invalid_grant` | The client's secret was changed after this connection was made |
