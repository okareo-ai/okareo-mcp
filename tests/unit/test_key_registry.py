"""044 US4: the openai_assistant Target type is gone from the SDK (OpenAI shut
the Assistants API down on 2026-08-26), so its key is no longer forwarded."""

import os

from src.key_registry import scan_provider_keys


def _clear_provider_keys(monkeypatch):
    for var in list(os.environ):
        if var.endswith("_API_KEY"):
            monkeypatch.delenv(var, raising=False)


def test_openai_assistant_key_not_forwarded(monkeypatch):
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("OPENAI_ASSISTANT_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "y")
    assert scan_provider_keys() == {"openai": "y"}


def test_other_providers_still_forwarded(monkeypatch):
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("GENERATION_API_KEY", "g")
    monkeypatch.setenv("VOICE_API_KEY", "v")
    assert scan_provider_keys() == {"generation": "g", "voice": "v"}
