# Custom Endpoint Streaming (SSE) Configuration

Use SSE streaming configuration when your target API returns responses as a
Server-Sent Events stream instead of a single JSON response. This is common
with LLM APIs like OpenAI's streaming chat completions.

## Structure

Add a `streaming` object inside `next_message_params` (or `start_session_params`):

```json
{
  "next_message_params": {
    "url": "https://api.example.com/chat",
    "method": "POST",
    "body": "{\"messages\": [{message_history}], \"stream\": true}",
    "response_message_path": "response.choices[0].delta.content",
    "streaming": {
      "stop": [ ... ],
      "select": [ ... ]
    }
  }
}
```

### Stop Conditions (OR semantics)

Any single match terminates the stream. Each condition has:
- `value` (required): String to match. Supports `"true"`/`"false"` for booleans, `"*"` for any non-null value, or exact string match.
- `path` (optional): Dot-path into the parsed JSON chunk (e.g., `response.is_final`). When omitted, `value` is matched against the raw SSE `data:` payload before JSON parsing.

### Select Conditions (AND semantics)

All conditions must match for a chunk's content to be extracted. Each condition requires:
- `path` (required): Dot-path into the parsed JSON chunk.
- `value` (required): Same matching rules as stop conditions.

When no select conditions are provided, content is extracted from every chunk.

## Examples

### 1. OpenAI-style streaming

The stream ends when the server sends `data: [DONE]`:

```json
{
  "next_message_params": {
    "url": "https://api.openai.com/v1/chat/completions",
    "method": "POST",
    "headers": {"Authorization": "Bearer {access_token}"},
    "body": "{\"model\": \"gpt-4o\", \"messages\": [{message_history}], \"stream\": true}",
    "response_message_path": "response.choices[0].delta.content",
    "streaming": {
      "stop": [
        {"value": "[DONE]"}
      ],
      "select": [
        {"path": "response.choices[0].delta.content", "value": "*"}
      ]
    }
  }
}
```

### 2. Boolean stop + role-based filtering

The stream ends when `is_final` is true; only extract content from assistant chunks:

```json
{
  "next_message_params": {
    "url": "https://api.example.com/stream",
    "method": "POST",
    "body": "{\"message\": \"{latest_message}\"}",
    "response_message_path": "response.assistant_response",
    "streaming": {
      "stop": [
        {"value": "true", "path": "response.is_final"}
      ],
      "select": [
        {"path": "response.role", "value": "assistant"}
      ]
    }
  }
}
```

### 3. Streaming on start_session_params

If your session initialization endpoint also streams:

```json
{
  "start_session_params": {
    "url": "https://api.example.com/session/start",
    "method": "POST",
    "response_session_id_path": "response.session_id",
    "response_message_path": "response.welcome_text",
    "streaming": {
      "stop": [
        {"value": "[DONE]"}
      ]
    }
  }
}
```

## Tips

- Set `response_message_path` to the chunk-level field (e.g., `response.choices[0].delta.content`), not the full response shape.
- Stop conditions without a `path` match raw SSE data lines before JSON parsing. Use this for sentinel values like `[DONE]`.
- Stop conditions with a `path` match after JSON parsing. Use this for boolean flags like `response.is_final`.
- Select conditions filter which chunks contribute to the assembled response. Use `"*"` as the value to match any non-null value at a path.
