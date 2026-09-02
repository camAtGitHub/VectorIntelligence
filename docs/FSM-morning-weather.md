# Morning weather — owner’s guide

Optional **desk aliveness** for Vector: once Work Day has actually started for the morning, and it is still morning through lunch, he fires the **built-in weather intent** (`intent_weather_extend`) — the same firmware animation/voice as you saying “what’s the weather.” Once per calendar morning. Not an LLM one-liner.

**Default: off.** Safe for holidays and guests.

**Requires:** full Vector Intelligence install with a **VI chipper build** (behavior-tick). Companion mode does not run presence ticks.

Related: [FSM-workday-companion.md](./FSM-workday-companion.md) · [FSM-implementation.md](./FSM-implementation.md) · live knobs in `shared/config/pod.conf-default`

---

## What it does

1. Work Day is **WORKING** (morning start armed — not `no_show` / `late_working` / paused).
2. Local clock is from the morning window begin through **lunch** (default 09:00–12:30).
3. Someone is at the occupied desk, quiet dwell has elapsed (default 60s).
4. He has not already told weather today.
5. Chipper then runs `weatherParser` and injects `intent_weather_extend` via WebViz cloud intent (`ws://<robot-ip>:8888/socket`) — not AppIntent. Spoken “what’s the weather” still uses the voice IntentPass stream.

He does **not** invent a weather sentence in vector-ai. If the weather API is not configured, chipper says the same error line as a failed user weather request.

## Enable

Both gates, then restart vector-ai (and chipper if you just rebuilt it):

```conf
BEHAVIORS_ENABLED=workday,morning_weather
WORKDAY_ENABLED=1
MORNING_WEATHER_ENABLED=1
WORKDAY_TZ=Australia/Sydney
```

Weather will never fire if Work Day did not arm that morning.

| Knob | Default | Purpose |
|------|---------|---------|
| `MORNING_WEATHER_ENABLED` | off | Master switch (also need `morning_weather` in `BEHAVIORS_ENABLED`) |
| `MORNING_WEATHER_PRIORITY` | `40` | Below Work Day (80), above joke idle (15) |
| `MORNING_WEATHER_WINDOW_BEGIN` | `WORKDAY_START_BEGIN` / `09:00` | Start of the fire window |
| `MORNING_WEATHER_LUNCH_END` | `12:30` | Exclusive end (12:30 does not fire) |
| `MORNING_WEATHER_MIN_DWELL_S` | `60` | Occupied + quiet before firing |
| `MORNING_WEATHER_TZ` | Work Day / host TZ | Clock for the window |

## Ops

```bash
curl -s http://127.0.0.1:8090/v1/behaviors/morning_weather | jq
curl -s http://127.0.0.1:8090/v1/behaviors/state | jq '.behaviors.morning_weather'
```

`summary` / `reason` is plain English (`workday not started`, `outside morning-lunch window`, `already told weather today`, `ready to tell weather`, …).
