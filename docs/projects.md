# Projects

Work in Okareo is organized into **projects**. Scenarios, targets, simulations,
evaluations, and dashboards each belong to exactly one project. **Checks and
drivers are shared** across every project in your organization.

If your organization has only the default **Global** project — which is the
case for most organizations — nothing here changes anything for you. You will
never be asked to pick a project, and everything continues to land in Global.

## How the project for an operation is decided

In order, highest priority first:

1. **A project named on the individual call** — the `project` argument, which
   accepts a project **name or id**. Applies to that one call only.
2. **A project pinned to the connection** — see [Pinning](#pinning-a-project-to-a-workspace).
3. **Your organization's only project**, when you have just one.
4. Otherwise the call **stops and asks**. It never guesses, and it never
   quietly falls back to Global.

Every project-scoped response tells you which project it acted on and how that
was decided:

```json
{ "project": { "id": "…", "name": "Billing Agent", "basis": "pin" } }
```

`basis` is `explicit` (named on the call), `pin` (from your connection
configuration), or `default` (your organization's only project).

## Choosing a project in conversation

Just say so — "work in the Billing Agent project". Your copilot calls
`select_project` to validate the name, then passes it on subsequent calls.
Changing project takes effect immediately and **never** requires signing out,
reconnecting, or re-authorizing.

The MCP server is stateless: it does not remember your selection between calls
or between conversations. Your copilot is asked to record your preference and
reuse it next time, but that is up to the copilot. For a guarantee, pin it.

## Pinning a project to a workspace

A pin is the only project selection guaranteed to survive across
conversations, and it applies to everyone using that connection — which makes
it the right tool for "this repository works on this agent".

**Hosted server** — add `project` to the MCP URL:

```jsonc
{ "okareo": { "url": "https://mcp.okareo.com/mcp?project=Billing%20Agent" } }
```

An `X-Okareo-Project` header works too, if your client supports custom headers.

**Local (stdio) install** — set the environment variable:

```jsonc
{
  "okareo": {
    "command": "uvx",
    "args": ["okareo-mcp"],
    "env": { "OKAREO_PROJECT": "Billing Agent" }
  }
}
```

`okareo-mcp-setup` will offer to write this for you, or pass
`--project "Billing Agent"`.

> **`OKAREO_PROJECT` has no effect on the hosted server.** One hosted process
> serves every customer, so an environment variable there would pin all of
> them to the same project. Use the URL parameter instead.

A pinned connection **governs**: it overrides any conversational selection, and
asking to switch projects will tell you to edit the pin rather than pretending
to switch. A per-call `project` argument still works for a one-off look
elsewhere.

## Creating a project

Projects are created in the **Okareo web application**. The MCP is read-only
over projects — it can list them and select among them, but not create,
rename, archive, or delete them. A project you create in the web application
becomes selectable from your copilot within about a minute, with no reconnect.

## Shared checks and drivers

Checks and drivers belong to the organization, not to a project, and behave
identically from every project. They are looked up organization-wide by
design — a driver is not filtered to the project you happen to be in. Creating one from inside a project records
where it was authored but does not make it private to that project — the
response says so explicitly.

There is no other cross-project sharing. A simulation running in project B
cannot use a target or scenario that lives in project A; when you try, the
error names the project the artifact actually belongs to rather than reporting
it as missing.

## Errors you might see

| Code | Meaning | Fix |
|---|---|---|
| `project_not_selected` | You have more than one project and none was chosen | Pick one; the error lists them |
| `project_not_found` | The named project does not exist or you cannot access it | Check the name against `list_projects`; your current project is unchanged |
| `project_misconfigured` | Your **connection pin** names a project that cannot be resolved | Edit the pin in your MCP connection configuration |
| `artifact_not_in_project` | The scenario, target, or model you named is not in the project you are working in | The error lists what *is* available there; use one of those, or switch project |

Okareo never reports which *other* project an artifact belongs to. The MCP
looks only inside the project you are working in — cross-project visibility is
a boundary it deliberately does not read across.
