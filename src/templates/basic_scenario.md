# Basic Scenario Template

A Scenario in Okareo is a collection of test cases (rows) used to evaluate an AI system. Each row contains an input and an expected result that your Checks will evaluate against.

## Structure

```python
from okareo import Okareo

okareo = Okareo("YOUR_API_KEY")

# Create a scenario from seed data
scenario = okareo.create_scenario_set(
    name="{scenario_name}",
    seed_data=[
        {
            "input": "{user_input_1}",
            "result": "{expected_result_1}"
        },
        {
            "input": "{user_input_2}",
            "result": "{expected_result_2}"
        },
        {
            "input": "{user_input_3}",
            "result": "{expected_result_3}"
        }
    ]
)
```

## Fields

| Field | Type | Description |
|-------|------|-------------|
| `input` | string or JSON object | The user message or prompt to send to the target. Use a JSON object when the scenario is used with a Driver whose prompt contains mustache parameters. |
| `result` | string | The expected or reference output for evaluation |

## Usage with Drivers (JSON input)

When a scenario is used in a simulation with a Driver, the `input` must be a JSON object whose keys match the mustache parameters in the Driver's prompt template. For example, if the Driver prompt contains `{persona_name}`, `{objective_1}`, and `{background_details}`, the scenario input must include those keys:

```python
from okareo import Okareo

okareo = Okareo("YOUR_API_KEY")

# Create a scenario with JSON inputs that match Driver mustache parameters
scenario = okareo.create_scenario_set(
    name="{scenario_name}",
    seed_data=[
        {
            "input": {
                "persona_name": "Alex",
                "persona_description": "a long-time customer frustrated with billing issues",
                "objective_1": "Get a refund for the double charge on the last bill",
                "objective_2": "Understand why the billing error keeps happening",
                "objective_3": "Decide whether to continue the subscription",
                "tactic_1": "Start politely but become more direct if the agent gives generic responses",
                "tactic_2": "Reference specific dates and amounts from billing history",
                "tactic_3": "Ask follow-up questions to verify the agent understands the issue",
                "rule_1": "Never use profanity or threats",
                "rule_2": "Always provide specific details when asked",
                "rule_3": "Do not accept vague promises — ask for concrete next steps",
                "background_details": "3-year customer. Double-charged twice in 4 months. Previously told it was fixed."
            },
            "result": "The agent resolves the billing issue and provides a refund"
        }
    ]
)
```

Every `{parameter}` in the Driver prompt template must have a corresponding key in the scenario `input` object, or it will not be populated during the simulation.

## Usage (simple string input)

For non-simulation evaluations (e.g., `okareo.run_test`), a simple string input is sufficient:

1. Replace `{scenario_name}` with a descriptive name (e.g., "Customer Support FAQ")
2. Replace each `{user_input_N}` with a realistic user query
3. Replace each `{expected_result_N}` with the ideal response
4. Add as many rows as needed for thorough coverage

## Tips

- Include edge cases: empty inputs, very long inputs, adversarial prompts
- Cover the main use cases your AI system should handle
- Write expected results that your Checks can meaningfully compare against
- Start with 5-10 representative rows, then expand based on evaluation results
- When using with a Driver, double-check that every mustache parameter in the Driver prompt has a matching key in the scenario input
