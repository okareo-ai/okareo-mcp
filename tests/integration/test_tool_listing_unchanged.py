"""Running tools on worker threads changes nothing a client can see (049).

Every tool's whole ``list_tools()`` entry (input and output schema, title,
annotations, description) must be identical with and without the hook, in
both transports. Each transport registers its own tools (``save_scenario`` is
defined once per transport), and the transport is fixed when ``src.server`` is
imported, so each mode runs in its own process.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

_SCRIPT = r"""
import asyncio, json
from src import server
from src.auth.protected_resource import OkareoFastMCP
from src.tools import (
    checks, clone, docs, insights, models, projects, reps, scenarios,
    simulations, tenants, tests, voice,
)

# The class every tool registered on before 049, given the same modules in
# the order src/server.py registers them, with nothing copied from the hooked
# server: titles, descriptions, annotations and schemas are derived anew.
baseline = OkareoFastMCP("baseline")
for module in (tests, scenarios, models, simulations, checks, docs, reps,
               voice, insights, projects, clone, tenants):
    module.register_tools(baseline)

async def listing(app):
    return {t.name: t.model_dump(mode="json") for t in await app.list_tools()}

print(json.dumps({
    "hooked": asyncio.run(listing(server.mcp)),
    "baseline": asyncio.run(listing(baseline)),
}))
"""


def _listings(env_overrides: dict[str, str]) -> dict:
    env = {**os.environ, **env_overrides}
    out = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=_REPO, env=env, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "mode,env",
    [
        ("stdio", {"TRANSPORT": "stdio", "OKAREO_API_KEY": "test-key"}),
        (
            "streamable-http",
            {
                "TRANSPORT": "streamable-http",
                "MCP_RESOURCE_SERVER_URL": "http://localhost:8080",
                "FRONTEGG_DOMAIN": "test.frontegg.example",
                "FRONTEGG_CLIENT_ID": "test-client",
                "MCP_DCR_SIGNING_KEY": "x" * 43,
            },
        ),
    ],
)
def test_every_tool_listing_is_identical_with_and_without_the_hook(mode, env):
    result = _listings(env)
    hooked, baseline = result["hooked"], result["baseline"]
    assert len(hooked) >= 59, f"{mode}: only {len(hooked)} tools listed"
    assert hooked.keys() == baseline.keys()
    differing = [name for name in hooked if hooked[name] != baseline[name]]
    assert not differing, f"{mode}: listings differ for {differing}"
