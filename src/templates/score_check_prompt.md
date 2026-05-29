# Score Check Prompt Template

A Score Check evaluates a target's output and returns a numeric score (typically 1-5 or 0-10). Use this when you need granular quality measurement rather than a binary pass/fail.

## Prompt Structure

```
You are an expert evaluator. Your task is to score the following output on a scale of {min_score} to {max_score}.

## Scoring Rubric
{scoring_rubric}

## Input
{scenario_input}

## Output to Evaluate
{model_output}

## Instructions
Score the output based on the rubric above. Return ONLY a single integer between {min_score} and {max_score}.

Do not explain your reasoning. Return only the numeric score.
```

## Placeholders

| Placeholder | Description |
|-------------|-------------|
| `{min_score}` | Minimum score value (e.g., 1) |
| `{max_score}` | Maximum score value (e.g., 5) |
| `{scoring_rubric}` | Detailed description of what each score level means |
| `{scenario_input}` | The original user input from the scenario |
| `{model_output}` | The target's actual response to evaluate |

### All Available Placeholders

The prompt template above uses the most common placeholders. The full set available for model-based checks:

| Placeholder | Description |
|-------------|-------------|
| `{generation}` | The model's generated output |
| `{scenario_input}` | The original scenario input |
| `{scenario_result}` | The expected result from the scenario |
| `{model_input}` | The input sent to the model |
| `{model_output}` | The model's output |
| `{message_history}` | Conversation history |
| `{tool_calls}` | Tool calls made by the model |
| `{tools}` | Tools available to the model |
| `{model_output_metadata}` | Metadata about the model output |
| `{simulation_message_history}` | Simulation conversation history |

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
