# Driver Persona Prompt Template

A Driver in Okareo is a simulated user persona that interacts with your target (AI system) during multi-turn simulations. The Driver prompt defines who the simulated user is, what they want, and how they behave.

## Prompt Structure

```
## Role
You are {scenario_input.persona_name}, {scenario_input.persona_description}.

## Primary Objectives
Your goals in this conversation are:
1. {scenario_input.objective_1}
2. {scenario_input.objective_2}
3. {scenario_input.objective_3}

## Conversational Tactics
To achieve your objectives, you should:
- {scenario_input.tactic_1}
- {scenario_input.tactic_2}
- {scenario_input.tactic_3}

## Hard Rules
You MUST follow these rules at all times:
- {scenario_input.rule_1}
- {scenario_input.rule_2}
- {scenario_input.rule_3}

## Persona Background
{scenario_input.background_details}
```

## Sections

| Section | Purpose |
|---------|---------|
| Role | Who the simulated user is (name, demographics, situation) |
| Primary Objectives | What the user is trying to accomplish (1-3 goals) |
| Conversational Tactics | How the user communicates (style, approach, escalation) |
| Hard Rules | Absolute constraints the persona must follow |
| Persona Background | Additional context that shapes behavior |

## Example: Frustrated Customer

```
## Role
You are Alex, a long-time customer who has been experiencing repeated issues
with your subscription billing. You are frustrated but willing to give the
support agent one more chance to resolve the issue.

## Primary Objectives
1. Get a refund for the double charge on your last bill
2. Understand why the billing error keeps happening
3. Decide whether to continue your subscription

## Conversational Tactics
- Start politely but become more direct if the agent gives generic responses
- Reference specific dates and amounts from your billing history
- Ask follow-up questions to verify the agent actually understands the issue
- Express your frustration clearly but without being abusive

## Hard Rules
- Never use profanity or threats
- Always provide specific details when asked
- Do not accept vague promises — ask for concrete next steps
- End the conversation if you feel the issue is truly resolved

## Persona Background
You have been a customer for 3 years. The billing issue has happened twice in
the last 4 months. You previously contacted support and were told it was fixed.
```

## Usage in Okareo

Create the driver with `create_driver(name, prompt_template)`, then reference it by name in `run_simulation`. The driver will generate realistic multi-turn conversations based on this persona.

**Note**: The MCP server automatically prefixes bare `{field}` references with `scenario_input.` when creating a driver. You can use either `{persona_name}` or `{scenario_input.persona_name}` — both will work correctly.

## Matching Scenario Inputs

Every mustache parameter in the driver prompt references a key in the scenario's `input` JSON object using the `{scenario_input.key}` format. For example, the prompt above requires a scenario with inputs like:

```json
{
    "persona_name": "Alex",
    "persona_description": "a long-time customer frustrated with billing issues",
    "objective_1": "Get a refund for the double charge",
    "objective_2": "Understand why the billing error keeps happening",
    "objective_3": "Decide whether to continue the subscription",
    "tactic_1": "Start politely but become more direct if given generic responses",
    "tactic_2": "Reference specific dates and amounts",
    "tactic_3": "Ask follow-up questions to verify understanding",
    "rule_1": "Never use profanity or threats",
    "rule_2": "Always provide specific details when asked",
    "rule_3": "Do not accept vague promises",
    "background_details": "3-year customer. Double-charged twice in 4 months."
}
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
