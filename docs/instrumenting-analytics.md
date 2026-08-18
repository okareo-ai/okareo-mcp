# Instrumenting a tool for analytics

To attach context to the shared `okareo_mcp_tool_call` event, call
`annotate()` from the tool's own call stack at the point a value is known:

```python
from src.analytics_context import annotate

annotate(
    project_id=project_id,
    entity_type="scenario",
    entity_id=str(result.id),
)
```

Only allow-listed keys are accepted; unknown keys and non-scalar values are
dropped. Never annotate user text (names, prompts, rows, transcripts).

Full rules, the allow-list, and local verification commands:
[specs/034-analytics-entity-context/quickstart.md](../specs/034-analytics-entity-context/quickstart.md)
(Part A).
