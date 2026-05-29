"""Unit tests for src/target_redaction.py (spec 023-tool-fixes US1)."""

from src.target_redaction import (
    REDACTION_SENTINEL,
    apply_redaction,
    find_sentinel_paths,
)


# --- apply_redaction ---------------------------------------------------------

def test_apply_redaction_top_level_path() -> None:
    payload = {"token": "secret", "name": "x"}
    result = apply_redaction(payload, ["token"])
    assert result == {"token": REDACTION_SENTINEL, "name": "x"}


def test_apply_redaction_nested_dict_path() -> None:
    payload = {
        "auth_params": {
            "headers": {"Authorization": "Bearer abc", "Content-Type": "application/json"},
            "body": {"client_secret": "shh"},
        }
    }
    result = apply_redaction(
        payload,
        ["auth_params.headers.Authorization", "auth_params.body.client_secret"],
    )
    assert result["auth_params"]["headers"]["Authorization"] == REDACTION_SENTINEL
    assert result["auth_params"]["headers"]["Content-Type"] == "application/json"
    assert result["auth_params"]["body"]["client_secret"] == REDACTION_SENTINEL


def test_apply_redaction_list_index_path() -> None:
    payload = {"items": [{"value": "a"}, {"value": "b"}]}
    result = apply_redaction(payload, ["items[1].value"])
    assert result["items"][0]["value"] == "a"
    assert result["items"][1]["value"] == REDACTION_SENTINEL


def test_apply_redaction_silently_skips_missing_paths() -> None:
    payload = {"a": 1}
    # The path doesn't resolve — apply_redaction should not raise.
    result = apply_redaction(payload, ["b.c.d", "nonexistent"])
    assert result == {"a": 1}


def test_apply_redaction_empty_sensitive_list_is_noop() -> None:
    payload = {"a": 1, "b": {"c": 2}}
    assert apply_redaction(payload, []) == payload
    assert apply_redaction(payload, None) == payload  # type: ignore[arg-type]


def test_apply_redaction_does_not_mutate_input() -> None:
    payload = {"token": "secret"}
    apply_redaction(payload, ["token"])
    assert payload == {"token": "secret"}, "input was mutated"


# --- find_sentinel_paths -----------------------------------------------------

def test_find_sentinel_paths_returns_empty_when_none_present() -> None:
    payload = {
        "auth_params": {
            "headers": {"Authorization": "Bearer real-token"},
            "body": {"client_secret": "real-secret"},
        }
    }
    assert find_sentinel_paths(payload) == []


def test_find_sentinel_paths_detects_single_path() -> None:
    payload = {
        "auth_params": {
            "headers": {"Authorization": REDACTION_SENTINEL},
        }
    }
    assert find_sentinel_paths(payload) == ["auth_params.headers.Authorization"]


def test_find_sentinel_paths_detects_multiple_paths() -> None:
    payload = {
        "auth_params": {
            "headers": {"Authorization": REDACTION_SENTINEL},
            "body": {"client_secret": REDACTION_SENTINEL, "client_id": "abc"},
        }
    }
    paths = find_sentinel_paths(payload)
    assert set(paths) == {
        "auth_params.headers.Authorization",
        "auth_params.body.client_secret",
    }


def test_find_sentinel_paths_walks_lists() -> None:
    payload = {"items": [{"v": "ok"}, {"v": REDACTION_SENTINEL}, "literal"]}
    assert find_sentinel_paths(payload) == ["items[1].v"]


def test_find_sentinel_paths_uses_exact_equality_not_substring() -> None:
    # A field whose value contains the sentinel as a substring is NOT flagged.
    payload = {
        "description": f"This Target uses {REDACTION_SENTINEL} as a placeholder",
    }
    assert find_sentinel_paths(payload) == []


def test_find_sentinel_paths_does_not_match_non_string_values() -> None:
    payload = {"count": 0, "enabled": False, "items": []}
    assert find_sentinel_paths(payload) == []
