# Analysis Check Prompt Template

An Analysis Check evaluates a target's output and returns a qualitative written analysis. Use this when you need detailed feedback rather than a numeric score or binary pass/fail.

## Prompt Structure

```
You are an expert evaluator. Your task is to provide a detailed qualitative analysis of the following output.

## Analysis Criteria
{analysis_criteria}

## Input
{scenario_input}

## Output to Evaluate
{model_output}

## Instructions
Analyze the output based on the criteria above. Provide a structured analysis covering:
1. Strengths of the output
2. Weaknesses or areas for improvement
3. Specific observations related to the criteria
4. Overall assessment

Be specific and reference particular parts of the output in your analysis.
```

## Placeholders

| Placeholder | Description |
|-------------|-------------|
| `{analysis_criteria}` | The specific aspects to analyze (e.g., "tone, accuracy, and completeness") |
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

## Example Criteria

- "Evaluate the response for tone, accuracy, completeness, and helpfulness"
- "Analyze whether the response appropriately handles sensitive topics"
- "Assess the technical accuracy and clarity of the explanation"
- "Review the response for bias, fairness, and inclusivity"
- "Evaluate the creative quality, originality, and engagement of the writing"

## Expected Output Format

The check returns a free-form text analysis. Unlike pass/fail or score checks, there is no fixed format — the evaluator provides detailed qualitative feedback.

## Usage in Okareo

Register this as a Check in Okareo with output_type="analysis", then reference it by name in your evaluation runs. Analysis checks provide rich qualitative feedback alongside any quantitative checks you run.
