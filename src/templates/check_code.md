# Code-Based Check Template

A Code Check is a Python class that programmatically evaluates a target's output. Use this when you need deterministic, rule-based evaluation that goes beyond what a prompt-based check can do.

## Class Structure

```python
from okareo.checks import CodeBasedCheck, CheckResponse

class Check(CodeBasedCheck):
    @staticmethod
    def evaluate(model_output: str, scenario_input: str, scenario_result: str) -> CheckResponse:
        """Evaluate the model output.

        Args:
            model_output: The target's actual response.
            scenario_input: The original user input from the scenario.
            scenario_result: The expected/reference result from the scenario.

        Returns:
            CheckResponse with score (bool, int, or float) and explanation.
        """
        score = model_output == scenario_result
        explanation = "Match" if score else "No match"
        return CheckResponse(score=score, explanation=explanation)
```

## Available Parameters

All parameters are optional — include only what your check needs. The `evaluate` method signature can use any subset:

| Parameter | Type | Description |
|-----------|------|-------------|
| `model_output` | str | The target's actual response |
| `scenario_input` | str | The original scenario input |
| `scenario_result` | str | The expected result from the scenario |
| `metadata` | dict | Metadata associated with the model output |
| `model_input` | str | The input sent to the model |

## Return Type

Return a `CheckResponse` with:
- **score**: `bool` for pass/fail checks, `int` or `float` for scored checks
- **explanation**: A string explaining the result

## Example: Keyword Presence Check

Uses `model_output` and `scenario_result` only:

```python
from okareo.checks import CodeBasedCheck, CheckResponse

class Check(CodeBasedCheck):
    @staticmethod
    def evaluate(model_output: str, scenario_result: str) -> CheckResponse:
        """Check that all required keywords from the expected result appear in the output."""
        keywords = [k.strip().lower() for k in scenario_result.split(",")]
        output_lower = model_output.lower()
        score = all(k in output_lower for k in keywords)
        explanation = (
            "All keywords found in output."
            if score
            else "Missing keywords: " + ", ".join(k for k in keywords if k not in output_lower)
        )
        return CheckResponse(score=score, explanation=explanation)
```

## Example: Length Constraint Check

Uses `model_output` only:

```python
from okareo.checks import CodeBasedCheck, CheckResponse

class Check(CodeBasedCheck):
    @staticmethod
    def evaluate(model_output: str) -> CheckResponse:
        """Verify the output is within an acceptable length range."""
        word_count = len(model_output.split())
        score = 10 <= word_count <= 500
        explanation = f"Word count: {word_count} ({'within' if score else 'outside'} 10-500 range)."
        return CheckResponse(score=score, explanation=explanation)
```

## Example: JSON Format Check

Uses `model_output` only:

```python
from okareo.checks import CodeBasedCheck, CheckResponse

class Check(CodeBasedCheck):
    @staticmethod
    def evaluate(model_output: str) -> CheckResponse:
        """Verify the output is valid JSON."""
        import json
        try:
            json.loads(model_output)
            return CheckResponse(score=True, explanation="Output is valid JSON.")
        except (json.JSONDecodeError, TypeError) as e:
            return CheckResponse(score=False, explanation=f"Invalid JSON: {e}")
```

## Usage in Okareo

Register the class as a Code Check in Okareo using `create_or_update_check` with `check_type="code"`. The `evaluate` method will be called once per scenario row during evaluation. Results appear alongside prompt-based check results in your test run.
