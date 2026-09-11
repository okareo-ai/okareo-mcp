# Target Validation Check Prompt Template

A Target Validation Check evaluates whether a target's output matches or aligns with the expected result from the scenario. Use this to compare actual target output against reference answers.

Two kinds of slot appear below and they are not interchangeable. `<angle>` slots are yours: replace them with your own text before saving. `{brace}` placeholders are Okareo's: leave them exactly as written and Okareo substitutes the row's data at evaluation time.

## Prompt Structure

```
You are an expert evaluator. Your task is to determine whether the actual output from the AI system aligns with the expected result.

## Expected Result
{scenario_result}

## Actual Output
{model_output}

## Original Input
{scenario_input}

## Evaluation Criteria
<validation_criteria>

## Instructions
Compare the actual output against the expected result using the criteria above. Return ONLY "true" if the output acceptably matches the expected result, or "false" if it does not.

Do not explain your reasoning. Return only "true" or "false".
```

## Placeholders

### Fill these in yourself

Replace each slot with your own text before saving the check. Okareo does not substitute these — a slot left unreplaced reaches the judge as literal text.

| Slot | What to write |
|------|---------------|
| `<validation_criteria>` | How strictly to compare (semantic vs exact) |

### Runtime placeholders

Okareo substitutes these with the row's data at evaluation time. Leave them exactly as written.

| Placeholder | Description |
|-------------|-------------|
| `{scenario_result}` | The reference/expected result from the scenario |
| `{model_output}` | The target's actual response |
| `{scenario_input}` | The original user input |

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
| `{user_only_audio}` | The user's audio only, for speaker-scoped audio checks. Audio checks only — empty on text evaluations |

### Rejected placeholders

> Okareo rejects any placeholder that is not listed above. That includes the legacy aliases `{generation}`, `{input}`, `{result}`, `{audio_messages}`, and `{audio_output}` — use `{model_output}`, `{scenario_input}`, `{scenario_result}`, and `{user_only_audio}` instead. A rejected prompt fails on save and fails `calibrate_check`.

## Example Validation Criteria

### Semantic Match
```
The actual output should convey the same meaning as the expected result.
Minor differences in wording, formatting, or structure are acceptable as
long as the core information is preserved.
```

### Factual Accuracy
```
The actual output must contain the same factual claims as the expected
result. Additional context is acceptable, but no factual contradictions
are allowed.
```

### Key Points Coverage
```
The actual output must address all key points present in the expected
result. It may include additional information, but must not omit any
of the core points.
```

## Expected Output Format

- `true` — output acceptably matches the expected result
- `false` — output does not match

## Usage in Okareo

This check is particularly useful for regression testing — verifying that model updates don't degrade output quality on known-good test cases. Register as a Check and include in evaluation runs alongside your scenarios.

The section below is a DIFFERENT namespace: those variables are substituted into a Target's configuration, not into a check prompt. Do not mix them into the judge prompt above.

## Substitution Variables

These variables are available when Okareo processes your target configuration:

| Variable | Description |
|----------|-------------|
| `{scenario_input}` | The original input from the scenario row. Use for one-shot generation targets. |
| `{session_id}` | Unique session identifier for maintaining conversation state in multi-turn simulations. |
| `{scenario_row_run_guid}` | Unique identifier for the current scenario row execution. |
| `{message_history}` | Full conversation history up to the current turn (multi-turn only). |
| `{latest_message}` | The most recent message in the conversation. Use for multi-turn agent targets instead of `{scenario_input}`. |
| `{access_token}` | The Okareo API access token, for targets that need Okareo service authentication. |

## Target Request Body Examples

### One-shot Generation Target

Use `{scenario_input}` directly when the target processes a single input without conversation state:

```json
{
  "message": {
    "role": "user",
    "content": "{scenario_input}"
  }
}
```

### Multi-turn Agent Target

Use `{latest_message}` when the target is a multi-turn agent. Okareo interprets `{scenario_input}` and generates an appropriate `{latest_message}` for each conversation turn:

```json
{
  "session_id": "{session_id}",
  "message": {
    "role": "user",
    "content": "{latest_message}"
  },
  "streaming": false
}
```

## Multi-turn Configuration

For multi-turn agent targets, always include `max_parallel_requests` in your target configuration with a default value of `1`. This prevents race conditions when multiple conversation turns execute concurrently.

```json
{
  "max_parallel_requests": 1
}
```
