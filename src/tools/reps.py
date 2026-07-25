"""REPS baseline tool for the Okareo MCP server.

Provides one MCP tool:

- get_reps_baseline: Serve the REPS agent-evaluation baseline (the reps/
  tree of the okareo-ai/okareo-tools repo) from its latest tagged GitHub
  Release, so reps skills can run evaluations in environments with no
  local copy of the material (e.g. claude.ai / Claude Desktop sandboxes).

The response envelope is stable and additive-only from v1 — see
specs/033-serve-reps-baseline/contracts/get_reps_baseline.md.
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from src import reps_baseline
from src.analytics import is_truthy
from src.reps_baseline import (
    RECOGNIZED_PILLARS,
    BaselineSnapshot,
    BaselineUnavailableError,
)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _provenance(snapshot: BaselineSnapshot) -> dict:
    fields: dict = {
        "tag": snapshot.tag,
        "fetched_at": _iso(snapshot.fetched_at),
        "stale": snapshot.stale,
        "pin": snapshot.pinned,
    }
    if snapshot.stale and snapshot.stale_reason:
        fields["stale_reason"] = snapshot.stale_reason
    return fields


def _envelope(snapshot: BaselineSnapshot, mode: str, **fields) -> str:
    return json.dumps({"mode": mode, **_provenance(snapshot), **fields})


def _error(
    code: str,
    message: str,
    snapshot: Optional[BaselineSnapshot] = None,
    **context,
) -> str:
    payload: dict = {"error": {"code": code, "message": message, **context}}
    if snapshot is not None:
        payload["tag"] = snapshot.tag
        payload["stale"] = snapshot.stale
    return json.dumps(payload)


def register_tools(mcp: FastMCP) -> None:
    """Register the REPS baseline tool with the FastMCP server."""

    @mcp.tool(
        title="Get REPS Baseline Material",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    def get_reps_baseline(
        pillar: Optional[str] = None,
        path: Optional[str] = None,
        version: Optional[str] = None,
    ) -> str:
        """Serve REPS agent-evaluation baseline material (scenario banks, drivers, checks, eval configs).

        REPS is Okareo's agent-evaluation workbench: per-pillar baseline
        material for evaluating AI agents across R-reasoning, E-execution,
        P-performance, and S-security, plus shared explore/ probes and a
        profile/ example. The material is published as tagged releases of
        the okareo-tools repo; this tool serves the latest release so reps
        skills need no local copy of the tree.

        Two modes:
        - Discovery (omit `path`): list what files exist in the served
          release — the full tree, or one area via `pillar`. File lists
          change between releases, so always discover before fetching.
        - Fetch (provide `path`): return one file's exact content as
          published in the release. Use paths verbatim from discovery,
          e.g. 'S-security/scenarios/verification-gate.jsonl'.

        Every response carries the release tag it was served from (e.g.
        'v0.5.1') — record it in evaluation reports as baseline
        provenance. `stale: true` means the last release check failed and
        the content may lag the newest release.

        Args:
            pillar: Optional discovery filter. One of: R-reasoning,
                E-execution, P-performance, S-security, explore, profile.
                Omit to list the entire baseline tree (which also includes
                shared material outside these areas).
            path: Optional file path (relative to the baseline tree, as
                returned by discovery). Provide to fetch that file's
                content; omit for discovery.
            version: Optional release tag. Currently only the served tag
                is available; any other value returns an error naming what
                IS available. Omit to accept the served release.
        """
        if is_truthy(os.environ.get("AIRGAP")):
            return _error(
                "baseline_unavailable",
                "REPS baseline is disabled in airgap mode.",
                detail=(
                    "No external network calls are made when AIRGAP is "
                    "enabled, and the baseline is sourced from GitHub."
                ),
            )

        try:
            snapshot = reps_baseline.get_snapshot()
        except BaselineUnavailableError as e:
            return _error("baseline_unavailable", str(e), detail="Retry later.")

        if version is not None and version.strip() and version.strip() != snapshot.tag:
            return _error(
                "version_not_available",
                f"Version '{version}' is not available. This server "
                f"currently serves {snapshot.tag}.",
                snapshot=snapshot,
                available_versions=[snapshot.tag],
            )

        if path is not None:
            if not path.strip():
                return _error(
                    "invalid_request",
                    "path must be a non-empty file path from discovery.",
                    snapshot=snapshot,
                    detail="Omit path entirely to list available files.",
                )
            record = snapshot.files.get(path)
            if record is None:
                return _error(
                    "unknown_path",
                    f"'{path}' is not a file in the {snapshot.tag} baseline.",
                    snapshot=snapshot,
                    suggestion=(
                        "Call get_reps_baseline() without path to discover "
                        "the files available in this release."
                    ),
                )
            if record.oversize or record.content is None:
                return _error(
                    "file_not_servable",
                    f"'{path}' exceeds the servable file size limit.",
                    snapshot=snapshot,
                    path=path,
                )
            try:
                text = record.content.decode("utf-8")
            except UnicodeDecodeError:
                return _error(
                    "file_not_servable",
                    f"'{path}' is not UTF-8 text and cannot be served.",
                    snapshot=snapshot,
                    path=path,
                )
            return _envelope(
                snapshot,
                "fetch",
                path=path,
                size=record.size,
                content=text,
            )

        # Discovery mode
        pillar_filter = pillar.strip() if pillar is not None else None
        if pillar_filter is not None and pillar_filter not in RECOGNIZED_PILLARS:
            return _error(
                "unknown_pillar",
                f"'{pillar}' is not a recognized pillar or shared directory.",
                snapshot=snapshot,
                valid_pillars=RECOGNIZED_PILLARS,
            )

        records = sorted(snapshot.files.values(), key=lambda r: r.path)
        if pillar_filter is not None:
            records = [r for r in records if r.pillar == pillar_filter]

        files = []
        for record in records:
            entry: dict = {
                "path": record.path,
                "size": record.size,
                "pillar": record.pillar,
            }
            if record.oversize:
                entry["oversize"] = True
            files.append(entry)

        return _envelope(
            snapshot,
            "discovery",
            pillar=pillar_filter,
            pillars=RECOGNIZED_PILLARS,
            files=files,
            count=len(files),
        )

    return None
