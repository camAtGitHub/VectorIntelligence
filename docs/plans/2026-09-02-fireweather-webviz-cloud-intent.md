# Fix FireWeatherIntent: WebViz cloud-intent side channel

**Status:** implemented chipper (Phases 1–2); E2E (Phase 4) pending. Docs closeout Phase 3 done 2026-09-02.  
**Parent:** [2026-08-29-morning-weather-intent.md](./2026-08-29-morning-weather-intent.md) · owner guide [FSM-morning-weather.md](../FSM-morning-weather.md)  
**Live proof:** 2026-09-02 WebViz WS inject to `192.168.50.189:8888` → engine `weather_response` + robot weather anim played.

---

## Problem

Morning weather FSM (vector-ai) correctly proposes `intent: "intent_weather_extend"`. Chipper `FireWeatherIntent` correctly calls `weatherParser`, then delivers via **gRPC `AppIntent`** to robot `:443`.

Weather is registered only as a **cloud** intent in firmware `user_intent_map.json` (no `app_intent`). Engine `OnAppIntent` → unmatched → nothing plays. Spoken weather still works because it uses the voice chipper stream → `IntentPass` → `CloudMic` path.

Spoken `IntentPass` **cannot** be reused for morning weather: it requires an open `StreamingIntent` / `StreamingIntentGraph` session (robot-initiated after wake word).

---

## Phase 0 — Allowed APIs (documentation discovery)

### How spoken weather reaches the engine (reference — do not invent a stream)

| Hop | App | Port / path | Protocol |
|-----|-----|-------------|----------|
| Robot → chipper | `vic-cloud` → chipper | chipper **TCP 443** (TLS; also 8084) | gRPC `ChipperGrpc` `StreamingIntentGraph` |
| Chipper reply | `IntentPass` on **same stream** | — | `IntentResult{Action, Parameters}` |
| On robot | `vic-cloud` → `vic-engine` | `/dev/socket/ai_sock` | Unix datagram + CLAD `CloudMic::IntentResult` |

Sources: `wire-pod/chipper/pkg/initwirepod/startserver.go` (TLS `:443` + cmux); `matchIntentSend.go` `IntentPass`; `victor-1.6-rebuild-2/cloud/internal/voice/stream/context.go` `sendIntentResponse`; `clad/cloud/mic.clad` `IntentResult`; `userIntentComponent.cpp` `OnCloudData`.

### Broken path (current FireWeatherIntent)

| Hop | Detail |
|-----|--------|
| Chipper → robot | gRPC SDK `AppIntent` to **robot TCP 443** |
| Engine | `OnAppIntent` → `GetUserIntentFromAppIntent` → **no map** for `intent_weather_extend` |

Sources: `behavior_tick.go` `FireWeatherIntent` ~272–321; `user_intent_map.json` cloud-only entry ~140–148; `userIntentComponent.cpp` `OnAppIntent` ~1091+.

### Proven side channel (use this)

| Hop | Detail |
|-----|--------|
| Client → robot | **WebSocket** `ws://<robot-ip>:8888/socket` |
| Envelope | `{ "type":"data", "module":"intents", "data":{ "intentType":"cloud", "request": <CloudMic JSON string> } }` |
| Engine | WebViz intents handler → `SetCloudIntentPendingFromString(request)` |

**CloudMic `request` string shape** (copy from tests / voice encoding):

```json
{
  "intent": "intent_weather_extend",
  "parameters": "{\"condition\":\"Cloudy\",\"is_forecast\":\"false\",\"local_datetime\":\"...\",\"speakable_location_string\":\"Sydney\",\"temperature\":\"22\",\"temperature_unit\":\"C\"}\n"
}
```

- `parameters` is a **string** (stringified JSON), not an object.
- Missing `"type"` → engine assumes `"result"`.
- Live inject 2026-09-02 returned WebViz `current-intent`: user=`weather_response`, cloud=`intent_weather_extend`, and weather anim played.

Sources: `resources/webserver/webviz/js/socket.js`; `webVizModules/intents.js`; `behaviorComponentMessageHandler.cpp` ~443–465; `testUserIntentMap.cpp` ~537–557; `userIntentComponent.h` ~253–266.

### Anti-APIs (do not use)

| API | Why |
|-----|-----|
| `robot.Conn.AppIntent` for weather | No `app_intent` mapping; single `param` string cannot fill `UserIntent_WeatherResponse` |
| `/api-sdk/cloud_intent` | Misnamed — still AppIntent |
| `IntentPass` without stream | Needs active chipper voice stream |
| HTTP `GET /triggerIntent?type=cloud&intent=<json>` | Handler does **not** percent-decode; encoded JSON is treated as literal intent name (verified 2026-09-02). Name-only works; rich params do not |
| `/sendAppMessage` | `ANKI_DEV_CHEATS` only; AppIntent path |
| `ambientReact` / SayText for weather | Product rule — firmware weather anim, not LLM line |
| BehaviorControl OVERRIDE around fire | Comment in `FireWeatherIntent`: OVERRIDE blocks firmware weather |
| Editing archived `shared/patches/add-behavior-tick.py` | SoT is monorepo `wire-pod/` |

### Allowed building blocks for the fix

1. Keep `botWeatherLocationUnits` + `weatherParser("what's the weather", …)` + `localDatetime == "test"` → `KGSim` error path (`behavior_tick.go`, `weather.go`, `intentparam.go`).
2. Keep weather param map keys exactly as today (`condition`, `is_forecast`, `local_datetime`, `speakable_location_string`, `temperature`, `temperature_unit`).
3. Resolve robot LAN IP from `vars.BotInfo.Robots` / `lookupBot` (`robotsession/registry.go` — today builds `ip:443`; strip to host for `:8888`).
4. Open WebSocket to `ws://<ip>:8888/socket`; send subscribe + data envelopes matching WebViz.
5. Prefer Go stdlib `gorilla/websocket` or whatever wire-pod already vendors — **check `go.mod` before adding a dep**; if none, add the same client the repo already uses elsewhere, or stdlib-only if present.
6. Optional: `SpeechVolumeHoldFor` may still apply if weather TTS is loud; keep unless it proves unnecessary on WS path.
7. No vector-ai / morning_weather.py changes required for delivery.
8. No firmware / `user_intent_map.json` changes for this plan.

---

## Phase 1 — Helper: inject cloud intent via WebViz WS

**What to implement**

Add a small helper in chipper (suggested name/location: `ttr/webviz_intent.go` next to `behavior_tick.go`) that:

1. Takes `robotIP string` and `cloudMicJSON string` (already the FromString shape).
2. Dials `ws://{robotIP}:8888/socket` with a short timeout (e.g. 5–8s).
3. Sends `{type:subscribe, module:intents}` (matches WebViz client; harmless if redundant).
4. Sends `{type:data, module:intents, data:{intentType:cloud, request: cloudMicJSON}}`.
5. Returns dial/write errors; does not require reading a response (engine may push `current-intent` — log if easy).

**Copy from**

- Envelope shape: live test script / `intents.js` `safeSend({ intentType, request })` + `socket.js` protocol comment.
- CloudMic string: `testUserIntentMap.cpp` CloudMessageTest; voice `context.go` encodes params with `json.Encoder` into `parameters` string.

**Verification**

- [ ] `go test` / compile chipper package containing the helper.
- [ ] Manual: helper against `192.168.50.189` with fixed Cloudy/22 payload → WebViz Intents tab or robot anim (same as 2026-09-02 proof).
- [ ] Grep: helper must not call `AppIntent`.

**Anti-patterns**

- Do not HTTP `/triggerIntent` for parameterized weather.
- Do not put expanded `params` object in the outer JSON for this path — use string `parameters` (WebViz calls `SetCloudIntentPendingFromString`, not ExpandedJSON).

---

## Phase 2 — Rewire `FireWeatherIntent`

**What to implement**

In `behavior_tick.go` `FireWeatherIntent`:

1. Keep weatherParser + error `KGSim` branch unchanged.
2. Build param map as today; `json.Marshal` the map → that bytes string is the **inner** `parameters` value.
3. Build outer CloudMic JSON: `{"intent":"intent_weather_extend","parameters": <inner string + optional "\n">}`.
4. Resolve IP: from `lookupBot(esn)` / BotInfo (host only, not `:443`).
5. Call Phase 1 helper instead of `sess.Unary` + `AppIntent`.
6. Update function comment: WebViz cloud inject, not AppIntent; note OVERRIDE/AppIntent history.
7. Remove or gate unused `robotsession` get if only used for AppIntent here (keep session if still needed for volume hold — decide with evidence).

**Verification**

- [ ] `rg 'AppIntent' behavior_tick.go` — no weather AppIntent left.
- [ ] Chipper log line still prints weather param JSON.
- [ ] Unit-level: if easy, table test that outer JSON has string `parameters` and intent name correct (pure Go, no robot).

**Anti-patterns**

- Do not change VI `morning_weather.py` or tick contract.
- Do not take BehaviorControl.
- Do not set `is_forecast` to `"true"` (wire-pod always uses `"false"` even for forecast speech hours — firmware CoordinateWeather rejects true).

---

## Phase 3 — Docs closeout

**What to implement**

1. Update [FSM-morning-weather.md](../FSM-morning-weather.md) delivery bullet: chipper fires via WebViz cloud inject (`:8888/socket`), not AppIntent.
2. Patch parent plan [2026-08-29-morning-weather-intent.md](./2026-08-29-morning-weather-intent.md): strike “AppIntent / intent-graph is how user weather is delivered” for the **proactive** path; note voice still uses IntentPass stream; proactive uses WebViz cloud side channel (proven 2026-09-02).
3. Optional FAQ one-liner if FAQ mentions AppIntent for morning weather.
4. Mark this plan status → implemented chipper when Phase 2 ships (done; Phase 4 E2E still pending).

**Verification**

- [x] Docs no longer claim AppIntent delivers morning weather.
- [x] Owner guide still says dual enable + Work Day WORKING gates.

---

## Phase 4 — End-to-end verification

1. **Manual WS regression** (optional): same uv/websocket one-shot as 2026-09-02.
2. **Chipper path:** enable `morning_weather` + workday WORKING + in window; or temporarily force `FireWeatherIntent(esn)` from a dev hook / log-driven call.
3. Confirm robot plays weather (not unmatched / not ICantDoThat).
4. Confirm API-failure path still `KGSim`s the configure message when weather disabled.
5. Confirm spoken “what’s the weather” still works (IntentPass path untouched).

**Success criteria**

- Morning / forced `FireWeatherIntent` → firmware weather anim + TTS with real API params.
- No AppIntent for weather in chipper tick path.
- Voice weather unchanged.

---

## Out of scope

- Firmware `app_intent` mapping or `OnAppIntent` JSON merge.
- Teaching `/triggerIntent` to URL-decode (nice follow-up; not required).
- Afternoon weather, LLM weather lines, companion-mode ticks.
- Changing `is_forecast` semantics in `weather.go`.

---

## Execution note

Implement in **`/home/cam-test/repos/vectorStuff/wire-pod`** (chipper) + VI docs under **`VectorIntelligence/docs/`**. Prefer `/do` or a focused chipper session; Phase 1–2 are chipper-only code.
