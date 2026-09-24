"""Resolve a finished run's recorded configuration into re-run parameters (043 US6).

Two callers read this, from opposite directions:

- ``run_simulation``'s ``based_on_run_id`` path, as the defaults a re-run starts
  from, and
- ``get_test_run_results``, as the ``rerun`` block it shows a caller.

They must agree. The block's purpose is to predict what inheritance will do, so
a second implementation would drift in a way that reading either one could not
reveal — which is why both go through this one function and a test asserts the
round trip.

Two properties of the recorded data shape the design:

**The record is sparse.** Only the knobs actually set get recorded, and the set
differs by submission path — a voice run carries ``augmentation`` and
``silence_timeout_ms`` where a text run carries ``checks_at_every_turn`` and
``turn_transition_time``. A missing field is therefore *not* a field set to its
default, and emitting a default for it would assert something about the original
run that was never recorded.

**Not everything recorded can be replayed.** ``concurrent_ask_probability`` is
on the SDK's ``Simulation`` and lands in the record, but ``run_simulation`` has
no parameter for it. Emitting it produces a call the tool rejects; dropping it
silently produces a re-run that differs from its parent with nothing to say so.
It is reported instead.
"""

from typing import Any

# Recorded fields that map one-to-one onto a `run_simulation` parameter.
# `checks` is deliberately absent: it is not in `simulation_params` at all, and
# arrives from the run's `model_metrics.check_ids`.
RERUN_PARAM_FIELDS = (
    "repeats",
    "max_turns",
    "first_turn",
    "stop_check",
    "checks_at_every_turn",
    "turn_transition_time",
    "augmentation",
    "silence_timeout_ms",
)

# Recorded fields with no `run_simulation` parameter, and why. Anything recorded
# and not listed here is reported with a generic reason rather than dropped --
# a knob added to the backend later should surface as "cannot be replayed", not
# vanish.
_UNSETTABLE_REASONS = {
    "concurrent_ask_probability": (
        "recorded on the run but not settable through run_simulation"
    ),
}

# Values that carry no information when reported: a field sitting at the SDK's
# default says nothing about the original run, and listing it on every response
# would train a reader to skip the list that matters.
_UNSETTABLE_DEFAULTS = {
    "concurrent_ask_probability": 0.0,
}


def _simulation_params(run: Any) -> dict:
    if isinstance(run, dict):
        params = run.get("simulation_params")
    else:
        params = getattr(run, "simulation_params", None)
    if type(params).__name__ == "Unset":
        return {}
    if isinstance(params, dict):
        return params
    if hasattr(params, "to_dict"):
        try:
            got = params.to_dict()
            return got if isinstance(got, dict) else {}
        except Exception:
            return {}
    return {}


def resolve_run_config(run: Any, check_names: list) -> dict:
    """Map a run record onto ``run_simulation`` parameters.

    Args:
        run: A run record, as a dict or an SDK model.
        check_names: The run's check names, from ``model_metrics.check_ids``.

    Returns:
        ``{"params": {...}, "unavailable": [{"field", "value", "reason"}]}``.
        ``params`` holds only keys ``run_simulation`` accepts, and omits any
        field the record does not carry. ``unavailable`` names recorded fields
        that cannot be replayed through the tool.
    """
    recorded = _simulation_params(run)

    params: dict = {}
    for field in RERUN_PARAM_FIELDS:
        # Presence, not truthiness: a recorded `stop_check: null` means the run
        # had none, which is a fact worth carrying, while an absent key means
        # nothing was recorded and must stay absent.
        if field in recorded:
            params[field] = recorded[field]

    if check_names:
        params["checks"] = list(check_names)

    unavailable = []
    for field, value in recorded.items():
        if field in RERUN_PARAM_FIELDS:
            continue
        if field in _UNSETTABLE_DEFAULTS and value == _UNSETTABLE_DEFAULTS[field]:
            continue
        unavailable.append({
            "field": field,
            "value": value,
            "reason": _UNSETTABLE_REASONS.get(
                field,
                "recorded on the run but not settable through run_simulation",
            ),
        })

    return {"params": params, "unavailable": unavailable}


def rerun_block(run_id: str, config: dict) -> dict:
    """Render a resolved configuration as the response's ``rerun`` block.

    The block is both the configuration display and the action: the values it
    shows are the values a re-run inherits, so a caller changes one and sends
    it rather than reconstructing a call from prose.
    """
    block: dict = {
        "call": (
            f"run_simulation(name='<new name>', based_on_run_id='{run_id}')"
        ),
        "inherited": config["params"],
        "note": (
            "Pass any key from `inherited` to override that field alone; "
            "everything else carries over."
        ),
    }
    if config["unavailable"]:
        block["unavailable"] = config["unavailable"]
    return block
