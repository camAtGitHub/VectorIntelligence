# Morning weather intent

**Status:** implemented 2026-08-29. Owner’s guide: [docs/FSM-morning-weather.md](../FSM-morning-weather.md).

## What

A small behavior module: **if Work Day is live for the day** (morning start actually armed — not merely `WORKDAY_ENABLED`) **and local time is morning up through lunch**, trigger the **built-in user weather intent** (`intent_weather_extend`) so Vector plays the firmware weather animation/voice with real conditions — same path as the human saying “what’s the weather.”

Not an LLM one-liner. Use the existing weather stack (chipper `weatherParser` + robot intent), once per morning.

## Why this shape

- Work Day already knows “today started” vs `no_show` / `past_end`.
- Weather API + speakable params already live in chipper (`ttr/weather.go`, `intent_weather_extend`).
- AppIntent / intent-graph is how user weather is delivered today; don’t re-implement TTS weather in vector-ai.

## Suggested gates (when building)

- Workday FSM in a live working mode (morning-armed `working`, not `off` / `no_show` / `past_end`).
- Local `WORKDAY_TZ` clock from morning start through lunch (lunch end is **not** a knob yet — add e.g. `WORKDAY_LUNCH_END=12:30` or hard-default 12:00).
- Occupied desk, identity same manners as other morning pokes.
- Once per calendar day, speech-gated (don’t burn the day if the arbiter denies).
- Quiet / recent-voice / min-gap still apply.

## Chipper hook (likely needed)

`POST /v1/behaviors/tick` today only returns `{speak, need_identity}`. Weather is not a speak line. Extend the tick result with something like `"intent": "intent_weather_extend"` (and maybe param payload), and have `behavior_tick.go` fire the same intent path user speech uses — **not** `ambientReact` of LLM text.

Look at `intentparam.go` (`intent_weather_extend` → `weatherParser`) and `AppIntent` usage in `kgsim_cmds.go`.

## Out of scope until specified

- Afternoon / evening weather.
- Forecast-for-tomorrow unless the built-in intent already does it from “what’s the weather.”
- LLM describing the weather.
- Firing weather when workday never started that morning.
