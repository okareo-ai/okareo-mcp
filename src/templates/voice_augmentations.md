# Voice Simulation Augmentations

When running a multi-turn voice simulation via `run_simulation`, the
`augmentation` parameter applies realistic acoustic and conversational effects
to the run. Six top-level keys are available:

| Key | What it does |
|---|---|
| `cap` | Concurrent Ask — caller fires two questions in quick succession |
| `directed_speech` | User speaks off-mic with attenuation and reverb |
| `secondary_speaker` | A second human voice appears in the room |
| `backchannel` | Human-style "mm-hmm" cues while the agent speaks |
| `barge_in` | User interrupts the agent mid-utterance |
| `noise` | Ambient background noise (composable add-on) |

## Composition rule

Exactly **one** non-noise strategy may be active per run. `noise` may be
combined with one of the five non-noise strategies. Any other combination is
rejected by the MCP before any backend call.

```text
OK:   {}                                # no augmentation
OK:   {"cap": {...}}                    # single non-noise
OK:   {"noise": {...}}                  # noise only
OK:   {"barge_in": {...}, "noise": {...}}   # noise + one
REJECT: {"cap": {...}, "barge_in": {...}}   # two non-noise — error
```

Augmentations apply **only to voice Targets** (edge types: openai, deepgram,
twilio). Calls against generation or custom_endpoint Targets with an
`augmentation` block are rejected.

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
| `seed` | no | int (deterministic timing) | — |

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
| `seed` | no | int | — |

```json
{"barge_in": {
  "prompt": "Politely interrupt and ask the agent to slow down.",
  "probability": 0.2,
  "min_offset_ms": 200,
  "max_offset_ms": 600
}}
```

### `noise` — Ambient background noise (composable)

| Field | Required? | Type / range | Default |
|---|---|---|---|
| `noise_profile` | yes | server-defined string (e.g. `cafeteria`, `classroom`, `office_babble`, `traffic`) | — |
| `noise_snr_db` | yes | float (recommended [-5, 25]) | — |
| `seed` | no | int | — |

```json
{"noise": {"noise_profile": "cafeteria", "noise_snr_db": 10}}
```

The set of valid `noise_profile` and `reverb_preset` values is server-controlled.
The MCP does not preflight these values — if you send something the backend
doesn't recognise, the backend will respond with a 400 listing the current
valid set.

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

## Common errors

| Error | Meaning |
|---|---|
| `Unknown augmentation strategy '<x>'. Known: [...].` | Top-level key not in the six valid names |
| `Unsupported augmentation combination: cap, barge_in. Only noise + one other strategy is composable.` | Two non-noise strategies in the same block |
| `Invalid cap.probability=1.5. Must be in [0.0, 1.0].` | Range violation; field path is in `field` of the error envelope |
| `cap.probability is required.` | A required field was omitted |
| `Invalid barge_in.max_offset_ms=100: must be >= min_offset_ms (200).` | Offsets are swapped |
| `Augmentations apply only to voice Targets. Target 'X' is of type 'custom_endpoint'.` | The Target named is not voice-type |

All preflight errors return without making any network call to the backend.

## Related parameters on `run_simulation`

These are documented here because users tuning realism often want them together:

- `turn_transition_time` — ms of pause between turns (default 1000).
- `silence_timeout_ms` — ms of silence before the simulator advances.
- `checks_at_every_turn` — evaluate checks per turn instead of at end-of-run.
- `stop_check` — `{"check_name": str, "stop_on": <value>}` halts the run when
  the named check returns the configured value.
