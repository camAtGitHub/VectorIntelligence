# Morning weather — owner’s guide

Optional **desk aliveness** for Vector: once Work Day has actually started (`working` or `late_working`), and the local clock is still in the fire window (morning through lunch by default), he fires the **built-in weather intent** (`intent_weather_extend`) — the same firmware animation/voice as you saying “what’s the weather.” Once per calendar day. Not an LLM one-liner.

**Default: off.** Safe for holidays and guests.

**Requires:** full Vector Intelligence install with a **VI chipper build** (behavior-tick). Companion mode does not run presence ticks.

Related: [FSM-workday-companion.md](./FSM-workday-companion.md) · [FSM-implementation.md](./FSM-implementation.md) · live knobs in `shared/config/pod.conf-default`

---

## What it does

1. Work Day is **armed** (`working` or `late_working` — not `no_show` / `late_check` / `waiting_morning` / paused). This path **cannot** arm the day.
2. Local clock is from the morning window begin through **lunch** (default 09:00–12:30).
3. Someone is at the occupied desk.
4. An **enrolled named face** is recent: `face_id > 0`, not a stranger (any enrolled person — not limited to the Work Day primary), and `face_ts` is within `MORNING_WEATHER_FACE_MAX_AGE_S` (default **90s** — not the 30-minute face cache).
5. He has not already told weather today.
6. Chipper then runs `weatherParser` and injects `intent_weather_extend` via **WebViz** cloud intent (`ws://<robot-ip>:8888/socket`) — `FireWeatherIntent`, not AppIntent. Vector-ai **proposing** the intent does not lock the day; chipper POSTs `/v1/behaviors/morning_weather/fired` after inject (or a speak fallback if WebViz fails every robot). Spoken “what’s the weather” still uses the voice IntentPass stream.

He does **not** invent a weather sentence in vector-ai. If the weather API is not configured, chipper says the same error line as a failed user weather request.

### When it fires

Two chipper paths, same gates except occupancy dwell:

| Path | Trigger | Occupancy dwell | Work Day |
|------|---------|-----------------|----------|
| **Restricted** | Enrolled `POST /v1/state/face_seen` | **Skipped** — the enrolled POST is the event | Not run (cannot arm `WORKING`) |
| **60s tick** | `POST /v1/behaviors/tick` | **Kept** (`MORNING_WEATHER_MIN_DWELL_S`, default 60s) | Full FSM still only here |

Idle enrolled sighting is **not** a continuous `robot_observed_face` stream. Chipper’s sparse / `need_identity` **ProbeFace** (first `[face] observed: id=… 'Cam' (enrolled)` bounded by **≤2 min**) POSTs `face_seen` **before** the 60s tick; weather anim then follows in **seconds of that log line**, not of walking into view. Voice-start `ObserveFaceBriefly` still notifies; the arbiter `recent_voice` deny keeps weather off “hey Vector.”

Quiet mode, recent chat, and the global proactive speech gap still apply. A denied tick does **not** consume the daily lock.

## Enable

Both gates, then restart vector-ai (and chipper if you just rebuilt it):

```conf
BEHAVIORS_ENABLED=workday,morning_weather
WORKDAY_ENABLED=1
MORNING_WEATHER_ENABLED=1
WORKDAY_TZ=Australia/Sydney
```

Weather will never fire if Work Day is not armed (`working` or `late_working`). A late-yes afternoon start is enough; `waiting_morning` / `no_show` / `late_check` / paused are not.

| Knob | Default | Purpose |
|------|---------|---------|
| `MORNING_WEATHER_ENABLED` | off | Master switch (also need `morning_weather` in `BEHAVIORS_ENABLED`) |
| `MORNING_WEATHER_PRIORITY` | `40` | Below Work Day (80), above joke idle (15) |
| `MORNING_WEATHER_WINDOW_BEGIN` | `WORKDAY_START_BEGIN` / `09:00` | Start of the fire window |
| `MORNING_WEATHER_LUNCH_END` | `12:30` | Exclusive end (12:30 does not fire) |
| `MORNING_WEATHER_MIN_DWELL_S` | `60` | Occupied + quiet before firing (**60s tick only**; restricted `face_seen` skips this) |
| `MORNING_WEATHER_FACE_MAX_AGE_S` | `90` | Enrolled-face freshness (both paths). Not `FACE_CACHE_MAX_AGE_S` (1800s) |
| `MORNING_WEATHER_TZ` | Work Day / host TZ | Clock for the window |

## Ops

```bash
curl -s http://127.0.0.1:8090/v1/behaviors/morning_weather | jq
curl -s http://127.0.0.1:8090/v1/behaviors/state | jq '.behaviors.morning_weather'
```

`summary` / `reason` is plain English (`workday not started`, `outside morning-lunch window`, `already told weather today`, `need enrolled face`, `stranger or non-primary`, `enrolled face too old`, `ready to tell weather`, …).
