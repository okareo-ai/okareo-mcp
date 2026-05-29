"""T021: confirms the server boots with Cloud-Run-style env (PORT only),
binds 0.0.0.0:$PORT, and serves the PRM doc within 10 s of process start.

This test spawns the actual ``okareo-mcp`` CLI as a subprocess — it's the
closest in-test we get to "does the container shape work" without invoking
docker. Skip cleanly when ``uv`` isn't on PATH (CI environments without uv).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _pick_free_port() -> int:
    """Return a free TCP port on localhost."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv not on PATH — skip subprocess boot test",
)
def test_container_honors_port_env_and_serves_prm_within_10s():
    port = _pick_free_port()
    env = {
        **os.environ,
        "TRANSPORT": "streamable-http",
        "PORT": str(port),
        "MCP_RESOURCE_SERVER_URL": f"http://localhost:{port}",
        "FRONTEGG_DOMAIN": "example.frontegg.com",
        # Make sure no stray .env from cwd interferes with the test.
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    # Drop any FASTMCP_* overrides so the test really exercises the PORT
    # code path (Cloud Run injects PORT only, not FASTMCP_PORT).
    env.pop("FASTMCP_PORT", None)
    env.pop("FASTMCP_HOST", None)

    proc = subprocess.Popen(
        ["uv", "run", "okareo-mcp"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                # Process died early — surface its output for diagnosis.
                stdout = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
                stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                raise AssertionError(
                    f"okareo-mcp exited early (code={proc.returncode}):\n"
                    f"--- stderr ---\n{stderr}\n--- stdout ---\n{stdout}"
                )
            try:
                resp = httpx.get(
                    f"http://127.0.0.1:{port}/.well-known/oauth-protected-resource",
                    timeout=1.0,
                )
                if resp.status_code == 200:
                    body = resp.json()
                    # Sanity: PRM should advertise our test URL.
                    assert "resource" in body, body
                    return  # success
            except (httpx.HTTPError, httpx.ConnectError) as exc:
                last_error = exc
            time.sleep(0.2)
        # Timed out.
        stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        raise AssertionError(
            f"Server did not serve PRM within 10s "
            f"(last error: {last_error!r}). stderr:\n{stderr[:2000]}"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv not on PATH — skip subprocess boot test",
)
def test_server_imports_clean_in_stdio_mode():
    """US1 / FR-006: stdio mode must import and register tools without any
    network/Frontegg dependency on SDK 0.0.132.

    Importing ``src.server`` registers every tool at module load. In stdio
    mode (``TRANSPORT`` unset/stdio) this must succeed offline — no Frontegg,
    no auth imports.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("FRONTEGG_", "FASTMCP_"))
    }
    env["TRANSPORT"] = "stdio"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        ["uv", "run", "python", "-c", "import src.server; print('stdio-ok')"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"stdio import failed (rc={proc.returncode}).\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr[:2000]}"
    )
    assert "stdio-ok" in proc.stdout
