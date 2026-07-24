# Driver Persona Prompt Template

A Driver in Okareo is a simulated user persona that interacts with your target (AI system) during multi-turn simulations. The Driver prompt defines who the simulated user is, what they want, and how they behave.

## Prompt Structure (canonical core sections)

Author ONLY these four sections, in this order — they match how the Okareo platform generates drivers:

```
## Persona

-   **Identity:** Who the simulated user is (the static character).
-   **Mindset:** What they believe and how they approach the conversation.

## Scenario Details

{scenario_input}

## Objectives

1. First goal the driver pursues, written from the driver's goal.
2. Second goal.
3. Stop condition — when the driver considers the task done.

## Soft Tactics

1. How the driver probes when replies are vague.
2. How it escalates.
3. When it stops pushing.
```

| Section | Purpose |
|---------|---------|
| Persona | Who the simulated user is — the static character (identity, mindset) |
| Scenario Details | The scenario reference — `{scenario_input}` (or a specific path such as `{scenario_input.objectives}`), placed **immediately before** Objectives. This is how each scenario row's data reaches the conversation. Keep the braces exactly as written; do not fill it in. |
| Objectives | WHAT the driver is trying to accomplish — written from the driver's goal, NOT from scenario variables |
| Soft Tactics | HOW the driver probes, escalates, and wraps up |

**Do NOT author `## Hard Rules`, `## Turn-End Checklist`, or `## Conversation Behavior` sections.** The MCP appends the platform's canonical versions automatically when the driver is saved — the same blocks the Okareo UI appends to generated drivers (stay in character, never offer help, one question at a time, the language rule, scenario-grounded information disclosure, voice-call speech behaviors). Any variant of these sections you include is replaced by the canonical text, and repeated updates never duplicate the blocks.

## Example: Frustrated Customer

```
## Persona

-   **Identity:** You are **Alex**, a long-time customer who has been
    experiencing repeated issues with your subscription billing.
-   **Mindset:** Frustrated but willing to give the support agent one more
    chance to resolve the issue.

## Scenario Details

{scenario_input}

## Objectives

1. Get a refund for the double charge on your last bill.
2. Understand why the billing error keeps happening.
3. Decide whether to continue your subscription.

## Soft Tactics

1. Start politely but become more direct if the agent gives generic responses.
2. Reference specific dates and amounts from your billing history.
3. Ask follow-up questions to verify the agent actually understands the issue.
4. End the conversation once you feel the issue is truly resolved.
```

## Usage in Okareo

Create the driver with `create_or_update_driver(name, prompt_template)`, then reference it by name in `run_simulation`. The driver will generate realistic multi-turn conversations based on this persona.

**Note**: The MCP server automatically prefixes bare `{field}` references with `scenario_input.` when creating a driver. You can use either `{persona_name}` or `{scenario_input.persona_name}` — both will work correctly.

## Matching Scenario Inputs

Every mustache parameter in the driver prompt references a key in the scenario's `input` JSON object using the `{scenario_input.key}` format. Use `{scenario_input}` bare (as in the structure above) to pass each row's entire input; use dotted paths inside Scenario Details when the scenario rows are JSON objects, for example:

```
## Scenario Details

Account: {scenario_input.account_id}
Issue summary: {scenario_input.issue}
```

If a mustache parameter has no matching key in the scenario input, it will not be populated during the simulation. See the `basic_scenario` template for the full scenario creation pattern.

## Substitution Variables

These reserved variables are available when Okareo processes the driver configuration during multi-turn simulations. They are **not** prefixed with `scenario_input.`:

| Variable | Description |
|----------|-------------|
| `{scenario_input}` | The entire original input from the scenario row. Use for one-shot generation targets. |
| `{scenario_result}` | The expected result from the scenario row. |
| `{session_id}` | Unique session identifier for maintaining conversation state in multi-turn simulations. |
| `{scenario_row_run_guid}` | Unique identifier for the current scenario row execution. |
| `{message_history}` | Full conversation history up to the current turn (multi-turn only). |
| `{latest_message}` | The most recent message in the conversation. Use for multi-turn agent targets instead of `{scenario_input}`. |
| `{access_token}` | The Okareo API access token, for targets that need Okareo service authentication. |
