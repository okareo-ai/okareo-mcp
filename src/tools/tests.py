"""Test run tools for the Okareo MCP server.

Provides five MCP tools for the core test execution workflow:

- list_checks: Browse available quality checks that can evaluate model outputs
- run_test: Execute a test by evaluating a model against a scenario with checks
- list_test_runs: Browse past test runs with optional filters
- get_test_run_results: Load per-row scores of a specific test run (transcripts opt-in)
- get_conversation_transcript: Retrieve the full transcript for a single conversation
"""

import json
from typing import Annotated, Any, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from okareo_api_client.errors import UnexpectedStatus

from src.analytics_context import annotate
from src.error_handling import ArtifactNotInProject, format_tool_error
from src.okareo_client import (
    organization_scoped,
    find_test_runs,
    PROJECT_PARAM_DESC,
    get_okareo_client,
    resolve_artifact_by_name,
    project_scoped,
    resolve_project,
    resolve_run_provenance,
)
from src.run_config import rerun_block, resolve_run_config
from src.response_depth import (
    DEPTHS_LISTING,
    DEPTHS_RUN,
    DETAILED,
    FULL,
    SUMMARY,
    page_window,
    validate_depth,
)

# Test-run statuses that block re-evaluation — the run has not produced a
# terminal result set yet.
_NON_TERMINAL_STATUSES = {"RUNNING", "PENDING", "STARTED", "IN_PROGRESS", "QUEUED"}


def _serialize_datetime(dt) -> Optional[str]:
    """Convert a datetime to ISO format string, handling Unset values."""
    if dt is None or isinstance(dt, type(None)):
        return None
    try:
        return dt.isoformat()
    except (AttributeError, TypeError):
        return None


def _serialize_metrics(metrics) -> Optional[dict]:
    """Convert model metrics to a plain dict, handling Unset values."""
    if metrics is None:
        return None
    # find_test_runs surfaces a 200 through UnexpectedStatus, whose body is
    # json.loads'd into plain dicts -- the common path in practice, and one
    # this helper used to answer None for.
    if isinstance(metrics, dict):
        return dict(metrics)
    try:
        if hasattr(metrics, "additional_properties"):
            return dict(metrics.additional_properties)
        if hasattr(metrics, "to_dict"):
            return metrics.to_dict()
        return None
    except (AttributeError, TypeError):
        return None


def _serialize_value(val):
    """Serialize a value that may be Unset, a complex object, or a primitive."""
    if val is None:
        return None
    # Handle Unset sentinel from the SDK
    if type(val).__name__ == "Unset":
        return None
    if hasattr(val, "additional_properties"):
        return dict(val.additional_properties)
    if hasattr(val, "to_dict"):
        return val.to_dict()
    if isinstance(val, (dict, list, str, int, float, bool)):
        return val
    return str(val)


def _get_attr(obj, attr, default=None):
    """Get an attribute, returning default if Unset."""
    val = getattr(obj, attr, default)
    if type(val).__name__ == "Unset":
        return default
    return val


def _build_scenario_index_map(run_metadata: Optional[dict]) -> dict:
    """Build a test_id → scenario_index lookup from run metadata scores_by_row."""
    if not run_metadata:
        return {}
    metrics = run_metadata.get("model_metrics")
    if not isinstance(metrics, dict):
        return {}
    scores_by_row = metrics.get("scores_by_row", [])
    if not isinstance(scores_by_row, list):
        return {}
    mapping = {}
    for row in scores_by_row:
        if isinstance(row, dict):
            test_id = row.get("test_id")
            idx = row.get("scenario_index")
            if test_id is not None and idx is not None:
                mapping[str(test_id)] = idx
    return mapping


def _data_point_id(dp) -> str:
    return str(_get_attr(dp, "id") or _get_attr(dp, "test_id") or "")


def _scenario_index_map(run_metadata: Optional[dict], data_points) -> dict:
    """Resolve test_id → scenario_index for a run's data points.

    scores_by_row is a positional list that carries no test_id, so the
    metadata map is empty for every run the API actually returns. Fall back
    to 1-based position in the data-point list — both this tool and
    get_conversation_transcript issue the identical find_test_data_points
    request, so the ordering agrees.
    """
    mapping = _build_scenario_index_map(run_metadata)
    if mapping:
        return mapping
    if not isinstance(data_points, list):
        return {}
    return {
        _data_point_id(dp): i
        for i, dp in enumerate(data_points, start=1)
        if _data_point_id(dp)
    }


def _find_test_run(okareo, project_id, identifier: str):
    """Resolve a test-run identifier (UUID or name) to its run record.

    Returns the run dict/object, or ``None`` if nothing matches.
    """
    from okareo_api_client.models.general_find_payload import GeneralFindPayload

    def _query(**kw):
        try:
            runs = find_test_runs(okareo, GeneralFindPayload(project_id=project_id, **kw))
        except UnexpectedStatus as e:
            runs = json.loads(e.content) if e.status_code == 200 else None
        except Exception:
            runs = None
        return runs if isinstance(runs, list) else None

    by_id = _query(id=identifier)
    if by_id:
        return by_id[0]

    everything = _query()
    if not everything:
        return None
    matches = [
        r for r in everything
        if (r.get("name") if isinstance(r, dict) else _get_attr(r, "name")) == identifier
    ]
    if not matches:
        return None
    matches.sort(
        key=lambda r: (
            r.get("start_time", "") if isinstance(r, dict)
            else str(_get_attr(r, "start_time", ""))
        ),
        reverse=True,
    )
    return matches[0]


def _derive_run_check_ids(okareo, run_id, name_to_id: dict) -> list:
    """Derive a run's checks from its data points' metric_value keys.

    The TestRunItem record does not list its checks, so the check names are
    recovered from per-row metric values and mapped to check ids.
    """
    from okareo_api_client.models.find_test_data_point_payload import (
        FindTestDataPointPayload,
    )

    try:
        dps = okareo.find_test_data_points(
            FindTestDataPointPayload(test_run_id=run_id, full_data_point=True)
        )
    except Exception:
        return []
    if not isinstance(dps, list):
        return []
    check_names: set = set()
    for dp in dps:
        mv = _serialize_value(_get_attr(dp, "metric_value"))
        if isinstance(mv, dict):
            check_names.update(mv.keys())
    return [name_to_id[n] for n in check_names if n in name_to_id]


# The run-level fields this tool returns. A defined set, not a passthrough:
# the raw record also carries filter_group_id, error_matrix, failure_message,
# progress and tags, none of which answer a question a caller asked. Author
# identity is retained deliberately -- it is provenance, answering who
# produced a result (FR-006a).
_RUN_ENVELOPE_FIELDS = (
    "id",
    "name",
    "type",
    "status",
    "test_data_point_count",
    "start_time",
    "end_time",
    "time_created",
    "app_link",
    "simulation_params",
    "author_email",
    "author_name",
    "author_type",
)


def _run_envelope(run, okareo, project_id: str) -> dict:
    """Build the defined run-level response from a raw run record."""
    envelope: dict = {}
    for field in _RUN_ENVELOPE_FIELDS:
        value = (
            run.get(field) if isinstance(run, dict) else _get_attr(run, field)
        )
        if value is not None:
            envelope[field] = _serialize_value(value)

    envelope.update(resolve_run_provenance(okareo, run, project_id))

    metrics = (
        run.get("model_metrics") if isinstance(run, dict)
        else _get_attr(run, "model_metrics")
    )
    metrics = _serialize_metrics(metrics)
    if isinstance(metrics, dict):
        envelope["model_metrics"] = dict(metrics)
    return envelope


def _run_check_names(run, data_points) -> list:
    """The check names a run was evaluated with.

    `model_metrics.check_ids` is the source and survives
    `return_model_metrics=False`, so it costs nothing. Some runs do not carry
    it, and those fall back to the keys of the rows' own metric values — the
    same derivation `_derive_run_check_ids` already performs (research R9).
    """
    metrics = (
        run.get("model_metrics") if isinstance(run, dict)
        else _get_attr(run, "model_metrics")
    )
    metrics = _serialize_value(metrics)
    if isinstance(metrics, dict):
        names = [
            str(e["name"])
            for e in (metrics.get("check_ids") or [])
            if isinstance(e, dict) and e.get("name")
        ]
        if names:
            return names

    derived: list = []
    for dp in data_points or []:
        mv = _serialize_value(_get_attr(dp, "metric_value"))
        if isinstance(mv, dict):
            for key in mv:
                if key not in derived and not key.endswith("__explanation"):
                    derived.append(str(key))
    return derived


def _next_step_for_run(run_id, depth, limit, offset, has_more) -> Optional[str]:
    """Name the call that retrieves what this response withheld (FR-020).

    Without this an agent's reflex on a truncated response is to re-request
    everything, which is the behaviour this feature exists to stop.
    """
    if has_more:
        return (
            f"get_test_run_results(test_run_id='{run_id}', "
            f"detail_level='{depth}', limit={limit}, offset={offset + limit})"
        )
    if depth == SUMMARY:
        return (
            f"get_test_run_results(test_run_id='{run_id}', "
            "detail_level='detailed') for per-check outcomes and explanations"
        )
    return None


_SHARED_NOTE = (
    "Checks are shared across every project in your organization, not private to the project you are working in."
)


def register_tools(mcp: FastMCP) -> None:
    """Register all test run tools with the FastMCP server."""

    @mcp.tool(
        title="List Checks",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @organization_scoped(_SHARED_NOTE)
    def list_checks(limit: int = 20, all_versions: bool = False) -> str:
        """List available quality checks, grouped by category.

        Returns checks (both built-in and custom) available in your Okareo
        account, organized into `checks_by_category` using the platform's
        `__category:<Category>` tags; checks with no category appear under
        `uncategorized`. Select checks from the category matching your task
        AND modality: voice-specific categories (e.g. voice/audio quality)
        apply to voice simulations, while checks outside voice-specific
        categories are generally useful for both chat and voice. A check
        carrying multiple categories appears under each of them.

        Each check has a name, description, and output_data_type.
        output_data_type uses the server vocabulary: "bool" is a pass/fail
        check and "int" is a scored check — these correspond to output_type
        "pass_fail" and "score" in create_or_update_check and generate_check.
        Use these check names with run_test to evaluate model quality.

        Args:
            limit: Maximum number of checks to return (default 20), applied to
                the total before grouping. Use 0 for no limit.
            all_versions: When false (default), returns only the latest version of
                each check. When true, returns the full version history of every
                check, each entry annotated with its version number.
        """
        try:
            okareo = get_okareo_client()
            checks = okareo.get_all_checks(all_versions=all_versions)
        except Exception as e:
            return format_tool_error(e)

        entries = []
        for check in checks or []:
            entry = {
                "name": _get_attr(check, "name", ""),
                "description": _get_attr(check, "description", ""),
                "output_data_type": _get_attr(check, "output_data_type", ""),
            }
            props = getattr(check, "additional_properties", None)
            if not isinstance(props, dict):
                props = {}
            if all_versions and isinstance(props.get("version"), int):
                entry["version"] = props["version"]
            tags = props.get("tags")
            categories = [
                t[len("__category:"):].strip()
                for t in (tags if isinstance(tags, list) else [])
                if isinstance(t, str) and t.startswith("__category:")
                and t[len("__category:"):].strip()
            ]
            entries.append((entry, categories))

        total = len(entries)
        if limit and limit > 0:
            entries = entries[:limit]

        # A check carrying three categories used to be serialized three
        # times, description and all. Categories now hold names and the
        # entries live once in `checks` (FR-024).
        checks_by_category: dict = {}
        uncategorized: list = []
        catalog: list = []
        for entry, categories in entries:
            catalog.append(entry)
            name = entry["name"]
            if categories:
                for cat in categories:
                    # De-duplicated: with all_versions=True the same name
                    # arrives once per version, and a category listing the
                    # name three times says nothing extra.
                    bucket = checks_by_category.setdefault(cat, [])
                    if name not in bucket:
                        bucket.append(name)
            elif name not in uncategorized:
                uncategorized.append(name)

        response = {
            "checks_by_category": checks_by_category,
            "uncategorized": uncategorized,
            "checks": catalog,
            "count": len(entries),
            "total": total,
            "note": (
                "checks_by_category and uncategorized list check NAMES; each "
                "check's description, output_data_type and version appear in "
                "`checks`. A check with multiple categories is named under "
                "each, and with all_versions=True `checks` holds one entry per "
                "version of that name. Voice-specific categories apply to voice "
                "simulations; other categories are generally useful for both "
                "chat and voice."
            ),
        }
        if not entries:
            response["message"] = "No checks available."
        return json.dumps(response, default=str)

    @mcp.tool(
        title="Run Test",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    @project_scoped
    def run_test(
        scenario_name: str,
        model_name: str,
        checks: list[str],
        name: Optional[str] = None,
        type: str = "NL_GENERATION",
        ctx: Context = None,
        project: Annotated[Optional[str], Field(description=PROJECT_PARAM_DESC)] = None,
    ) -> str:
        """Submit a quality test that evaluates a model against a scenario using checks.

        Returns promptly so the call never times out on long runs. Short runs return
        ``status: "finished"`` with results ready; longer runs return
        ``status: "running"`` with the ``test_run_id`` and ``app_link`` — the run
        continues to completion on its own. In both cases, poll get_test_run_results
        with the returned test_run_id to retrieve scores.

        Args:
            scenario_name: Name of the scenario to evaluate against.
            model_name: Name of the registered model to evaluate.
            checks: List of check names to apply (e.g., ["coherence", "relevance"]).
                Use list_checks to discover available checks and pick from the
                category matching the task and modality — do not use
                voice-specific checks for text evaluations (or vice versa);
                checks outside voice-specific categories suit both.
            name: Optional human-readable name for this test run.
            type: Type of evaluation. Defaults to NL_GENERATION. Valid values:
                NL_GENERATION, INFORMATION_RETRIEVAL, MULTI_CLASS_CLASSIFICATION,
                INVARIANT, MULTI_TURN, AGENT_EVAL.
        """
        # Lazy imports to avoid circular dependencies and keep SDK path setup in okareo_client
        from okareo_api_client.api.default import (
            get_scenario_sets_v0_scenario_sets_get,
        )
        from okareo_api_client.models.test_run_type import TestRunType

        try:
            okareo = get_okareo_client()
            project_id = resolve_project(okareo, project).id
        except Exception as e:
            return format_tool_error(e)

        # Look up scenario by name
        try:
            scenarios = get_scenario_sets_v0_scenario_sets_get.sync(
                client=okareo.client,
                project_id=project_id,
                api_key=okareo.api_key,
            )
        except Exception as e:
            return format_tool_error(e)

        scenario = None
        if scenarios and not isinstance(scenarios, Exception):
            for s in scenarios:
                if _get_attr(s, "name") == scenario_name:
                    scenario = s
                    break

        if scenario is None:
            return json.dumps({
                "error": f"Scenario '{scenario_name}' not found. "
                "Use list_scenarios to see available scenarios.",
            })

        # Look up MUT by name
        try:
            from okareo.model_under_test import ModelUnderTest

            # Resolve inside the acting project, then build the client object
            # from that record — okareo.get_model() resolves the name with no
            # project and cannot see a model outside the default one (R13).
            _resp = resolve_artifact_by_name(
                okareo, model_name, project_id, kind="model"
            )
            _models = _get_attr(_resp, "models") or {}
            if not isinstance(_models, dict):
                _models = {}
            mut = ModelUnderTest(
                client=okareo.client,
                api_key=okareo.api_key,
                mut=_resp,
                models=_models,
            )
        except ArtifactNotInProject as e:
            # FR-030: keep the structured outcome — it names the
            # project searched and what is available there.
            return format_tool_error(e)
        except Exception:
            return json.dumps({
                "error": f"Model '{model_name}' not found. "
                "Use list_models to see registered models.",
            })

        # Validate check names
        try:
            available_checks = okareo.get_all_checks()
            available_names = {
                _get_attr(c, "name") for c in available_checks
            }
        except Exception as e:
            return format_tool_error(e)

        for check_name in checks:
            if check_name not in available_names:
                return json.dumps({
                    "error": f"Check '{check_name}' not found. "
                    "Use list_checks to see available checks.",
                })

        # Validate test run type
        try:
            test_run_type = TestRunType(type)
        except ValueError:
            valid_types = [t.value for t in TestRunType]
            return json.dumps({
                "error": f"Invalid test run type '{type}'. "
                f"Valid values: {', '.join(valid_types)}.",
            })

        # Get provider keys from lifespan context + SSE headers
        key_registry = {}
        if ctx and hasattr(ctx, "request_context"):
            lifespan_ctx = getattr(ctx.request_context, "lifespan_context", None)
            if lifespan_ctx and isinstance(lifespan_ctx, dict):
                key_registry = dict(lifespan_ctx.get("key_registry", {}))

        # Run the test through the faux-async buffer (spec 025): run_test blocks
        # until the backend finishes, so run it on a background thread and hand the
        # co-pilot a pollable id within the buffer window. The run survives the
        # early return and finishes on its own (see specs/025/research.md).
        from src.tools.simulations import _build_handoff_response, _buffered_submit

        run_name = name or f"{scenario_name}-{model_name}"
        test_kwargs = dict(
            scenario=scenario,
            name=run_name,
            test_run_type=test_run_type,
            checks=checks,
        )
        if key_registry:
            test_kwargs["api_keys"] = key_registry

        def submit_thunk(_kwargs=test_kwargs):
            return mut.run_test(**_kwargs)

        status, payload_obj, run_id, app_link = _buffered_submit(
            submit_thunk,
            okareo=okareo,
            project_id=project_id,
            scenario_set_id=_get_attr(scenario, "scenario_id"),
            name=run_name,
            types=[test_run_type],
        )
        if status == "failed":
            return format_tool_error(payload_obj, key_registry)

        extra = {
            "scenario": scenario_name,
            "model": model_name,
            "type": getattr(test_run_type, "value", str(test_run_type)),
        }
        response = _build_handoff_response(
            status, payload_obj, run_id, app_link,
            name=run_name,
            project_id=project_id,
            estimate_seconds=None,  # single-turn tests: no conversation estimate
            based_on_run_id=None,
            extra=extra,
            noun="Test",
            transcript_hint=False,
        )
        return json.dumps(response, default=str)

    @mcp.tool(
        title="List Test Runs",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @project_scoped
    def list_test_runs(
        model_name: Optional[str] = None,
        scenario_name: Optional[str] = None,
        limit: int = 10,
        offset: int = 0,
        simulation_only: bool = False,
        detail_level: str = "summary",
        project: Annotated[Optional[str], Field(description=PROJECT_PARAM_DESC)] = None,
    ) -> str:
        """Find a test run in the project, most recent first.

        Two depths, because finding a run and comparing runs are different jobs:

        - "summary" (default): identity, type, status, conversation count,
          timings, and what each run executed against (target, scenario,
          driver). Enough to find the run you mean.
        - "detailed": adds aggregate metrics, the app link, and the provenance
          identifiers. Use it to compare runs without opening any of them.

        Neither depth returns per-conversation scores — use
        get_test_run_results for those.

        Args:
            model_name: Optional filter — only show test runs using this model.
            scenario_name: Optional filter — only show test runs using this scenario.
            limit: Maximum number of runs to return, sorted by most recent first.
                Defaults to 10. Set to 0 to return all runs.
            offset: Number of runs to skip. Defaults to 0.
            simulation_only: When True, return only MULTI_TURN simulation runs.
                Useful for browsing past simulation results without NL_GENERATION or
                other test run types appearing in the list.
            detail_level: "summary" (default) or "detailed".
        """
        invalid = validate_depth(detail_level, DEPTHS_LISTING)
        if invalid is not None:
            return invalid
        detailed = detail_level == DETAILED
        from okareo_api_client.api.default import (
            get_scenario_sets_v0_scenario_sets_get,
        )
        from okareo_api_client.models.general_find_payload import GeneralFindPayload
        from okareo_api_client.models.test_run_type import TestRunType

        try:
            okareo = get_okareo_client()
            project_id = resolve_project(okareo, project).id
        except Exception as e:
            return format_tool_error(e)

        # Build filter payload.
        #
        # return_model_metrics=False does not blank the metrics: server-side it drops
        # only ROW_METRICS_KEYS (scores_by_row / scores_by_label / row_level_metrics)
        # and keeps mean_scores, percentile_scores, aggregate_* and check_ids. Those
        # row-level entries carry a written explanation per check per row, which is
        # essentially the entire response body -- measured at 2.82 MiB vs 0.40 MiB
        # over 410 runs. find_test_runs has no limit parameter, so the caller cannot
        # bound the row count either, and a large enough project exceeds Cloud Run's
        # response cap and fails the whole request with a bare HTTP 500.
        #
        # A list view shows aggregates. Per-row scores come from get_test_run_results.
        payload_kwargs: dict = {
            "project_id": project_id,
            "return_model_metrics": False,
        }

        if simulation_only:
            payload_kwargs["types"] = [TestRunType.MULTI_TURN]

        # Resolve model_name to mut_id if provided
        if model_name is not None:
            try:
                mut = resolve_artifact_by_name(
                    okareo, model_name, project_id, kind="model"
                )
                payload_kwargs["mut_id"] = _get_attr(mut, "id", "")
            except ArtifactNotInProject as e:
                # FR-030: keep the structured outcome — it names the
                # project searched and what is available there.
                return format_tool_error(e)
            except Exception:
                return json.dumps({
                    "error": f"Model '{model_name}' not found. "
                    "Use list_models to see registered models.",
                })

        # Resolve scenario_name to scenario_set_id if provided
        if scenario_name is not None:
            try:
                scenarios = get_scenario_sets_v0_scenario_sets_get.sync(
                    client=okareo.client,
                    project_id=project_id,
                    api_key=okareo.api_key,
                )
                scenario_id = None
                if scenarios and not isinstance(scenarios, Exception):
                    for s in scenarios:
                        if _get_attr(s, "name") == scenario_name:
                            scenario_id = _get_attr(s, "scenario_id")
                            break
                if scenario_id is None:
                    return json.dumps({
                        "error": f"Scenario '{scenario_name}' not found. "
                        "Use list_scenarios to see available scenarios.",
                    })
                payload_kwargs["scenario_set_id"] = scenario_id
            except Exception as e:
                return format_tool_error(e)

        # Find test runs
        try:
            payload = GeneralFindPayload(**payload_kwargs)
            runs = find_test_runs(okareo, payload)
        except UnexpectedStatus as e:
            if e.status_code == 200:
                runs = json.loads(e.content)
            else:
                return format_tool_error(e)
        except Exception as e:
            return format_tool_error(e)

        if not runs or isinstance(runs, Exception):
            return json.dumps({
                "test_runs": [],
                "count": 0,
                "message": "No test runs found.",
            })

        # Format results — runs may be raw dicts from the low-level API
        result = []
        for run in runs:
            if isinstance(run, dict):
                entry = {
                    "id": run.get("id", ""),
                    "name": run.get("name", ""),
                    "type": run.get("type", ""),
                    "status": run.get("status", ""),
                    "test_data_point_count": run.get("test_data_point_count", 0),
                    "start_time": run.get("start_time"),
                    "end_time": run.get("end_time"),
                }
            else:
                entry = {
                    "id": _get_attr(run, "id", ""),
                    "name": _get_attr(run, "name", ""),
                    "type": _get_attr(run, "type", ""),
                    "status": _get_attr(run, "status", ""),
                    "test_data_point_count": _get_attr(
                        run, "test_data_point_count", 0
                    ),
                    "start_time": _serialize_datetime(
                        _get_attr(run, "start_time")
                    ),
                    "end_time": _serialize_datetime(
                        _get_attr(run, "end_time")
                    ),
                }

            # What the run executed against. Absent from every listing before
            # this feature, which made a comparison of two runs impossible to
            # qualify (FR-025, FR-029).
            entry.update(
                resolve_run_provenance(
                    okareo, run, project_id, include_ids=detailed
                )
            )

            if detailed:
                entry["model_metrics"] = (
                    run.get("model_metrics") if isinstance(run, dict)
                    else _serialize_metrics(_get_attr(run, "model_metrics"))
                )
                entry["app_link"] = (
                    run.get("app_link", "") if isinstance(run, dict)
                    else _get_attr(run, "app_link", "")
                )
            result.append(entry)

        # Sort by start_time descending (most recent first)
        result.sort(key=lambda r: r.get("start_time") or "", reverse=True)

        total_count = len(result)
        start, stop, has_more = page_window(total_count, limit, offset)
        page = result[start:stop]

        response = {
            "test_runs": page,
            "count": len(page),
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
            "detail_level": detail_level,
        }
        if has_more:
            response["next_step"] = (
                f"list_test_runs(limit={limit}, offset={offset + limit}, "
                f"detail_level='{detail_level}')"
            )
        elif not detailed:
            response["next_step"] = (
                "list_test_runs(detail_level='detailed') for aggregate metrics"
            )
        return json.dumps(response, default=str)

    @mcp.tool(
        title="Get Test Run Results",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @project_scoped
    def get_test_run_results(
        test_run_id: Optional[str] = None,
        name: Optional[str] = None,
        detail_level: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        include_transcripts: bool = False,
        project: Annotated[Optional[str], Field(description=PROJECT_PARAM_DESC)] = None,
    ) -> str:
        """Load the results of a specific test run, one page at a time.

        Look up by test run ID (UUID) or by name (returns the most recent run
        matching that name). Both lookups return the same shape.

        A test run is a container of conversations, so this reads like a list:
        it returns a bounded page, not the whole run. Three depths:

        - "summary" (default): run aggregates, provenance, and each
          conversation's whole scenario input and result. No per-check
          outcomes, no explanations, no transcripts.
        - "detailed": adds the row-level metrics block, which carries
          per-check outcomes AND their written explanations. This is where you
          find out which conversations failed and why.
        - "full": adds conversation transcripts and any media.

        Use get_conversation_transcript to inspect one conversation without
        raising the depth for the whole page.

        Every response carries a `rerun` block: the run_simulation call that
        re-runs this simulation, and every configuration value that call will
        inherit, already in the parameter shape the tool takes. To re-run with
        a change — a different driver, an added check, background noise — pass
        that one parameter alongside based_on_run_id and the rest carries over.

        Args:
            test_run_id: The UUID of the test run. Takes precedence over name.
            name: The name of the test run. Returns the most recent match.
            detail_level: "summary" (default), "detailed", or "full".
            limit: Conversations per page. Defaults to 20. Use 0 for all.
            offset: Number of conversations to skip. Defaults to 0.
            include_transcripts: DEPRECATED — use detail_level="full". Retained
                for one release; detail_level wins when both are supplied.
        """
        from okareo_api_client.models.find_test_data_point_payload import (
            FindTestDataPointPayload,
        )
        from okareo_api_client.models.general_find_payload import GeneralFindPayload

        # A bare `include_transcripts=True` has to mean "full", but an explicit
        # detail_level must still win (FR-010a). Those are only distinguishable
        # if the default is None rather than "summary".
        deprecation = None
        if include_transcripts:
            deprecation = (
                "include_transcripts is deprecated and will be removed after the "
                "next release; use detail_level='full'."
            )
            if detail_level is None:
                detail_level = FULL
            else:
                deprecation += (
                    f" detail_level={detail_level!r} was supplied as well and takes "
                    "precedence."
                )
        depth = detail_level or SUMMARY

        invalid = validate_depth(depth, DEPTHS_RUN)
        if invalid is not None:
            return invalid

        if not test_run_id and not name:
            return json.dumps({
                "error": "Provide either test_run_id or name to look up a test run.",
            })

        lookup_by = "id" if test_run_id else "name"

        try:
            okareo = get_okareo_client()
            project_id = resolve_project(okareo, project).id
        except Exception as e:
            return format_tool_error(e)

        annotate(
            project_id=project_id,
            lookup_by=lookup_by,
            detail_level=depth,
            # Kept alongside detail_level for the deprecation window so
            # existing analytics keyed on the boolean stay comparable.
            include_transcripts=(depth == FULL),
        )

        def _fetch_run(**kw):
            try:
                runs = find_test_runs(
                    okareo, GeneralFindPayload(project_id=project_id, **kw)
                )
            except UnexpectedStatus as e:
                if e.status_code != 200:
                    raise
                runs = json.loads(e.content)
            return runs if isinstance(runs, list) else None

        resolved_id = test_run_id

        # Resolving a name means matching client-side over every run in the
        # project -- GeneralFindPayload has no name filter and no limit. That
        # sweep must never ask for row metrics: it would attach an explanation
        # per check per row to every run in the project and 500 a large one.
        # The depth the caller asked for is served by a second, id-bounded
        # fetch below, so both lookup paths end up identical (research R3).
        if not resolved_id and name:
            try:
                everything = _fetch_run(return_model_metrics=False)
            except Exception as e:
                return format_tool_error(e)
            matches = [
                r for r in (everything or [])
                if (r.get("name") if isinstance(r, dict) else _get_attr(r, "name"))
                == name
            ]
            if not matches:
                return json.dumps({
                    "error": f"No test run named '{name}' found. "
                    "Use list_test_runs to find available test runs.",
                })
            matches.sort(
                key=lambda r: (
                    r.get("start_time", "") if isinstance(r, dict)
                    else str(_get_attr(r, "start_time", ""))
                ),
                reverse=True,
            )
            best = matches[0]
            resolved_id = (
                best.get("id") if isinstance(best, dict)
                else _get_attr(best, "id", "")
            )

        try:
            found = _fetch_run(id=resolved_id, return_model_metrics=False)
        except Exception as e:
            return format_tool_error(e)
        if not found:
            return json.dumps({
                "error": f"Test run with ID '{resolved_id}' not found. "
                "Use list_test_runs to find available test runs.",
            })
        raw_run = found[0]

        if resolved_id:
            annotate(entity_type="test_run", entity_id=str(resolved_id))

        try:
            data_points = okareo.find_test_data_points(
                FindTestDataPointPayload(
                    test_run_id=resolved_id,
                    full_data_point=True,
                )
            )
        except Exception as e:
            return format_tool_error(e)
        if not isinstance(data_points, list):
            data_points = []

        index_map = _scenario_index_map(
            raw_run if isinstance(raw_run, dict) else None, data_points
        )

        # One list to page. Per-row verdicts ride on the data points
        # themselves, so there is no second list to keep aligned.
        total_count = len(data_points)
        start, stop, has_more = page_window(total_count, limit, offset)

        dp_list = []
        for dp in data_points[start:stop]:
            dp_id = _data_point_id(dp)
            metric = _serialize_value(_get_attr(dp, "metric_value"))
            # generation_output carries the whole transcript, so it must not
            # ride into a depth that excludes transcripts.
            if (
                depth != FULL
                and isinstance(metric, dict)
                and "generation_output" in metric
            ):
                metric = {
                    k: v for k, v in metric.items() if k != "generation_output"
                }

            entry = {
                "scenario_index": index_map.get(dp_id),
                "test_id": dp_id,
                "scenario_input": _serialize_value(_get_attr(dp, "scenario_input")),
                "scenario_result": _serialize_value(_get_attr(dp, "scenario_result")),
                "metric_value": metric,
                "error_message": _get_attr(dp, "error_message"),
            }
            if depth in (DETAILED, FULL):
                # This row's own Check values and judge explanations, correctly
                # paired to this conversation (041 FR-001). The judged prose is
                # the bulk of a row, which is why it starts at `detailed`.
                entry["checks"] = _serialize_value(_get_attr(dp, "checks"))
            if depth == FULL:
                entry["model_input"] = _serialize_value(_get_attr(dp, "model_input"))
                entry["model_result"] = _serialize_value(_get_attr(dp, "model_result"))
            dp_list.append(entry)

        run_envelope = _run_envelope(raw_run, okareo, project_id)
        metrics = run_envelope.get("model_metrics")
        if isinstance(metrics, dict):
            # Never surfaced, at any depth. The run-level row block is not
            # positionally aligned to the data-point list and carries no key
            # to join on (041), so pairing a verdict to a conversation through
            # it is guesswork -- and the backend is removing __explanation
            # from it besides. Per-row verdicts and evidence come from each
            # data point's own `checks`, which is correctly attached and rides
            # on the page. Dropping this also means the run fetch never asks
            # for it, so the saving is on the wire at every depth.
            metrics.pop("scores_by_row", None)
            metrics.pop("scores_by_label", None)
            metrics.pop("row_level_metrics", None)

        annotate(result_count=total_count)

        # The run's action surface: what a re-run would be, and what it would
        # carry (FR-033). Built from the same resolver `based_on_run_id`
        # consumes, so the block is a promise rather than a rendering.
        config = resolve_run_config(raw_run, _run_check_names(raw_run, data_points))

        response = {
            "test_run": run_envelope,
            "rerun": rerun_block(str(resolved_id), config),
            "data_points": dp_list,
            "data_point_count": len(dp_list),
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
            "detail_level": depth,
        }
        if deprecation:
            response["deprecation"] = deprecation

        nxt = _next_step_for_run(resolved_id, depth, limit, offset, has_more)
        if nxt:
            response["next_step"] = nxt

        return json.dumps(response, default=str)

    def _dropped_turns(data_point: Any) -> dict:
        """Turns the dropout augmentation silenced, when there were any.

        A dropped turn leaves no message in the transcript by design, so
        without this a silenced turn cannot be told apart from one that never
        happened. Omitted when empty, which is every run without dropout.
        """
        raw = _get_attr(data_point, "model_metadata")
        meta = raw.to_dict() if hasattr(raw, "to_dict") else raw
        if not isinstance(meta, dict):
            return {}
        dropped = meta.get("dropped_turns")
        if not isinstance(dropped, list) or not dropped:
            return {}
        return {"dropped_turns": dropped}

    @mcp.tool(
        title="Get Conversation Transcript",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @project_scoped
    def get_conversation_transcript(
        test_run_id: str,
        scenario_index: Optional[int] = None,
        test_id: Optional[str] = None,
        project: Annotated[Optional[str], Field(description=PROJECT_PARAM_DESC)] = None,
    ) -> str:
        """Retrieve the full conversation transcript for a single data point.

        Use this after get_test_run_results to drill into a specific
        conversation. Provide either scenario_index (1-based, from the
        scores summary) or test_id (UUID) to identify the conversation.

        Returns the complete message transcript (model_input), final
        output (model_result), per-turn check scores (metric_value),
        and the scenario seed data.

        Args:
            test_run_id: The UUID of the test run.
            scenario_index: 1-based index of the conversation within
                the test run. Visible in get_test_run_results output.
            test_id: UUID of the specific data point. Alternative to
                scenario_index.
        """
        from okareo_api_client.models.find_test_data_point_payload import (
            FindTestDataPointPayload,
        )

        if scenario_index is None and test_id is None:
            return json.dumps({
                "error": "Provide either scenario_index or test_id to "
                "identify the conversation.",
            })

        lookup_by = "id" if test_id else "index"

        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        annotate(
            entity_type="test_run",
            entity_id=str(test_run_id),
            lookup_by=lookup_by,
        )

        # Fetch run metadata for the name and scenario_index mapping
        run_name = ""
        run_metadata = None
        project_id = None
        try:
            from okareo_api_client.models.general_find_payload import (
                GeneralFindPayload,
            )
            project_id = resolve_project(okareo, project).id
            annotate(project_id=project_id)
            payload = GeneralFindPayload(
                id=test_run_id,
                project_id=project_id,
                return_model_metrics=True,
            )
            try:
                runs = find_test_runs(okareo, payload)
            except UnexpectedStatus as ue:
                runs = (
                    json.loads(ue.content) if ue.status_code == 200 else None
                )
            if runs and not isinstance(runs, Exception) and len(runs) > 0:
                r = runs[0]
                if isinstance(r, dict):
                    run_name = r.get("name", "")
                    run_metadata = r
                else:
                    run_name = _get_attr(r, "name", "")
                    run_metadata = {
                        "model_metrics": _serialize_metrics(
                            _get_attr(r, "model_metrics")
                        ),
                    }
        except Exception:
            pass  # Non-critical — we can still return the transcript

        # Fetch all data points
        try:
            data_points = okareo.find_test_data_points(
                FindTestDataPointPayload(
                    test_run_id=test_run_id,
                    full_data_point=True,
                )
            )
        except Exception as e:
            return format_tool_error(e)

        if not isinstance(data_points, list) or len(data_points) == 0:
            return json.dumps({
                "error": f"No data points found for test run '{test_run_id}'.",
            })

        # Build scenario_index lookup (same ordering as get_test_run_results)
        index_map = _scenario_index_map(run_metadata, data_points)
        reverse_map = {v: k for k, v in index_map.items()}

        # Resolve scenario_index to test_id for matching
        target_test_id = test_id
        if scenario_index is not None and not target_test_id:
            target_test_id = reverse_map.get(scenario_index)

        # Find the matching data point
        match = None
        for dp in data_points:
            if target_test_id and _data_point_id(dp) == str(target_test_id):
                match = dp
                break

        if match is None:
            if scenario_index is not None:
                all_indices = sorted(index_map.values())
                valid = (
                    f"valid indices {min(all_indices)}-{max(all_indices)}"
                    if all_indices
                    else "no indexable conversations"
                )
                return json.dumps({
                    "error": f"Scenario index {scenario_index} is out of "
                    f"range. This test run has {len(data_points)} "
                    f"conversation(s) ({valid}).",
                })
            return json.dumps({
                "error": f"No data point with test_id '{test_id}' found "
                f"in test run '{test_run_id}'.",
            })

        match_id = _data_point_id(match)
        annotate(data_point_id=match_id)
        return json.dumps({
            "test_run_id": test_run_id,
            "test_run_name": run_name,
            "scenario_index": index_map.get(match_id),
            "test_id": match_id,
            "scenario_input": _serialize_value(
                _get_attr(match, "scenario_input")
            ),
            "scenario_result": _serialize_value(
                _get_attr(match, "scenario_result")
            ),
            "model_input": _serialize_value(
                _get_attr(match, "model_input")
            ),
            "model_result": _serialize_value(
                _get_attr(match, "model_result")
            ),
            "metric_value": _serialize_value(
                _get_attr(match, "metric_value")
            ),
            "error_message": _get_attr(match, "error_message"),
            **_dropped_turns(match),
        }, default=str)

    @mcp.tool(
        title="Re-evaluate Test Run",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    @project_scoped
    def reevaluate_test_run(
        test_run_id: str,
        checks: Optional[list[str]] = None,
        project: Annotated[Optional[str], Field(description=PROJECT_PARAM_DESC)] = None,
    ) -> str:
        """Re-score a completed test run against a set of checks.

        Re-runs checks against an already-finished test run without re-executing
        the original model or simulation, and without changing the original
        run's results. Useful after a check definition changed, or to score an
        existing run against additional checks.

        Args:
            test_run_id: UUID or name of a completed test run.
            checks: Optional list of check names (or IDs) to score against.
                When omitted, the run's existing checks are re-run.
        """
        if not test_run_id:
            return json.dumps({"error": "test_run_id is required."})

        try:
            okareo = get_okareo_client()
            project_id = resolve_project(okareo, project).id
        except Exception as e:
            return format_tool_error(e)

        run = _find_test_run(okareo, project_id, test_run_id)
        if run is None:
            return json.dumps({
                "error": f"Test run '{test_run_id}' not found. "
                "Use list_test_runs to find available test runs.",
            })
        resolved_id = (
            run.get("id") if isinstance(run, dict) else _get_attr(run, "id")
        )
        status = (
            run.get("status") if isinstance(run, dict)
            else _get_attr(run, "status")
        ) or ""
        if str(status).upper() in _NON_TERMINAL_STATUSES:
            return json.dumps({
                "error": (
                    f"Test run '{test_run_id}' is not complete "
                    f"(status: {status}). Re-evaluation requires a finished "
                    "test run."
                ),
            })

        # Build the name→id / id-set lookup for resolving the check list.
        try:
            check_briefs = okareo.get_all_checks()
        except Exception as e:
            return format_tool_error(e)
        name_to_id: dict = {}
        id_set: set = set()
        for c in check_briefs:
            cid = str(_get_attr(c, "id", "") or "")
            cname = _get_attr(c, "name", "")
            if cid:
                id_set.add(cid)
                if cname:
                    name_to_id[cname] = cid

        check_ids: list = []
        if checks:
            unknown: list = []
            for ch in checks:
                if ch in id_set:
                    check_ids.append(ch)
                elif ch in name_to_id:
                    check_ids.append(name_to_id[ch])
                else:
                    unknown.append(ch)
            if unknown:
                return json.dumps({
                    "error": f"Unknown check(s): {unknown}. "
                    "Use list_checks to see available checks.",
                })
        else:
            check_ids = _derive_run_check_ids(okareo, resolved_id, name_to_id)
            if not check_ids:
                return json.dumps({
                    "error": (
                        "Could not determine this run's checks automatically. "
                        "Pass an explicit `checks` list (see list_checks)."
                    ),
                })

        try:
            result = okareo.re_evaluate(str(resolved_id), check_ids)
        except Exception as e:
            return format_tool_error(e)

        return json.dumps({
            "test_run_id": str(resolved_id),
            "reevaluated_check_ids": check_ids,
            "original_run_unchanged": True,
            "result": _serialize_value(result),
            "message": (
                f"Re-evaluated test run against {len(check_ids)} check(s). "
                "The original run's results are unchanged."
            ),
        }, default=str)

    return None
