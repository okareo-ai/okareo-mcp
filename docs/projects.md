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

The lifecycle tools are the exception: `update_project`, `archive_project`,
`unarchive_project`, and `clone_project` always require you to name the
project they act on. None of them reads your pin or your active project —
renaming or archiving the wrong project silently is the mistake that rule
exists to prevent.

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

## Creating, renaming, and archiving a project

Every project lifecycle action available in the Okareo web application is
available from your copilot:

| Ask for | Tool |
|---|---|
| "make me a project called X" | `create_project` |
| "rename this project" / "tag it X" | `update_project` |
| "retire this project" | `archive_project` |
| "bring back the X project" | `unarchive_project` |
| "give me a copy of this project's scenarios" | `clone_project` |

Creating a project does **not** switch you into it — this server remembers
nothing. The response hands your copilot the new project's id to pass on
subsequent calls; ask it to select the project if you want to keep working
there. A project created in the web application instead becomes selectable
within about a minute, with no reconnect.

Project names are unique per organization, case-insensitively, and archived
projects still hold theirs. A name that is already taken is refused, and
nothing is created or renamed.

### Archiving is not deleting

**Okareo has no project delete** — not in the app, not in the API, not here.
Archiving is the removal, and all it does is hide the project from the project
picker in the Okareo app. An archived project keeps every scenario, run, and
dashboard; `list_projects` still lists it (flagged `is_archived: true`); it can
still be selected by name or id; and every tool still works against it.
`unarchive_project` puts it back in the picker.

The organization's default project cannot be archived.

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
| `project_name_taken` | Another project in your organization already has that name (archived ones still hold theirs) | Choose a different name; nothing was created or renamed |
| `project_name_invalid` | The name has leading or trailing whitespace — Okareo refuses rather than trimming, so the typo stays visible | Retype the name |
| `project_name_required` | A create or rename was attempted with an empty name | Say what the project should be called |
| `project_target_required` | A lifecycle tool was called without naming which project to act on | Name the project; these tools never guess |
| `project_update_empty` | `update_project` was called with neither a name nor tags | Say what should change |
| `project_archive_refused` | You tried to archive your organization's default project | The default project cannot be archived; nothing changed |
| `project_rename_refused` | You tried to rename your organization's default project | The default project cannot be renamed; its tags can still be changed |
| `project_request_refused` | Okareo refused the change for some other reason, quoted in the message | Read the reason; nothing was written, and retrying unchanged will fail the same way |

Okareo never reports which *other* project an artifact belongs to. The MCP
looks only inside the project you are working in — cross-project visibility is
a boundary it deliberately does not read across.
