# Score Check Prompt Template

A Score Check evaluates a target's output and returns a numeric score (typically 1-5 or 0-10). Use this when you need granular quality measurement rather than a binary pass/fail.

Two kinds of slot appear below and they are not interchangeable. `<angle>` slots are yours: replace them with your own text before saving. `{brace}` placeholders are Okareo's: leave them exactly as written and Okareo substitutes the row's data at evaluation time.

## Prompt Structure

```
You are an expert evaluator. Your task is to score the following output on a scale of <min_score> to <max_score>.

## Scoring Rubric
<scoring_rubric>

## Input
{scenario_input}

## Output to Evaluate
{model_output}

## Instructions
Score the output based on the rubric above. Return ONLY a single integer between <min_score> and <max_score>.

Do not explain your reasoning. Return only the numeric score.
```

## Placeholders

### Fill these in yourself

Replace each slot with your own text before saving the check. Okareo does not substitute these — a slot left unreplaced reaches the judge as literal text.

| Slot | What to write |
|------|---------------|
| `<min_score>` | Minimum score value (e.g., 1) |
| `<max_score>` | Maximum score value (e.g., 5) |
| `<scoring_rubric>` | Detailed description of what each score level means |

### Runtime placeholders

Okareo substitutes these with the row's data at evaluation time. Leave them exactly as written.

| Placeholder | Description |
|-------------|-------------|
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
| `{user_only_audio}` | The user's audio only, for speaker-scoped audio checks. Audio checks only — empty on text evaluations |

### Rejected placeholders

> Okareo rejects any placeholder that is not listed above. That includes the legacy aliases `{generation}`, `{input}`, `{result}`, `{audio_messages}`, and `{audio_output}` — use `{model_output}`, `{scenario_input}`, `{scenario_result}`, and `{user_only_audio}` instead. A rejected prompt fails on save and fails `calibrate_check`.

## Example Rubric (1-5 Scale)

```
Score 1: Completely irrelevant or harmful response
Score 2: Partially relevant but contains significant errors or omissions
Score 3: Adequate response that addresses the question but lacks depth
Score 4: Good response that is accurate, relevant, and well-structured
Score 5: Excellent response that is comprehensive, accurate, and insightful
```

## Expected Output Format

The check must return a single integer:
- Example: `4`

## Usage in Okareo

Register this as a Check in Okareo, then reference it by name in your evaluation runs. Score checks appear as averaged metrics in your test results, enabling trend tracking across runs.
