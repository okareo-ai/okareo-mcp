"""Unit tests for backend-error surfacing in src.error_handling (US3)."""

import json

from src.error_handling import format_tool_error


class _FakeResponse:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class HTTPStatusError(Exception):
    """Mimics httpx.HTTPStatusError by class name (classify_error keys on it)."""

    def __init__(self, response):
        super().__init__("error")
        self.response = response


def test_422_validation_detail_is_surfaced():
    exc = HTTPStatusError(
        _FakeResponse(
            422,
            body={
                "detail": [
                    {
                        "loc": ["body", "measures"],
                        "msg": "unsupported measure",
                        "type": "value_error",
                    }
                ]
            },
        )
    )

    result = json.loads(format_tool_error(exc))

    assert result["error"]["category"] == "validation"
    assert "measures" in result["error"]["message"]
    assert "unsupported measure" in result["error"]["message"]
    # Validation suggestion, not the generic "try again later".
    assert "try again later" not in result["error"]["suggestion"]


def test_string_detail_is_surfaced():
    exc = HTTPStatusError(_FakeResponse(400, body={"detail": "Project not found"}))

    result = json.loads(format_tool_error(exc))

    assert "Project not found" in result["error"]["message"]


def test_500_without_detail_falls_back_to_status():
    exc = HTTPStatusError(_FakeResponse(500, body=None, text=""))

    result = json.loads(format_tool_error(exc))

    assert result["error"]["category"] == "server_error"
    assert "HTTP 500" in result["error"]["message"]
