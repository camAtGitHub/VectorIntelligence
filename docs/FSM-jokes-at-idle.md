# Joke / question when idle — owner’s guide

Optional **desk aliveness** for Vector: when someone has been present but quiet for a long while, he may occasionally speak first — a short dry one-liner or a light conversation-starter question — then stay quiet for a long time.

**Default: off.** Safe for holidays, guests, and shared desks.

**Requires:** full Vector Intelligence install with a **patched chipper** (behavior-tick loop). Companion mode (stock Wire-Pod) does not run presence ticks, so this behavior will not speak on its own there.

Related:

- Build / implementation spec (archived): `docs/completed/FSM-joke-when-idle-spec.md`
- Sibling FSM (accountability): `docs/FSM-workday-companion.md`
- Live template of all knobs: `shared/config/pod.conf-default`

---

## What it does (not how)

### The idea

You are at the desk. You have not talked to Vector for a while. He is not mid-workday-nag. After a long quiet stretch he may drop **one** line — either a joke or a question — then go quiet for hours (and only a few times per day).

He does **not** invent that line on the spot while deciding to talk. Lines are **pre-vetted and banked** so speech stays fast and the presence tick never waits on an LLM.

### Where the lines come from

Two sources fill a small queue in SQLite:

1. **Curated seeds** — a shipped list of hand-written jokes and questions (`joke_seeds.txt` next to the behavior code). Always available; workplace-safe, deadpan desk-robot voice.
2. **Background generation** (optional quality renewer) — when the feature is on, a slow interval loop may call the LLM **off the speak path** to generate candidates, have a second pass score them, drop clichés/near-duplicates, and bank the keepers. If generation fails or the API is down, **curated alone** still tops up the queue.

### Who he talks to

| Mode | Behavior |
|------|----------|
| **`known` (default)** | Only after a **recognized** enrolled face at the arm moment. Strangers stay silent; camera is not re-probed every tick. |
| **`anyone`** | Anyone at an occupied desk after dwell; no face probe for this behavior. |

### Manners built in

- Long **dwell** before the first line (default ~20 minutes quiet+occupied).
- Long **cooldown** after a delivered line (default ~2.5 hours).
- Hard **daily cap** (default 4 lines).
- Respects **quiet mode**, recent chat, and the global proactive speech gap (shared with Work Day).
- Loses to higher-priority behaviors (Work Day priority is much higher by default).

### What it does *not* do

- Does not call the LLM during `tick()` / while deciding to speak.
- Does not modify Work Day, ambient glances, greetings, or sensor lines.
- Does not require you to chat-command anything (no joke-specific tags).
- Does not run useful presence ticks on **stock companion** chipper without a full rebuild.

---

## Where to set configuration

Joke idle knobs are **process environment variables** read by **vector-ai** at start. In production the supervisor loads them from **`pod.conf`** (next to `supervisor.py`) and injects them into the vector-ai child env.

**`vector-ai/.env` is for OpenRouter / LLM credentials and base model settings only** — not for `JOKE_*` (or `WORKDAY_*`). Behavior FSM knobs live in **`pod.conf`**.

| Platform | Live config file (edit this) |
|----------|------------------------------|
| **Windows** | `%USERPROFILE%\vector-pod\pod.conf` |
| **Linux** | `~/vector-pod/pod.conf` (typical full install) |

Template / defaults shipped in the repo:

- `shared/config/pod.conf-default` — commented Work Day / **Joke** / speech keys  
- `shared/vector-ai/env-default` — OpenRouter/LLM only (points at pod.conf for FSM knobs)

**Legacy note:** if a key still exists only in a runtime `.env`, it may still be read when not already in the process env. When the same key is set in both places, **pod.conf wins** (supervisor injects first; dotenv does not override). Prefer editing **pod.conf** only.

After any change: **restart vector-ai** (e.g. stop then start Vector / companion so the supervisor reloads the brain). No chipper rebuild is required for config-only changes.

Install/setup **upserts** ports/paths into pod.conf and never wipes hand-edited `JOKE_*` / `WORKDAY_*` keys.

---

## Dual enable gate (both required)

A plugin runs only when **both** are true:

1. Its id is listed in `BEHAVIORS_ENABLED` (comma-separated).
2. Its feature flag is truthy (`JOKE_ENABLED` for this FSM).

Truthy values: `1` / `true` / `yes` / `on` (case-insensitive).

| Goal | `BEHAVIORS_ENABLED` | `JOKE_ENABLED` |
|------|---------------------|----------------|
| Off (default / guests) | anything | `0` or unset |
| Joke idle only | `joke_idle` | `1` |
| Work Day + jokes | `workday,joke_idle` | `1` (and `WORKDAY_ENABLED=1` for workday) |
| Work Day only | `workday` | `0` |

If either gate is missing, the behavior is not registered and the background refill loop does not start.

---

## All configurable variables

Values are strings in `pod.conf`. Times and intervals are integers in **seconds** unless noted. Booleans use the truthy list above. Bad numbers fall back to defaults (vector-ai still starts).

### Master switch and audience

| Variable | Default | What it does |
|----------|---------|----------------|
| `JOKE_ENABLED` | `0` | Master switch. `1` = on (also need `joke_idle` in `BEHAVIORS_ENABLED`). **Leave off** for holidays, guests, or anyone who does not want unprompted banter. |
| `JOKE_AUDIENCE` | `known` | Who may receive a line. **`known`** = only a recognized enrolled face. **`anyone`** = any occupied desk (no face probe for this FSM). Any other value is treated as `known`. |
| `JOKE_PRIORITY` | `15` | Speech priority vs other behaviors (higher wins if two want to talk). Work Day defaults to **80**, so accountability nags beat idle jokes. Rarely needs changing. |
| `JOKE_TZ` | `WORKDAY_TZ`, else host `TZ`, else `UTC` | Timezone used only for the **daily cap** calendar date (e.g. `Australia/Sydney`). |

### Pacing (how often he bothers you)

| Variable | Default | Human scale | What it does |
|----------|---------|-------------|--------------|
| `JOKE_MIN_DWELL_S` | `1200` | ~**20 minutes** | Quiet + occupied time required before he is allowed to arm a line. |
| `JOKE_COOLDOWN_S` | `9000` | ~**2.5 hours** | Enforced silence after a line was **actually spoken** (speech-gated: denied lines do not burn this). |
| `JOKE_MAX_PER_DAY` | `4` | 4/day | Hard cap on delivered lines per local calendar day (`JOKE_TZ`). |
| `JOKE_QUESTION_RATIO` | `0.6` | 60% questions | Probability a served line prefers a **question** over a **joke** (falls back to the other kind if that queue is empty). Range 0.0–1.0. |
| `JOKE_IDENTITY_REJECT_COOLDOWN_S` | `1800` | ~**30 minutes** | In **`known`** mode only: after a stranger (or no face) at the arm moment, wait this long before requesting face ID again. Avoids camera spam. |

### Queue refill (background; does not affect speak latency)

| Variable | Default | What it does |
|----------|---------|----------------|
| `JOKE_REFILL_INTERVAL_S` | `43200` | How often the background loop wakes (~**12 hours**). Interval-based (survives PC sleep); not “run at 3am”. |
| `JOKE_QUEUE_TARGET` | `50` | Soft full size of the vetted line queue. |
| `JOKE_QUEUE_LOW_WATERMARK` | `30` | Refill only runs when the queue has drained to **this many or fewer**. With defaults you use ~20 lines before regeneration. |
| `JOKE_CURATED_RATIO` | `0.5` | Preferred fraction of a refill drawn from the curated file vs LLM generation. If generation fails, curated may supply **100%** of the fill (serving never depends on the LLM). |
| `JOKE_SEED_FILE` | `joke_seeds.txt` | Curated data file name or path. Relative names resolve next to the behavior module (`shared/vector-ai/behaviors/`). |
| `JOKE_MIN_SCORE` | `0.55` | Critic score floor (0–1). Below this, a generated candidate is dropped. |
| `JOKE_NOVELTY_MIN` | `0.4` | Minimum novelty vs recently served history (0–1; higher = more different). Near-duplicates are dropped. |

### Model tiering (OpenRouter / `llm_chat_once`)

Generation uses the same stack as chat (`LLM_BASE_URL` + key in **`.env`**). These knobs only pick **which model string** to send on refill calls:

| Variable | Default | What it does |
|----------|---------|----------------|
| `JOKE_GENERATE_MODEL` | empty | OpenRouter (or compatible) model id for **writing** candidates. Empty → use default `LLM_MODEL` from `.env` (e.g. `google/gemini-2.0-flash`). |
| `JOKE_CRITIC_MODEL` | empty | Model id for **scoring** candidates. Empty → same default `LLM_MODEL`. Prefer a **different** model than generate when possible (cross-model judging beats self-judging). |

**Examples** (slugs change over time; pick ones you already use on OpenRouter):

```conf
# Use a stronger writer, keep a cheap/different critic
JOKE_GENERATE_MODEL=anthropic/claude-sonnet-4
JOKE_CRITIC_MODEL=google/gemini-2.0-flash

# Or leave both blank — both stages use LLM_MODEL from .env
# JOKE_GENERATE_MODEL=
# JOKE_CRITIC_MODEL=
```

Notes:

- Refill is infrequent; a larger generate model is usually affordable compared to chat.
- The critic call uses lower temperature; generate uses higher temperature.
- If either call fails, the loop logs and continues; curated top-up still runs.
- Changing models only needs a **vector-ai restart** (not a chipper rebuild).

### Shared multi-behavior / speech manners

These are not `JOKE_*` but they affect whether idle jokes can be heard:

| Variable | Default | What it does |
|----------|---------|----------------|
| `BEHAVIORS_ENABLED` | `workday` | Comma-separated plugin ids. Must include `joke_idle` for this FSM. Example: `workday,joke_idle`. |
| `SPEECH_MIN_GAP_S` | `90` | Global minimum seconds between **any** two proactive lines (Work Day + jokes share this). |
| `SPEECH_SUPPRESS_AFTER_VOICE_S` | `120` | After you chat with Vector, suppress unprompted proactive speech for this long. |
| `FACE_CACHE_MAX_AGE_S` | `120` | How long a face result may be reused before a fresh look (~**2 minutes**). Matters mainly for `JOKE_AUDIENCE=known`. |

---

## Example `pod.conf` blocks

### Minimal enable (recommended first try)

```conf
# Dual gate: list + flag
BEHAVIORS_ENABLED=workday,joke_idle
JOKE_ENABLED=1
JOKE_TZ=Australia/Sydney
# audience stays "known" — only enrolled faces
```

### Guest-friendly / party mode

```conf
BEHAVIORS_ENABLED=workday,joke_idle
JOKE_ENABLED=1
JOKE_AUDIENCE=anyone
JOKE_MAX_PER_DAY=2
JOKE_MIN_DWELL_S=1800
JOKE_COOLDOWN_S=14400
```

### Quieter desk (rare, longer waits)

```conf
BEHAVIORS_ENABLED=workday,joke_idle
JOKE_ENABLED=1
JOKE_MIN_DWELL_S=2400
JOKE_COOLDOWN_S=14400
JOKE_MAX_PER_DAY=2
JOKE_QUESTION_RATIO=0.8
```

### More banter (still capped)

```conf
BEHAVIORS_ENABLED=workday,joke_idle
JOKE_ENABLED=1
JOKE_MIN_DWELL_S=900
JOKE_COOLDOWN_S=5400
JOKE_MAX_PER_DAY=6
JOKE_QUESTION_RATIO=0.4
```

### Holiday / guests — fully off

```conf
JOKE_ENABLED=0
# optional: also drop from the list
# BEHAVIORS_ENABLED=workday
```

Restart vector-ai after edits. No rebuild required to turn off.

### Full commented reference (from template)

See `shared/config/pod.conf-default` — every `JOKE_*` key listed with defaults as comments.

---

## How speech vs refill interact (why it feels snappy)

```
chipper presence tick  →  vector-ai BehaviorRuntime.tick()
                              → JokeIdleBehavior (pure SQLite pop)
                              → SpeechArbiter (quiet / gap / priority)
                              → robot speaks (if allowed)
                                    └─ only then: daily count + cooldown commit

background (async)     →  refill every JOKE_REFILL_INTERVAL_S
                              → if queue ≤ watermark: curated + optional LLM
                              → bank into joke_queue
```

If the arbiter **denies** a line (quiet mode, recent voice, min gap, lower priority), cooldown and daily count **do not advance**. The queue item may already be marked served (so the same text is not re-offered), which is intentional.

---

## How to enable / install checklist

### Prerequisites

- Vector Intelligence full install with **behavior-tick** patch (same body loop Work Day uses).
- Working OpenRouter (or compatible) key in `vector-ai/.env` if you want **generated** renewals; curated-only still works without successful generation.
- For `JOKE_AUDIENCE=known`: face enrollment so Vector can recognize you.

### Config steps

1. Edit live **`pod.conf`** (paths above).  
2. Set `JOKE_ENABLED=1` and include `joke_idle` in `BEHAVIORS_ENABLED`.  
3. Set `JOKE_TZ` (or rely on `WORKDAY_TZ` / host `TZ`).  
4. Optionally set models, pacing, and audience.  
5. Restart vector-ai.

### Confirm it’s live

With vector-ai running (default port **8090**):

```bash
# Shared index (envelope v1): card under behaviors.joke_idle
curl -s http://127.0.0.1:8090/v1/behaviors/state | jq
# Joke detail (dwell/cooldown remaining, queue_len, daily cap)
curl -s http://127.0.0.1:8090/v1/behaviors/joke_idle | jq
```

On the index, `behaviors.joke_idle` should appear when both gates are on
(`JOKE_ENABLED=1` and `joke_idle` in `BEHAVIORS_ENABLED`); `summary` is a
gate reason such as `dwell_building`, `cooldown`, `capped`, or `idle_ready`.  
`/health` should still be healthy for normal chat.

Logs on enable typically include a short `[behaviors]` / joke status line at startup; when disabled, the refill task is not started.

### Developer / unit tests (no robot)

From the repo (vector-ai package tree):

```bash
cd shared/vector-ai
python3 test_joke_idle.py
# or full behaviors suite (includes workday + joke)
python3 test_behaviors.py
```

---

## Curated seed file (advanced)

Default file: `shared/vector-ai/behaviors/joke_seeds.txt`.

Format:

- UTF-8, one item per line: `KIND` then a tab then `TEXT`
- `KIND` is `joke` or `question`
- Blank lines and lines starting with `#` are comments

You can point `JOKE_SEED_FILE` at another path if you maintain your own list. Keep lines short (under ~20 words works best for TTS). Avoid guest-unsafe content.

---

## Quick troubleshooting

| Symptom | What to check |
|---------|----------------|
| Never jokes | `JOKE_ENABLED=1`? `joke_idle` in `BEHAVIORS_ENABLED`? Restarted vector-ai? **Full** patched chipper? |
| Still silent with flags on | Dwell not met yet (`JOKE_MIN_DWELL_S`)? Cooldown / daily cap? Quiet mode or recent chat (`SPEECH_SUPPRESS_AFTER_VOICE_S`)? Desk not “occupied”? |
| Only silent for strangers | Expected with `JOKE_AUDIENCE=known`. Enroll face or switch to `anyone`. |
| Face probe spam | Should not happen every tick; if stuck requesting identity, check `JOKE_IDENTITY_REJECT_COOLDOWN_S` and enrollment. |
| Same joke forever / empty novelty | Queue may be curated-only after LLM failures; check OpenRouter key / model ids; wait for next refill interval or lower watermark temporarily for testing. |
| Too chatty | Raise `JOKE_MIN_DWELL_S` / `JOKE_COOLDOWN_S`, lower `JOKE_MAX_PER_DAY`, or set `JOKE_ENABLED=0`. |
| Work Day never loses to jokes | Expected: workday priority ~80 vs joke ~15. |
| Config ignored | Edited **pod.conf** (not only `.env`)? Supervisor restarted so child env reloads? |
| Wrong “day” for daily cap | Set `JOKE_TZ` (or `WORKDAY_TZ`) to your IANA zone. |
| Companion-only install | Need full install + rebuild for the presence tick loop. |

---

## Summary cheat sheet

| Want… | Set… |
|-------|------|
| Off | `JOKE_ENABLED=0` |
| On | `BEHAVIORS_ENABLED=…,joke_idle` and `JOKE_ENABLED=1` |
| Only me | `JOKE_AUDIENCE=known` |
| Anyone at desk | `JOKE_AUDIENCE=anyone` |
| Bigger joke writer | `JOKE_GENERATE_MODEL=<openrouter-slug>` |
| Separate judge model | `JOKE_CRITIC_MODEL=<different-slug>` |
| Edit config | **`pod.conf`** next to supervisor; restart vector-ai |

LLM secrets and default chat model remain in **`vector-ai/.env`** (`OPENROUTER_API_KEY`, `LLM_MODEL`, …). Joke-specific model overrides are **`JOKE_GENERATE_MODEL` / `JOKE_CRITIC_MODEL` in `pod.conf`**.
