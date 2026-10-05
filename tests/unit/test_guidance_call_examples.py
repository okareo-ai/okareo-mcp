"""045 amendment A1 (FR-013): every example call to a guidance tool that the
server shows an agent must work as written.

Scans everything the server serves as guidance: all tool descriptions, the
server instructions, and every template. An agent copies these calls
literally, so a wrong argument shape is a call that fails.
"""

import asyncio
import re
from pathlib import Path

import pytest

from src.server import mcp
from src.tools.docs import TEMPLATE_NAMES

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "src/templates"

# get_templates(<argument>) as written in prose or backticks.
_CALL = re.compile(r"get_templates\(([^()]*)\)")
# The argument forms the tool accepts: nothing, a quoted name, or the same
# with the parameter named.
_ACCEPTED = re.compile(
    r"""^\s*(?:template_name\s*=\s*)?(?P<q>["'])(?P<name>[a-z_]+)(?P=q)\s*$"""
)


def _served_guidance() -> dict[str, str]:
    served: dict[str, str] = {"server instructions": mcp.instructions or ""}
    for tool in asyncio.run(mcp.list_tools()):
        served[f"tool description: {tool.name}"] = tool.description or ""
    for path in sorted(TEMPLATES_DIR.glob("*.md")):
        served[f"template: {path.stem}"] = path.read_text()
    return served


SERVED = _served_guidance()
EXAMPLES = [
    (source, match.group(1))
    for source, text in SERVED.items()
    for match in _CALL.finditer(text)
]


def test_the_scan_finds_the_known_examples():
    """Guards the other tests against passing by finding nothing."""
    sources = {source for source, _ in EXAMPLES}
    assert "tool description: run_simulation" in sources
    assert len(EXAMPLES) >= 3, EXAMPLES


@pytest.mark.parametrize("source", sorted(SERVED))
def test_no_list_argument_to_a_guidance_tool(source):
    text = SERVED[source]
    assert "get_templates([" not in text, f"{source}: get_templates takes a name, not a list"
    assert "get_docs([" not in text, f"{source}: get_docs takes a query, not a list"


@pytest.mark.parametrize("source,argument", EXAMPLES)
def test_example_uses_an_accepted_argument(source, argument):
    if argument.strip() == "":
        return  # get_templates() lists the templates
    match = _ACCEPTED.match(argument)
    assert match, f"{source}: get_templates({argument}) is not a call the tool accepts"
    assert match.group("name") in TEMPLATE_NAMES, (
        f"{source}: get_templates({argument}) names a template that does not exist; "
        f"available: {TEMPLATE_NAMES}"
    )
