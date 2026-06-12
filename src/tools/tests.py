"""Test run tools for the Okareo MCP server.

Provides five MCP tools for the core test execution workflow:

- list_checks: Browse available quality checks that can evaluate model outputs
- run_test: Execute a test by evaluating a model against a scenario with checks
- list_test_runs: Browse past test runs with optional filters
- get_test_run_results: Load per-row scores of a specific test run (transcripts opt-in)
- get_conversation_transcript: Retrieve the full transcript for a single conversation
"""

import json
from typing import Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from okareo_api_client.errors import UnexpectedStatus

from src.error_handling import format_tool_error
from src.okareo_client import (
    find_test_runs,
    get_okareo_client,
    okareo_api_request,
    resolve_project_id,
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
    def list_checks(limit: int = 20, all_versions: bool = False) -> str:
        """List available quality checks that can be used to evaluate model outputs.

        Returns checks (both built-in and custom) available in your Okareo account.
        Each check has a name, description, and output_data_type. output_data_type
        uses the server vocabulary: "bool" is a pass/fail check and "int" is a
        scored check — these correspond to output_type "pass_fail" and "score" in
        create_or_update_check and generate_check.
        Use these check names with run_test to evaluate model quality.

        Args:
            limit: Maximum number of checks to return (default 20). Use 0 for no limit.
            all_versions: When false (default), returns only the latest version of
                each check. When true, returns the full version history of every
                check, each entry annotated with its version number.
        """
        try:
            okareo = get_okareo_client()
            checks = okareo.get_all_checks(all_versions=all_versions)
        except Exception as e:
            return format_tool_error(e)

        if not checks:
            return json.dumps({
                "checks": [],
                "count": 0,
                "message": "No checks available.",
            })

        result = []
        for check in checks:
            entry = {
                "name": _get_attr(check, "name", ""),
                "description": _get_attr(check, "description", ""),
                "output_data_type": _get_attr(check, "output_data_type", ""),
            }
            if all_versions:
                props = getattr(check, "additional_properties", None)
                if isinstance(props, dict) and isinstance(props.get("version"), int):
                    entry["version"] = props["version"]
            result.append(entry)

        total = len(result)
        if limit and limit > 0:
            result = result[:limit]

        return json.dumps({
            "checks": result,
            "count": len(result),
            "total": total,
        }, default=str)

    @mcp.tool(
        title="Run Test",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    def run_test(
        scenario_name: str,
        model_name: str,
        checks: list[str],
        name: Optional[str] = None,
        type: str = "NL_GENERATION",
        ctx: Context = None,
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
                Use list_checks to discover available checks.
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
            project_id = resolve_project_id(okareo)
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
            mut = okareo.get_model(name=model_name)
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
    def list_test_runs(
        model_name: Optional[str] = None,
        scenario_name: Optional[str] = None,
        limit: int = 10,
        simulation_only: bool = False,
    ) -> str:
        """List past test runs in the project.

        Returns test run names, IDs, timestamps, status, and summary scores,
        sorted by most recent first. Defaults to the 10 most recent runs.
        Optionally filter by model name, scenario name, or type.

        For simulation runs (type MULTI_TURN), use get_test_run_results with the
        returned test_run_id to retrieve full conversation transcripts and per-turn
        check scores.

        Args:
            model_name: Optional filter — only show test runs using this model.
            scenario_name: Optional filter — only show test runs using this scenario.
            limit: Maximum number of runs to return, sorted by most recent first.
                Defaults to 10. Set to 0 to return all runs.
            simulation_only: When True, return only MULTI_TURN simulation runs.
                Useful for browsing past simulation results without NL_GENERATION or
                other test run types appearing in the list.
        """
        from okareo_api_client.api.default import (
            get_scenario_sets_v0_scenario_sets_get,
        )
        from okareo_api_client.models.general_find_payload import GeneralFindPayload
        from okareo_api_client.models.test_run_type import TestRunType

        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        # Build filter payload
        payload_kwargs: dict = {
            "project_id": project_id,
            "return_model_metrics": True,
        }

        if simulation_only:
            payload_kwargs["types"] = [TestRunType.MULTI_TURN]

        # Resolve model_name to mut_id if provided
        if model_name is not None:
            try:
                mut = okareo.get_model(name=model_name)
                payload_kwargs["mut_id"] = mut.mut_id
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
                result.append({
                    "id": run.get("id", ""),
                    "name": run.get("name", ""),
                    "type": run.get("type", ""),
                    "status": run.get("status", ""),
                    "test_data_point_count": run.get("test_data_point_count", 0),
                    "model_metrics": run.get("model_metrics"),
                    "start_time": run.get("start_time"),
                    "end_time": run.get("end_time"),
                    "app_link": run.get("app_link", ""),
                })
            else:
                result.append({
                    "id": _get_attr(run, "id", ""),
                    "name": _get_attr(run, "name", ""),
                    "type": _get_attr(run, "type", ""),
                    "status": _get_attr(run, "status", ""),
                    "test_data_point_count": _get_attr(
                        run, "test_data_point_count", 0
                    ),
                    "model_metrics": _serialize_metrics(
                        _get_attr(run, "model_metrics")
                    ),
                    "start_time": _serialize_datetime(
                        _get_attr(run, "start_time")
                    ),
                    "end_time": _serialize_datetime(
                        _get_attr(run, "end_time")
                    ),
                    "app_link": _get_attr(run, "app_link", ""),
                })

        # Sort by start_time descending (most recent first)
        result.sort(key=lambda r: r.get("start_time") or "", reverse=True)

        # Apply limit (0 = return all)
        if limit > 0:
            result = result[:limit]

        return json.dumps(
            {"test_runs": result, "count": len(result)}, default=str
        )

    @mcp.tool(
        title="Get Test Run Results",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def get_test_run_results(
        test_run_id: Optional[str] = None,
        name: Optional[str] = None,
        include_transcripts: bool = False,
        limit: int = 0,
        offset: int = 0,
    ) -> str:
        """Load the results of a specific test run.

        Look up by test run ID (UUID) or by name (returns the most recent run
        matching that name). Returns aggregate metrics and per-row check scores.

        By default, conversation transcripts (model_input/model_result) are
        excluded to keep responses concise. Set include_transcripts=True to
        include full transcripts. Use get_conversation_transcript to inspect
        a single conversation's transcript without loading all of them.

        Supports pagination via limit and offset for large result sets.

        Args:
            test_run_id: The UUID of the test run. Takes precedence over name.
            name: The name of the test run. Returns the most recent match.
            include_transcripts: Include full model_input and model_result in
                each data point. Defaults to False (scores only). Set True for
                full conversation transcripts.
            limit: Maximum number of data points to return. 0 (default) returns
                all data points. Use with offset for pagination.
            offset: Number of data points to skip. Defaults to 0.
        """
        from okareo_api_client.models.find_test_data_point_payload import (
            FindTestDataPointPayload,
        )
        from okareo_api_client.models.general_find_payload import GeneralFindPayload

        if not test_run_id and not name:
            return json.dumps({
                "error": "Provide either test_run_id or name to look up a test run.",
            })

        try:
            okareo = get_okareo_client()
            project_id = resolve_project_id(okareo)
        except Exception as e:
            return format_tool_error(e)

        resolved_id = test_run_id
        run_metadata = None

        # If looking up by name, resolve to most recent test run
        if not test_run_id and name:
            try:
                payload = GeneralFindPayload(
                    project_id=project_id,
                    return_model_metrics=True,
                )
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
                    "error": f"No test run named '{name}' found. "
                    "Use list_test_runs to find available test runs.",
                })

            # Filter by name and find most recent
            matching = []
            for run in runs:
                if isinstance(run, dict):
                    run_name = run.get("name", "")
                    if run_name == name:
                        matching.append(run)
                else:
                    run_name = _get_attr(run, "name", "")
                    if run_name == name:
                        matching.append(run)

            if not matching:
                return json.dumps({
                    "error": f"No test run named '{name}' found. "
                    "Use list_test_runs to find available test runs.",
                })

            # Sort by start_time descending, take most recent
            def _get_start_time(r):
                if isinstance(r, dict):
                    return r.get("start_time", "")
                return str(_get_attr(r, "start_time", ""))

            matching.sort(key=_get_start_time, reverse=True)
            best = matching[0]

            if isinstance(best, dict):
                resolved_id = best.get("id", "")
                run_metadata = best
            else:
                resolved_id = _get_attr(best, "id", "")
                run_metadata = {
                    "id": _get_attr(best, "id", ""),
                    "name": _get_attr(best, "name", ""),
                    "type": _get_attr(best, "type", ""),
                    "status": _get_attr(best, "status", ""),
                    "test_data_point_count": _get_attr(
                        best, "test_data_point_count", 0
                    ),
                    "model_metrics": _serialize_metrics(
                        _get_attr(best, "model_metrics")
                    ),
                    "start_time": _serialize_datetime(
                        _get_attr(best, "start_time")
                    ),
                    "end_time": _serialize_datetime(
                        _get_attr(best, "end_time")
                    ),
                    "app_link": _get_attr(best, "app_link", ""),
                }

        # If we have a test_run_id but no metadata yet, fetch it
        if run_metadata is None:
            try:
                payload = GeneralFindPayload(
                    id=resolved_id,
                    project_id=project_id,
                    return_model_metrics=True,
                )
                try:
                    runs = find_test_runs(okareo, payload)
                except UnexpectedStatus as ue:
                    runs = json.loads(ue.content) if ue.status_code == 200 else None
                if runs and not isinstance(runs, Exception) and len(runs) > 0:
                    r = runs[0]
                    if isinstance(r, dict):
                        run_metadata = r
                    else:
                        run_metadata = {
                            "id": _get_attr(r, "id", ""),
                            "name": _get_attr(r, "name", ""),
                            "type": _get_attr(r, "type", ""),
                            "status": _get_attr(r, "status", ""),
                            "test_data_point_count": _get_attr(
                                r, "test_data_point_count", 0
                            ),
                            "model_metrics": _serialize_metrics(
                                _get_attr(r, "model_metrics")
                            ),
                            "start_time": _serialize_datetime(
                                _get_attr(r, "start_time")
                            ),
                            "end_time": _serialize_datetime(
                                _get_attr(r, "end_time")
                            ),
                            "app_link": _get_attr(r, "app_link", ""),
                        }
                else:
                    return json.dumps({
                        "error": f"Test run with ID '{test_run_id}' not found. "
                        "Use list_test_runs to find available test runs.",
                    })
            except Exception as e:
                return format_tool_error(e)

        # Fetch per-row data points
        try:
            data_points = okareo.find_test_data_points(
                FindTestDataPointPayload(
                    test_run_id=resolved_id,
                    full_data_point=True,
                )
            )
        except Exception as e:
            return format_tool_error(e)

        # Build scenario_index lookup from run metadata
        index_map = _build_scenario_index_map(run_metadata)

        dp_list = []
        if isinstance(data_points, list):
            for dp in data_points:
                # Resolve scenario_index via test_id
                dp_id = str(
                    _get_attr(dp, "id") or _get_attr(dp, "test_id") or ""
                )
                scenario_idx = index_map.get(dp_id)

                metric = _serialize_value(_get_attr(dp, "metric_value"))
                # Strip generation_output (contains full transcript) when
                # transcripts are not requested
                if (
                    not include_transcripts
                    and isinstance(metric, dict)
                    and "generation_output" in metric
                ):
                    metric = {
                        k: v
                        for k, v in metric.items()
                        if k != "generation_output"
                    }

                entry = {
                    "scenario_index": scenario_idx,
                    "test_id": dp_id,
                    "scenario_input": _serialize_value(
                        _get_attr(dp, "scenario_input")
                    ),
                    "scenario_result": _serialize_value(
                        _get_attr(dp, "scenario_result")
                    ),
                    "metric_value": metric,
                    "error_message": _get_attr(dp, "error_message"),
                }
                if include_transcripts:
                    entry["model_input"] = _serialize_value(
                        _get_attr(dp, "model_input")
                    )
                    entry["model_result"] = _serialize_value(
                        _get_attr(dp, "model_result")
                    )
                dp_list.append(entry)

        # Pagination
        total_count = len(dp_list)
        if limit > 0:
            paginated = dp_list[offset:offset + limit]
            has_more = (offset + limit) < total_count
        else:
            paginated = dp_list[offset:] if offset > 0 else dp_list
            has_more = False

        response = {
            "test_run": run_metadata,
            "data_points": paginated,
            "data_point_count": len(paginated),
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
        }

        return json.dumps(response, default=str)

    @mcp.tool(
        title="Get Conversation Transcript",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def get_conversation_transcript(
        test_run_id: str,
        scenario_index: Optional[int] = None,
        test_id: Optional[str] = None,
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

        try:
            okareo = get_okareo_client()
        except Exception as e:
            return format_tool_error(e)

        # Fetch run metadata for the name and scenario_index mapping
        run_name = ""
        run_metadata = None
        try:
            from okareo_api_client.models.general_find_payload import (
                GeneralFindPayload,
            )
            project_id = resolve_project_id(okareo)
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

        # Build scenario_index lookup
        index_map = _build_scenario_index_map(run_metadata)
        # Also build reverse map: scenario_index → test_id
        reverse_map = {v: k for k, v in index_map.items()}

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

        # Resolve scenario_index to test_id for matching
        target_test_id = test_id
        if scenario_index is not None and not target_test_id:
            target_test_id = reverse_map.get(scenario_index)

        # Find the matching data point
        match = None
        for dp in data_points:
            dp_id = str(
                _get_attr(dp, "id") or _get_attr(dp, "test_id") or ""
            )
            if target_test_id and dp_id == str(target_test_id):
                match = dp
                break

        if match is None:
            if scenario_index is not None:
                all_indices = sorted(index_map.values())
                if all_indices:
                    range_str = f"{min(all_indices)}-{max(all_indices)}"
                else:
                    range_str = "none found"
                return json.dumps({
                    "error": f"Scenario index {scenario_index} is out of "
                    f"range. This test run has {len(data_points)} "
                    f"conversations (indices {range_str}).",
                })
            return json.dumps({
                "error": f"No data point with test_id '{test_id}' found "
                f"in test run '{test_run_id}'.",
            })

        match_id = str(
            _get_attr(match, "id") or _get_attr(match, "test_id") or ""
        )
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
    def reevaluate_test_run(
        test_run_id: str,
        checks: Optional[list[str]] = None,
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
            project_id = resolve_project_id(okareo)
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
            result = okareo_api_request(
                okareo,
                "post",
                f"/v0/test_runs/{resolved_id}/re_evaluate",
                json={"check_ids": check_ids},
            )
        except Exception as e:
            return format_tool_error(e)

        return json.dumps({
            "test_run_id": str(resolved_id),
            "reevaluated_check_ids": check_ids,
            "original_run_unchanged": True,
            "result": result,
            "message": (
                f"Re-evaluated test run against {len(check_ids)} check(s). "
                "The original run's results are unchanged."
            ),
        }, default=str)

    return None
