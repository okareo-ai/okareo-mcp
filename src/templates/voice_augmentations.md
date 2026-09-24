# Voice Simulation Augmentations

When running a multi-turn voice simulation via `run_simulation`, the
`augmentation` parameter applies realistic acoustic and conversational effects
to the run. Seven top-level keys are available:

| Key | What it does |
|---|---|
| `cap` | Concurrent Ask — caller fires two questions in quick succession |
| `directed_speech` | User speaks off-mic with attenuation and reverb |
| `secondary_speaker` | A second human voice appears in the room |
| `backchannel` | Human-style "mm-hmm" cues while the agent speaks |
| `barge_in` | User interrupts the agent mid-utterance |
| `dropout` | Caller says nothing for a whole turn — the agent hears dead air |
| `noise` | Ambient background noise (composable add-on) |

## Composition rule

Exactly **one** non-noise strategy may be active per run. `noise` may be
combined with one of the six non-noise strategies. Any other combination is
rejected by the MCP before any call to Okareo.

```text
OK:   {}                                # no augmentation
OK:   {"cap": {...}}                    # single non-noise
OK:   {"noise": {...}}                  # noise only
OK:   {"barge_in": {...}, "noise": {...}}   # noise + one
OK:   {"dropout": {...}, "noise": {...}}    # noise + one
REJECT: {"cap": {...}, "barge_in": {...}}   # two non-noise — error
REJECT: {"dropout": {...}, "barge_in": {...}}  # two non-noise — error
```

Augmentations apply **only to voice Targets** (edge types: twilio, sip).
Calls against generation or custom_endpoint Targets with an
`augmentation` block are rejected.

Every setting in a strategy is checked against what Okareo actually applies. A
misspelled setting, or one a strategy does not take, is rejected by name rather
than silently ignored.

## When a strategy starts firing

`directed_speech`, `secondary_speaker`, `backchannel`, `barge_in` and `dropout`
accept `start_at_turn` (int >= 1, default 1). It holds the strategy off until
that caller turn. Turns are numbered as in the transcript: the agent's greeting
is turn 0 and the first caller turn is turn 1, so `start_at_turn: 3` leaves the
first two caller turns clean. The default of 1 means live from the start.

`start_at_turn` must not exceed the run's `max_turns` — the strategy would never
fire, and the run would look clean while testing nothing. Equal to `max_turns` is
allowed. `cap` and `noise` do not take it.

## Two limits worth knowing

**Not every Target supports every strategy.** `dropout`, `barge_in` and
`backchannel` need a connection where the caller can stay silent or interrupt:
phone (Twilio, Vonage, Telnyx), SIP and WebRTC Targets. On OpenAI and Deepgram
realtime Targets, Okareo runs the simulation without them — the run succeeds,
and the augmentation simply never happened.

**A `seed` makes a run reproducible, including across repeats.** Every
conversation of a seeded run fires on the same turns. Leave `seed` out if you
want variety across `repeats`.

## Strategy reference

### `cap` — Concurrent Ask

| Field | Required? | Type / range | Default |
|---|---|---|---|
| `probability` | yes | float in [0.0, 1.0] | — |
| `pause_ms` | no | int in [0, 10000] | 1000 |

```json
{"cap": {"probability": 0.4, "pause_ms": 800}}
```

### `directed_speech` — Off-mic speech

| Field | Required? | Type / range | Default |
|---|---|---|---|
| `probability` | yes | float in [0.0, 1.0] | — |
| `lpf_cutoff_hz` | no | int > 0 | 800 |
| `gain_db` | no | float in [-40.0, 0.0] | -8.0 |
| `sample_rate` | no | int > 0 | 24000 |
| `prompt` | no | string | server default |
| `reverb_preset` | no | string (e.g. `room_teleco`) | server default |
| `start_at_turn` | no | int >= 1, <= `max_turns` | 1 |

```json
{"directed_speech": {"probability": 0.3, "lpf_cutoff_hz": 800, "gain_db": -8.0}}
```

### `secondary_speaker` — Second voice in the room

| Field | Required? | Type / range | Default |
|---|---|---|---|
| `probability` | yes | float in [0.0, 1.0] | — |
| `secondary_voice` | yes | non-empty string (TTS voice ID/name) | — |
| `inter_speaker_pause_ms` | no | int in [0, 5000] | 120 |
| `lpf_cutoff_hz` | no | int > 0 | 800 |
| `gain_db` | no | float in [-40.0, 0.0] | -8.0 |
| `sample_rate` | no | int > 0 | 24000 |
| `secondary_prompt` | no | string | server default |
| `secondary_voice_instructions` | no | string | — |
| `secondary_reverb_preset` | no | string | — |
| `start_at_turn` | no | int >= 1, <= `max_turns` | 1 |

Also accepted as (the SDK's spellings): `voice` for `secondary_voice`, `prompt`
for `secondary_prompt`, `reverb_preset` for `secondary_reverb_preset`. Use one
spelling per setting, not both.

```json
{"secondary_speaker": {
  "probability": 0.3,
  "secondary_voice": "Cathy - Coworker",
  "inter_speaker_pause_ms": 120
}}
```

### `backchannel` — "mm-hmm" style cues

| Field | Required? | Type / range | Default |
|---|---|---|---|
| `utterance` | yes | non-empty string | — |
| `probability` | no | float in [0.0, 1.0] | 0.35 |
| `min_offset_ms` | no | int >= 0 | 150 |
| `max_offset_ms` | no | int >= `min_offset_ms` | 450 |
| `start_at_turn` | no | int >= 1, <= `max_turns` | 1 |
| `seed` | no | int or null (deterministic timing) | — |

```json
{"backchannel": {
  "utterance": "mm-hmm",
  "probability": 0.35,
  "min_offset_ms": 150,
  "max_offset_ms": 450
}}
```

### `barge_in` — Mid-utterance interruption

| Field | Required? | Type / range | Default |
|---|---|---|---|
| `prompt` | yes | non-empty string (drives interruption text) | — |
| `probability` | no | float in [0.0, 1.0] | 0.2 |
| `min_offset_ms` | no | int >= 0 | 200 |
| `max_offset_ms` | no | int >= `min_offset_ms` | 600 |
| `start_at_turn` | no | int >= 1, <= `max_turns` | 1 |
| `seed` | no | int or null | — |

`replacement_text` and `utterance` appear in the SDK but Okareo ignores them for
`barge_in`, so the MCP rejects them. Steer what the interruption says with
`prompt`.

```json
{"barge_in": {
  "prompt": "Politely interrupt and ask the agent to slow down.",
  "probability": 0.2,
  "min_offset_ms": 200,
  "max_offset_ms": 600
}}
```

### `dropout` — The caller goes silent for a turn

With `probability`, per caller turn, the caller says nothing for the whole turn:
no words, no audio. The agent hears dead air and has to notice it and recover —
the way a real caller's line blips or they get distracted. Nothing from a
dropped turn enters the conversation, and it leaves no message in the transcript.
`get_conversation_transcript` lists those turns in `dropped_turns`, so a silenced
turn can be told apart from one that never happened.

| Field | Required? | Type / range | Default |
|---|---|---|---|
| `probability` | yes | float in [0.0, 1.0] | — |
| `start_at_turn` | no | int >= 1, <= `max_turns` | 1 |
| `seed` | no | int or null | — |

```json
{"dropout": {"probability": 0.3, "start_at_turn": 3}}
```

### `noise` — Ambient background noise (composable)

| Field | Required? | Type / range | Default |
|---|---|---|---|
| `noise_profile` | yes | server-defined string (e.g. `cafeteria`, `classroom`, `office_babble`, `traffic`) | — |
| `noise_snr_db` | yes | float (recommended [-5, 25]) | — |
| `seed` | no | int or null | — |

```json
{"noise": {"noise_profile": "cafeteria", "noise_snr_db": 10}}
```

Also accepted as (the SDK's spellings): `profile` for `noise_profile`, `snr_db`
for `noise_snr_db`. Use one spelling per setting, not both. Noise plays for the
whole call, so there is no `probability` — the SDK publishes one, but Okareo
ignores it and the MCP rejects it.

The set of valid `noise_profile` and `reverb_preset` values is controlled by
Okareo. The MCP does not preflight these values — if you send something Okareo
doesn't recognise, it responds with a 400 listing the current valid set.

## Composed example — barge-in over cafeteria noise

```json
{
  "barge_in": {
    "prompt": "Politely interrupt and ask the agent to slow down.",
    "probability": 0.2,
    "min_offset_ms": 200,
    "max_offset_ms": 600
  },
  "noise": {
    "noise_profile": "cafeteria",
    "noise_snr_db": 10
  }
}
```

## Composed example — the line cuts out in a busy café

```json
{
  "dropout": {"probability": 0.3, "start_at_turn": 3},
  "noise": {"profile": "cafeteria", "snr_db": 10}
}
```

## Common errors

| Error | Meaning |
|---|---|
| `Unknown augmentation strategy '<x>'. Known: [...].` | Top-level key not in the seven valid names |
| `Unsupported augmentation combination: barge_in, cap. Only noise + one other strategy is composable.` | Two non-noise strategies in the same block |
| `Invalid cap.probability=1.5. Must be in [0.0, 1.0].` | Range violation; field path is in `field` of the error envelope |
| `cap.probability is required.` | A required field was omitted |
| `Invalid barge_in.max_offset_ms=100: must be >= min_offset_ms (200).` | Offsets are swapped |
| `Unknown field cap.start_at_turn. cap accepts: ['pause_ms', 'probability'].` | A setting that strategy does not take — Okareo would ignore it |
| `barge_in.utterance is published by the SDK but ignored by Okareo. Use barge_in.prompt to steer what the interruption says.` | An SDK setting Okareo discards (also `barge_in.replacement_text`, `noise.probability`) |
| `Set either noise.profile or noise.noise_profile, not both.` | Both spellings of one setting in the same block |
| `Invalid dropout.start_at_turn=0. Must be an int >= 1 (turn 0 is the agent's greeting).` | `start_at_turn` below 1, or not an int |
| `augmentation.dropout.start_at_turn=6 is beyond max_turns=5, so dropout would never fire. Lower start_at_turn or raise max_turns.` | The strategy could never fire in this run |
| `Invalid dropout.seed='abc'. Must be an int or null.` | `seed` of the wrong type |
| `Augmentations apply only to voice Targets. Target 'X' is of type 'custom_endpoint'.` | The Target named is not voice-type |

All preflight errors return without making any network call to Okareo. On a
rerun (`based_on_run_id`), an inherited block is checked the same way; the error
then ends with `(inherited from run '<id>'; pass augmentation to override)`.

## Related parameters on `run_simulation`

These are documented here because users tuning realism often want them together:

- `turn_transition_time` — ms of pause between turns (default 1000).
- `silence_timeout_ms` — the target reply timeout: how patient Okareo is
  before indicating that the target can't respond. Do NOT set or change this
  value unless the user specifically directs it; it should be 10000 ms in
  nearly all cases. It exists to accommodate untuned targets with very long
  tool calls, during which the Driver waits patiently. It does NOT change how
  fast Okareo responds, and lowering it does not speed anything up — a slow
  simulation is not a reason to change it.
- `checks_at_every_turn` — evaluate checks per turn instead of at end-of-run.
- `stop_check` — `{"check_name": str, "stop_on": <value>}` halts the run when
  the named check returns the configured value.

## Response: the run never blocks the caller

`run_simulation` returns promptly so a long voice run never times out the
co-pilot. Short runs return `status: "finished"` with results ready. Longer runs
return `status: "running"` with the `test_run_id`, `app_link`, and an advisory
`estimated_runtime` (voice runs are slower than text) — the run finishes on its
own. In both cases, poll `get_test_run_results` with the `test_run_id` for scores
and `get_conversation_transcript` for transcripts.
