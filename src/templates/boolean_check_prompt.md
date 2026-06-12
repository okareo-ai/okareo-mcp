# Boolean Check Prompt Template

A Boolean Check evaluates whether a target's output meets a specific criterion and returns a pass/fail (true/false) result. Use this when you need a binary quality gate.

## Prompt Structure

```
You are an expert evaluator. Your task is to determine whether the following output meets the specified criterion.

## Criterion
{criterion_description}

## Input
{scenario_input}

## Output to Evaluate
{model_output}

## Instructions
Evaluate the output against the criterion above. Return ONLY "true" if the criterion is met, or "false" if it is not.

Do not explain your reasoning. Return only "true" or "false".
```

## Placeholders

| Placeholder | Description |
|-------------|-------------|
| `{criterion_description}` | The specific quality criterion to check (e.g., "The response is factually accurate") |
| `{scenario_input}` | The original user input from the scenario |
| `{model_output}` | The target's actual response to evaluate |

### All Available Placeholders

The prompt template above uses the most common placeholders. The full set available for model-based checks:

| Placeholder | Description |
|-------------|-------------|
| `{model_output}` | The model output being evaluated. In a multi-turn conversation this is ONLY the final assistant message, not the full conversation |
| `{scenario_input}` | The scenario input / source text |
| `{scenario_result}` | The reference/expected output from the scenario |
| `{model_input}` | What was sent to the model (prompt or messages) |
| `{message_history}` | The full multi-turn conversation — the model_input messages plus the assistant's model_output. Use this when the check must judge the whole conversation |
| `{tool_calls}` | The tool/function calls the model just made |
| `{tools}` | The tool definitions/schema available to the model |
| `{model_output_metadata}` | Metadata attached to the most recent model output |
| `{simulation_message_history}` | Full conversation history reconstructed from trace metadata. Only populated for traced (ingested) conversations; for simulations and evaluations use `{message_history}` |

> The legacy `{generation}` placeholder is deprecated — use `{model_output}` instead.

## Example Criteria

- "The response directly answers the user's question without deflecting"
- "The response does not contain any personally identifiable information"
- "The response is written in a professional, courteous tone"
- "The response acknowledges uncertainty when the answer is not definitive"
- "The response stays within the scope of the original question"

## Expected Output Format

The check must return exactly one of:
- `true` — criterion is met (pass)
- `false` — criterion is not met (fail)

## Usage in Okareo

Register this as a Check in Okareo, then reference it by name in your evaluation runs. Boolean checks appear as pass/fail metrics in your test results.
