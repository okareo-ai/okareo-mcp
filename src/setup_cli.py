"""Interactive setup CLI for configuring okareo-mcp in copilot environments.

Detects installed copilots (Claude Code, Cursor), prompts for the Okareo API key,
and writes the appropriate MCP server configuration into copilot-specific config files.
"""

import json
import os
import sys
from pathlib import Path


def _get_shell_profile() -> Path:
    """Determine the user's shell profile file based on $SHELL."""
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        return Path.home() / ".zshrc"
    if "bash" in shell:
        return Path.home() / ".bashrc"
    # Fallback: try .zshrc on macOS, .bashrc elsewhere
    if sys.platform == "darwin":
        return Path.home() / ".zshrc"
    return Path.home() / ".bashrc"


def _detect_copilots(cwd: Path) -> dict[str, bool]:
    """Detect which copilots are configured in the current directory."""
    return {
        "claude_code": (cwd / ".mcp.json").exists() or (cwd / ".claude").is_dir(),
        "cursor": (cwd / ".cursor").is_dir(),
    }


def _update_mcp_config(path: Path, server_entry: dict) -> None:
    """Write or merge an MCP server entry into a copilot config file.

    Preserves existing server entries; only updates the 'okareo' key.
    Creates the file and parent directories if they don't exist.
    """
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = {}

    existing.setdefault("mcpServers", {})
    existing["mcpServers"]["okareo"] = server_entry

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n")


def _export_to_shell_profile(api_key: str) -> Path | None:
    """Append OKAREO_API_KEY export to the user's shell profile.

    Idempotent: skips if an export line already exists.
    Returns the profile path if written, None if skipped.
    """
    profile = _get_shell_profile()
    export_line = f'export OKAREO_API_KEY="{api_key}"'

    if profile.exists():
        content = profile.read_text()
        if "OKAREO_API_KEY" in content:
            return None  # Already exported

    with open(profile, "a") as f:
        f.write(f"\n# Okareo MCP API key\n{export_line}\n")

    return profile


def _build_server_entry(key_value: str) -> dict:
    """Build the MCP server entry dict with the given key value."""
    return {
        "command": "uvx",
        "args": ["okareo-mcp"],
        "env": {
            "OKAREO_API_KEY": key_value,
        },
    }


def _configure_claude_code(cwd: Path, api_key: str) -> list[str]:
    """Configure Claude Code: write ${VAR} ref + shell profile export."""
    actions = []

    config_path = cwd / ".mcp.json"
    entry = _build_server_entry("${OKAREO_API_KEY}")
    _update_mcp_config(config_path, entry)
    actions.append(f"  Wrote {config_path}")

    profile = _export_to_shell_profile(api_key)
    if profile:
        actions.append(f"  Exported OKAREO_API_KEY to {profile}")
    else:
        actions.append("  OKAREO_API_KEY already in shell profile (skipped)")

    return actions


def _configure_cursor(cwd: Path, api_key: str) -> list[str]:
    """Configure Cursor: write literal key into .cursor/mcp.json."""
    actions = []

    config_path = cwd / ".cursor" / "mcp.json"
    entry = _build_server_entry(api_key)
    _update_mcp_config(config_path, entry)
    actions.append(f"  Wrote {config_path}")

    return actions


def _print_fallback_snippet(api_key: str) -> None:
    """Print a manual config snippet when no copilot is detected."""
    snippet = json.dumps(
        {
            "mcpServers": {
                "okareo": _build_server_entry(api_key),
            },
        },
        indent=2,
    )
    print("\nNo supported copilot detected in this directory.")
    print("Add the following to your copilot's MCP config file:\n")
    print(snippet)
    print()
    print("For Claude Code: save as .mcp.json in your project root")
    print("For Cursor: save as .cursor/mcp.json in your project root")


def main() -> None:
    """CLI entry point for okareo-mcp-setup."""
    print("Okareo MCP Setup")
    print("=" * 40)
    print()

    # Check for --key flag for non-interactive use
    api_key = None
    args = sys.argv[1:]
    if "--key" in args:
        idx = args.index("--key")
        if idx + 1 < len(args):
            api_key = args[idx + 1].strip()

    if not api_key:
        api_key = input("Enter your Okareo API key: ").strip()

    if not api_key:
        print("Error: API key cannot be empty.")
        print("Get your API key from https://app.okareo.com")
        raise SystemExit(1)

    cwd = Path.cwd()
    detected = _detect_copilots(cwd)
    any_configured = False
    all_actions: list[str] = []

    # Configure detected copilots
    if detected["claude_code"]:
        print("\nDetected: Claude Code")
        actions = _configure_claude_code(cwd, api_key)
        all_actions.extend(actions)
        any_configured = True

    if detected["cursor"]:
        print("\nDetected: Cursor")
        actions = _configure_cursor(cwd, api_key)
        all_actions.extend(actions)
        any_configured = True

    # If nothing detected, ask the user
    if not any_configured:
        print("\nNo copilot configuration detected in this directory.")
        print("Which copilot(s) do you use?")
        print("  1. Claude Code")
        print("  2. Cursor")
        print("  3. Both")
        print("  4. Other / manual setup")

        choice = input("\nSelect (1-4): ").strip()

        if choice in ("1", "3"):
            actions = _configure_claude_code(cwd, api_key)
            all_actions.extend(actions)
            any_configured = True

        if choice in ("2", "3"):
            actions = _configure_cursor(cwd, api_key)
            all_actions.extend(actions)
            any_configured = True

        if choice == "4" or not any_configured:
            _print_fallback_snippet(api_key)
            return

    # Print summary
    print()
    print("Done! Configuration updated:")
    for action in all_actions:
        print(action)

    print()
    print("Next steps:")
    print("  1. Restart your copilot to pick up the new configuration")
    if any(
        "shell profile" in a.lower() or "exported" in a.lower() for a in all_actions
    ):
        profile = _get_shell_profile()
        print(f"  2. Run: source {profile}  (or restart your terminal)")
    print()
    print("Try asking your copilot: 'List my Okareo scenarios'")
