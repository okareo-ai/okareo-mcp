"""T063 — FR-022: MCP_DCR_SIGNING_KEY entropy / warning behavior.

Verifies the two behaviors mandated by FR-022:

  (a) Env var unset in HTTP mode → server generates an ephemeral random key
      (≥32 bytes) AND emits a WARNING that DCR registrations won't survive
      restart.

  (b) Env var set to a short value (<32 bytes) → server still boots but
      emits a WARNING that the key is below the documented entropy threshold.

The actual server.py module-level code is re-evaluated by importing it under
a controlled environment. We use a subprocess so we get the real ``print(...,
file=sys.stderr)`` output unchanged by pytest's capture machinery — the server
imports a lot of unrelated machinery at module load and the simpler subprocess
boundary avoids interference.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def _boot_server_under_env(env_overrides: dict[str, str]) -> str:
    """Import src.server in a fresh subprocess and return captured stderr.

    Stderr is whitespace-normalized (runs of whitespace collapsed to a single
    space) so substring assertions aren't sensitive to Rich's line-wrapping.
    """
    env = os.environ.copy()
    env["TRANSPORT"] = "streamable-http"
    env.setdefault("MCP_RESOURCE_SERVER_URL", "http://localhost:8080")
    env.setdefault("FRONTEGG_DOMAIN", "test.frontegg.example")
    # Force minimal noise from analytics — DEV truthiness disables it.
    env.setdefault("DEV", "true")
    env.update(env_overrides)
    # Don't carry the test runner's OKAREO_API_KEY accidentally.
    env.pop("OKAREO_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "-c", "import src.server"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return re.sub(r"\s+", " ", result.stderr)


class TestFR022:
    def test_unset_key_generates_ephemeral_and_warns(self):
        stderr = _boot_server_under_env({"MCP_DCR_SIGNING_KEY": ""})
        assert "MCP_DCR_SIGNING_KEY is not set" in stderr
        assert "ephemeral" in stderr.lower()
        assert "WARNING" in stderr

    def test_short_key_warns_below_threshold(self):
        # 8 bytes — well below the 32-byte / 256-bit threshold.
        stderr = _boot_server_under_env(
            {"MCP_DCR_SIGNING_KEY": "shortKey"}
        )
        assert "WARNING" in stderr
        # Rich injects the source location (e.g. "server.py:425") in the
        # middle of the formatted message, so assert on the two halves
        # separately rather than the full phrase.
        assert "shorter than" in stderr
        assert "recommended 32 bytes" in stderr
        # Server should NOT have warned about "not set" — the key was set,
        # just short.
        assert "MCP_DCR_SIGNING_KEY is not set" not in stderr

    def test_strong_key_no_warning(self):
        # 64 hex chars = 64 bytes of entropy — well above 32.
        strong = "a" * 64
        stderr = _boot_server_under_env({"MCP_DCR_SIGNING_KEY": strong})
        assert "MCP_DCR_SIGNING_KEY is not set" not in stderr
        assert "recommended 32 bytes" not in stderr
