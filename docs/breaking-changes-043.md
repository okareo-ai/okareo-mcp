# Breaking changes: tiered tool responses (043)

Every default below changed in one release. Each is restorable per call — no
redeploy, no configuration.

The reason for a single flip rather than a staged one: the previous defaults
were unbounded, which is the defect being corrected rather than a contract to
preserve. A staged rollout would have spread the same breakage over two
releases while leaving token exhaustion unfixed in the first.

## `get_test_run_results`

| | Before | After |
|---|---|---|
| `limit` | `0` — every conversation | `20` |
| Per-check outcomes and explanations | Run-level `scores_by_row` | Each row's own `checks`, from `detail_level="detailed"` |
| Transcripts | `include_transcripts=True` | `detail_level="full"` |
| Run fields | Raw passthrough (23 fields) | Defined set |

**Restore the previous response**: `limit=0, detail_level="full"`.

`include_transcripts` still works as a deprecated alias for
`detail_level="full"` and will be removed after the next release. When both are
supplied, `detail_level` wins.

**Why the page bound is new behaviour rather than a fix to an existing one**:
for multi-turn runs `limit` and `offset` never bounded the response at all.
Per-conversation scores arrived in `model_metrics.scores_by_row`, a full-run
list, while the paged `data_points` carried an empty `metric_value`. A `limit`
of 1 against a 50-conversation run returned all 50 rows of scores and
explanations.

**`model_metrics.scores_by_row` is no longer returned at any depth.** Per 041,
it is not positionally aligned to the data-point list and carries no key to
join on, so pairing a verdict to a conversation through it was guesswork — and
the backend is removing its `__explanation` entries. Per-row verdicts and
evidence now come from each data point's own `checks` field (041), which the
backend attaches to the correct conversation and which pages with its row.

**Migrating off `scores_by_row`**: read `data_points[i].checks` instead. Same
`<check>` and `<check>__explanation` keys, correctly paired, available from
`detail_level="detailed"`. Run-level averages are unaffected — `mean_scores`,
`percentile_scores` and the `aggregate_*` blocks are unchanged.

Run fields dropped from the response: `project_id`, `filter_group_id`, `tags`,
`error_matrix`, `failure_message`, `progress`. Author identity
(`author_name`, `author_email`, `author_type`) is **retained** at every depth.

## `list_test_runs`

| | Before | After |
|---|---|---|
| `model_metrics` | Always present | `detail_level="detailed"` |
| `app_link` | Always present | `detail_level="detailed"` |

**Restore**: `detail_level="detailed"`.

## `list_drivers` and `list_targets`

| | Before | After |
|---|---|---|
| Result count | Every one in the organization | 20 |
| Null voice attributes on text drivers | Returned as `null` | Omitted |

**Restore**: `limit=0`.

## `list_scenarios`

`project_id` no longer appears on each row. The response envelope already names
the project. No parameter change.

## `list_checks`

`checks_by_category` and `uncategorized` now list check **names**; each check's
description and `output_data_type` appear once in a new `checks` array.
Previously a check in three categories was serialized three times in full.

With `all_versions=True`, a category names the check once and `checks` holds one
entry per version.

## New in every affected response

- `total_count`, `limit`, `offset`, `has_more`
- `next_step` — the call that retrieves what was withheld
- `detail_level` — the depth actually served
- Run listings and run retrieval carry `target`, `scenario` and `driver`, which
  no listing surfaced before

## Unchanged

`get_scenario`, `get_target`, `get_driver` and `get_conversation_transcript`
return exactly what they returned before. Naming one item declares intent, so
these return it in full regardless of size.

---

# US6: re-running a simulation

## `run_simulation(based_on_run_id=...)`

| | Before | After |
|---|---|---|
| Inherited from the original | scenario, target, driver | **plus** checks, `stop_check`, `repeats`, `max_turns`, `first_turn`, `checks_at_every_turn`, `turn_transition_time`, `augmentation`, `silence_timeout_ms` |
| A recorded field the tool cannot set | silently defaulted | reported in `rerun_notes` on success as well as failure |

**This is behaviour-changing, and deliberately so.** The parameter's description
said it reran "keeping its configuration" while resolving only three fields, so
changing the driver silently reset `max_turns` to 5 and discarded `stop_check`,
`augmentation` and the check list.

A caller that passed `based_on_run_id` and relied on the other parameters
reverting to tool defaults now gets the original run's values. That reliance was
almost certainly accidental — the documentation promised the opposite — but it
is a visible change. To get the old behaviour, pass the parameters explicitly:
any argument you supply still overrides that one field.

A field the original run never recorded still falls to the tool default. The
recorded set is sparse and differs by run, so a missing field is not treated as
a field set to its default.

## `get_test_run_results` — the `rerun` block

Every response now carries a `rerun` block at every depth:

```jsonc
"rerun": {
  "call": "run_simulation(name='<new name>', based_on_run_id='<id>')",
  "inherited": { "target_name": …, "checks": […], "max_turns": 10,
                 "augmentation": { "noise": {…} }, … },
  "unavailable": [ { "field": "concurrent_ask_probability", "value": 0.4,
                     "reason": "…not settable through run_simulation" } ],
  "note": "Pass any key from `inherited` to override that field alone; …"
}
```

`inherited` holds only keys `run_simulation` accepts, already in the shape the
tool takes — so re-running with different background noise is an edit to a
returned value rather than a construction from the augmentation reference.

Additive: no existing field changed.

## `list_simulations` — `rerun_hint`

A new `rerun_hint` string names the call that carries a listed run's `rerun`
block. Additive.
