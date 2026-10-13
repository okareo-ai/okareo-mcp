"""Start a Run and find the Run each call started.

Two paths, chosen by the Target's type:

- **Submit**, for the Target types the backend queues (server-run text,
  custom endpoints, voice). ``POST /v0/test_run/submit`` answers at once with
  the Run, so its id is known. Its status is then read every few seconds,
  with a timeout on each read and nothing printed, until it ends or the
  buffer window closes.
- **run_test**, for everything else (code-based Targets, single-turn tests).
  ``POST /v0/test_run`` answers only when the Run ends, so it runs on a
  background thread that outlives the call, on a client with no timeout. The
  Run carries a tag unique to the call, and the call finds it by listing the
  Scenario's Runs filtered by that tag. Nothing else identifies it: not its
  name, not which Runs are new.

Either way the outcome is what the tools returned before this module existed
(spec 025): "finished" with the Run, "running" with its id, or "failed" with
the error the tool reports, which is a ``TestRunError`` whichever path failed.
This module builds the ``ModelUnderTest`` itself, on the client its path
needs, and stops its datapoint thread when the Run no longer needs it
(``close_model``: okareo 0.0.159 starts one per model and can stop it only
with a call that prints to stdout).
"""

import contextvars
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Callable, Literal, NamedTuple

import attrs
import httpx
import okareo.model_under_test as _sdk_model_under_test
from okareo import Okareo
from okareo.model_under_test import ModelUnderTest
from okareo_api_client import Client
from okareo_api_client.models.test_run_item import TestRunItem
from okareo_api_client.models.test_run_type import TestRunType
from okareo_api_client.types import Unset

from src.okareo_client import find_test_runs
from src.okareo_sdk_workarounds import close_model

_logger = logging.getLogger(__name__)

# The Target types the backend queues on submit (okareo_server
# execution_routing.route_for, with DISTRIBUTED_VOICE_ON_WORKER_ENABLED and
# WEBRTC_FANOUT_EXECUTION on in production). `custom` routes in-request and
# submit creates a Run nothing executes; `custom_target`s are answered by a
# listener in the caller's process; single-turn tests route in-request, where
# submit takes the background path that stalled at 99% (spec 025 R2).
SUBMIT_TARGET_TYPES = frozenset(
    {"voice", "custom_endpoint", "generation", "openai", "openai_assistant", "cohere"}
)

# `__` marks a system tag in the Okareo app.
CALL_TAG_PREFIX = "__mcp_call:"

POLL_INTERVAL_SECONDS = 2.0
STATUS_READ_TIMEOUT_SECONDS = 5.0
_DEFAULT_WINDOW_SECONDS = 25.0
_FINISHED = "FINISHED"
_FAILED = "FAILED"

RunStatus = Literal["finished", "running", "failed"]


class RunOutcome(NamedTuple):
    """What a start hands back: ``(status, result, run_id, app_link)``.

    ``result`` is the Run when "finished", None when "running", and the
    exception the tool reports when "failed".
    """

    status: RunStatus
    result: TestRunItem | BaseException | None
    run_id: str | None
    app_link: str


class ListedRun(NamedTuple):
    """One Run from a listing of the Scenario's Runs by tag."""

    name: str | None
    app_link: str
    tags: list[str]


def start_run(
    build_model: Callable[[Client], ModelUnderTest],
    *,
    submit: bool,
    run_kwargs: dict[str, Any],
    okareo: Okareo,
    project_id: str,
    scenario_set_id: str,
    types: list[TestRunType] | None = None,
    tags: list[str] | None = None,
    window_seconds: float | None = None,
    poll_seconds: float = POLL_INTERVAL_SECONDS,
    read_status: Callable[[str, float], TestRunItem] | None = None,
    find_runs: Callable[..., dict[str, ListedRun]] | None = None,
) -> RunOutcome:
    """Start a Run with exactly ``run_kwargs`` (plus tags), on the path
    ``submit`` names (see ``submits``).

    ``build_model`` turns a client into the call's ``ModelUnderTest``; this
    function picks the client: the tool's own for submit, one with no timeout
    for run_test, whose request stays open until the Run ends. ``tags`` are
    any tags the call already sets; the run_test path adds the call's own.
    """
    if "tags" in run_kwargs:
        raise ValueError("Pass the Run's tags as `tags`, not inside run_kwargs.")
    window = buffer_window_seconds() if window_seconds is None else window_seconds

    if submit:
        reader = read_status or (
            lambda run_id, timeout: _sdk_model_under_test.fetch_test_run(
                okareo.client, okareo.api_key, run_id, timeout_seconds=timeout
            )
        )
        return _submit_path(
            build_model(okareo.client), run_kwargs, tags, window, poll_seconds, reader
        )

    finder = find_runs or find_runs_by_tag
    run_types = types if types is not None else [TestRunType.MULTI_TURN]
    return _run_test_path(
        build_model,
        run_kwargs,
        tags,
        lambda tag: finder(okareo, project_id, scenario_set_id, run_types, tag),
        run_request_client(okareo),
        window,
        poll_seconds,
    )


def submits(target_type: str | None) -> bool:
    """Whether a Run against this Target type is started with submit."""
    return bool(target_type) and target_type in SUBMIT_TARGET_TYPES


def buffer_window_seconds() -> float:
    """How long a call waits for its Run before handing back a Run id.

    Tunable via OKAREO_SIM_BUFFER_SECONDS; kept under the co-pilot's tool-call
    timeout (Slack gives up at 60 s).
    """
    raw = os.environ.get("OKAREO_SIM_BUFFER_SECONDS", "").strip()
    if raw:
        try:
            value = float(raw)
            if value >= 0:
                return value
        except ValueError:
            pass
    return _DEFAULT_WINDOW_SECONDS


def new_call_tag() -> str:
    return f"{CALL_TAG_PREFIX}{uuid.uuid4()}"


def run_request_client(okareo: Okareo) -> Client:
    """A copy of the tool's client with no timeout, for the request that runs
    a Run: ``ModelUnderTest.run_test`` holds ``POST /v0/test_run`` open until
    the Run ends, minutes to an hour. A copy, not ``Client.with_timeout``,
    which would change the shared HTTP client in place.
    """
    return attrs.evolve(okareo.client, timeout=None)


def find_runs_by_tag(
    okareo: Okareo,
    project_id: str,
    scenario_set_id: str,
    types: list[TestRunType],
    tag: str,
) -> dict[str, ListedRun]:
    """The Scenario's Runs carrying ``tag``, by Run id."""
    from okareo_api_client.errors import UnexpectedStatus
    from okareo_api_client.models.general_find_payload import GeneralFindPayload

    payload = GeneralFindPayload(
        project_id=project_id,
        scenario_set_id=scenario_set_id,
        types=list(types),
        tags=[tag],
        return_model_metrics=False,
    )
    try:
        runs = find_test_runs(okareo, payload)
    except UnexpectedStatus as status:
        runs = json.loads(status.content) if status.status_code == 200 else None
    found: dict[str, ListedRun] = {}
    for run in runs or []:
        rid = _field(run, "id")
        if rid:
            found[str(rid)] = ListedRun(
                name=_field(run, "name"),
                app_link=_field(run, "app_link") or "",
                tags=list(_field(run, "tags") or []),
            )
    return found


def submit_and_poll(
    submit: Callable[[list[str] | None], TestRunItem],
    *,
    tags: list[str] | None,
    window_seconds: float,
    read_status: Callable[[str, float], TestRunItem],
    poll_seconds: float = POLL_INTERVAL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> RunOutcome:
    """Submit the Run, then read its status until it ends or the window closes."""
    try:
        item = submit(tags)
    except Exception as exc:
        # ModelUnderTest.run_test wraps every error in TestRunError and
        # submit_test doesn't; wrap here so the tool's error response is the
        # one it gave before submit existed (FR-005).
        return RunOutcome("failed", _as_test_run_error(exc), None, "")

    rid = _field(item, "id")
    run_id = str(rid) if rid else None
    app_link = _field(item, "app_link") or ""
    if run_id is None:
        return RunOutcome(
            "failed", _sdk_model_under_test.TestRunError("Submit returned no Run id."), None, ""
        )
    deadline = monotonic() + window_seconds
    while True:
        status = _field(item, "status")
        if status == _FINISHED:
            return RunOutcome("finished", item, run_id, app_link)
        if status == _FAILED:
            message = _field(item, "failure_message") or "Test run failed."
            return RunOutcome(
                "failed", _sdk_model_under_test.TestRunError(message), run_id, app_link
            )
        remaining = deadline - monotonic()
        if remaining <= 0:
            return RunOutcome("running", None, run_id, app_link)
        sleep(min(poll_seconds, remaining))
        remaining = max(deadline - monotonic(), 0.0)
        try:
            item = read_status(
                run_id, min(STATUS_READ_TIMEOUT_SECONDS, max(remaining, 0.5))
            )
        except Exception:
            # A read that fails or times out says nothing about the Run; the
            # next poll tries again until the window closes.
            _logger.debug("Status read for run %s failed; retrying", run_id, exc_info=True)


def run_and_find(
    run: Callable[[list[str]], TestRunItem],
    *,
    tags: list[str],
    call_tag: str,
    find_runs: Callable[[str], dict[str, ListedRun]],
    window_seconds: float,
    poll_seconds: float = POLL_INTERVAL_SECONDS,
    on_done: Callable[[], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> RunOutcome:
    """Run on a background thread; find the Run by the call's tag.

    The thread outlives the call when the Run outlasts the window, and
    ``on_done`` runs when the Run itself ends. The thread starts in a copy of
    the caller's context, so its log lines carry the caller's call id.
    """
    holder: dict[str, Any] = {}

    def _runner() -> None:
        try:
            holder["result"] = run(tags)
        except BaseException as exc:  # surfaced to the caller inline
            holder["error"] = exc
        finally:
            if on_done is not None:
                try:
                    on_done()
                except Exception:
                    _logger.warning("Closing the Run's model failed", exc_info=True)

    thread = threading.Thread(
        target=contextvars.copy_context().run,
        args=(_runner,),
        name=f"okareo-run-{call_tag[-8:]}",
        daemon=True,
    )
    thread.start()
    found = _wait_for_run(
        thread, find_runs, call_tag, monotonic() + window_seconds, poll_seconds, monotonic
    )
    run_id = found[0] if found else None
    app_link = found[1].app_link if found else ""

    if thread.is_alive():
        return RunOutcome("running", None, run_id, app_link)
    if "error" in holder:
        return RunOutcome("failed", holder["error"], run_id, app_link)
    result = holder.get("result")
    rid = _field(result, "id")
    return RunOutcome(
        "finished",
        result,
        run_id or (str(rid) if rid else None),
        app_link or _field(result, "app_link") or "",
    )


def _submit_path(
    mut: ModelUnderTest,
    run_kwargs: dict[str, Any],
    tags: list[str] | None,
    window: float,
    poll_seconds: float,
    read_status: Callable[[str, float], TestRunItem],
) -> RunOutcome:
    try:
        return submit_and_poll(
            lambda run_tags: mut.submit_test(**run_kwargs, tags=run_tags),
            tags=list(tags) if tags else None,
            window_seconds=window,
            poll_seconds=poll_seconds,
            read_status=read_status,
        )
    finally:
        close_model(mut)


def _run_test_path(
    build_model: Callable[[Client], ModelUnderTest],
    run_kwargs: dict[str, Any],
    tags: list[str] | None,
    find_runs: Callable[[str], dict[str, ListedRun]],
    run_client: Client,
    window: float,
    poll_seconds: float,
) -> RunOutcome:
    mut = build_model(run_client)

    def _done() -> None:
        close_model(mut)
        run_client.get_httpx_client().close()

    call_tag = new_call_tag()
    return run_and_find(
        lambda run_tags: mut.run_test(**run_kwargs, tags=run_tags),
        tags=[*(tags or []), call_tag],
        call_tag=call_tag,
        find_runs=find_runs,
        window_seconds=window,
        poll_seconds=poll_seconds,
        on_done=_done,
    )


def _wait_for_run(
    thread: threading.Thread,
    find_runs: Callable[[str], dict[str, ListedRun]],
    call_tag: str,
    deadline: float,
    poll_seconds: float,
    monotonic: Callable[[], float],
) -> tuple[str, ListedRun] | None:
    """Wait until the Run thread ends or the deadline passes, looking for the
    call's Run by its tag meanwhile. A listed Run counts only if its own tags
    include the call's tag."""
    found: tuple[str, ListedRun] | None = None
    while True:
        thread.join(timeout=poll_seconds)
        if not thread.is_alive():
            return found
        if found is None:
            try:
                listing = find_runs(call_tag)
            except Exception:
                listing = {}
            mine = [(rid, r) for rid, r in listing.items() if call_tag in r.tags]
            if len(mine) == 1:
                found = mine[0]
        if monotonic() >= deadline:
            return found


def _as_test_run_error(exc: Exception) -> Exception:
    # A timeout keeps the timeout error every other tool reports in HTTP mode
    # (spec edge cases); run_test never timed out, its client had no limit.
    if isinstance(exc, (_sdk_model_under_test.TestRunError, httpx.TimeoutException)):
        return exc
    wrapped = _sdk_model_under_test.TestRunError(str(exc))
    wrapped.__cause__ = exc
    return wrapped


def _field(item: Any, name: str, default: Any = None) -> Any:
    value = item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)
    return default if isinstance(value, Unset) else value
