# vector-ai HTTP endpoints

Copy-paste debugging / chipper process imitation.

```bash
export BASE=http://127.0.0.1:8090   # AI_PORT (pod.conf / install default)
# jq is optional (pretty-print); drop "| jq" if not installed
```

| | |
|---|---|
| **Bind** | localhost only (`127.0.0.1`) |
| **Auth** | none |
| **PUT** | none (GET / POST / PATCH / DELETE only) |
| **OpenAPI** | `$BASE/docs`, `$BASE/openapi.json` (FastAPI) |
| **State dump** | `linux/dump-vector-ai-state.sh --port 8090` |

---

## Method matrix

| Method | Path | Domain |
|--------|------|--------|
| GET | `/health` | Health |
| POST | `/v1/chat/completions` | Chat |
| POST | `/v1/chat/session/message` | Chat |
| GET | `/v1/chat/session` | Chat |
| DELETE | `/v1/chat/session` | Chat |
| POST | `/v1/ambient` | Ambient |
| GET | `/v1/ambient/state` | Ambient |
| POST | `/v1/ambient/quiet` | Ambient |
| POST | `/v1/sensor_reaction` | Sensor |
| POST | `/v1/state/face_seen` | Face |
| GET | `/v1/state/face` | Face |
| POST | `/v1/proactive_greeting` | Greeting |
| GET | `/v1/mood` | Mood |
| POST | `/v1/mood/reflect` | Mood |
| GET | `/v1/memory/list` | Memory |
| POST | `/v1/memory/remember` | Memory |
| POST | `/v1/memory/forget` | Memory |
| POST | `/v1/memory/clear` | Memory |
| GET | `/v1/soul` | Soul |
| PATCH | `/v1/soul` | Soul |
| POST | `/v1/soul/dream` | Soul |
| POST | `/v1/soul/reflect_goals` | Soul |
| POST | `/v1/behaviors/tick` | Behaviors |
| GET | `/v1/behaviors/state` | Behaviors |
| GET | `/v1/behaviors/{behavior_id}` | Behaviors |

---

## Chipper process map

Who normally hits what (production path).

| Caller | Endpoints |
|--------|-----------|
| **Wire-Pod / chipper STT knowledge** | `POST /v1/chat/completions` |
| **Chipper ambient idle glance** | `POST /v1/ambient` → optional speak from `text` |
| **Chipper behavior loop** | `POST /v1/behaviors/tick` (occupancy/face); may follow with face probe if `need_identity` |
| **Chipper face / on-demand probe** | Idle enrolled `ProbeFace` (sparse ≤2 min / `need_identity`) and voice-start `ObserveFaceBriefly` → `POST /v1/state/face_seen`; enrolled may return weather `intent`; may then `POST /v1/proactive_greeting` |
| **Chipper sensors** | `POST /v1/sensor_reaction` (`pickup` / `putdown` / `pet`) |
| **Ops / keyboard inject** | `POST /v1/chat/session/message`, session GET/DELETE |
| **Ops / debug dump** | all GETs + `linux/dump-vector-ai-state.sh` |
| **Ops / memory & soul** | memory + soul routes (manual / tools) |
| **Ops / quiet desk** | `POST /v1/ambient/quiet` |
| **Ops / mood** | `GET /v1/mood`, `POST /v1/mood/reflect` |

---

## Health

### `GET /health`

Readiness: models, `api_key_set`, per-path temperatures, debug flag.

```bash
curl -s "$BASE/health" | jq
```

---

## Chat

### `POST /v1/chat/completions`

Wire-Pod STT knowledge path. Body: `messages[]` (role/content). Always SSE (`text/event-stream`). Request `temperature` is logged but **not** applied (uses `LLM_TEMPERATURE`). **LLM cost.**

```bash
curl -sN -X POST "$BASE/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"hi"}]}'
```

### `POST /v1/chat/session/message`

Keyboard/text inject into server-side multi-turn session (bypasses STT). **Required:** `text`. Optional: `session_id` (default `default`), `stream` (default false → JSON reply). **LLM cost.**

```bash
curl -s -X POST "$BASE/v1/chat/session/message" \
  -H 'Content-Type: application/json' \
  -d '{"text":"hello"}'
```

### `GET /v1/chat/session`

Inspect session messages. Query: `session_id` (default `default`).

```bash
curl -s "$BASE/v1/chat/session?session_id=default" | jq
```

### `DELETE /v1/chat/session`

Clear session history. Query: `session_id`.

```bash
curl -s -X DELETE "$BASE/v1/chat/session?session_id=default" | jq
```

---

## Ambient

### `POST /v1/ambient`

Chipper idle glance. Body: `image` (base64 JPEG). Runs PRESENCE + SCENE + novelty; may LLM-speak. **Costly** (vision/LLM) when not quiet.

**Gates / side effects:**
- Quiet on → **no vision/LLM**; `{"text":"","quiet":true}` (toggle via `POST /v1/ambient/quiet`)
- Long gap since last ambient call (~4h sleep gap) → may clear sticky presence and schedule a debounced dream job
- First glance with no prior SCENE = baseline only (no spoken novelty)

```bash
# Linux: base64 -w0; macOS: base64 < desk.jpg | tr -d '\n'
B64=$(base64 -w0 desk.jpg)
curl -s -X POST "$BASE/v1/ambient" \
  -H 'Content-Type: application/json' \
  -d "{\"image\":\"$B64\"}"
```

### `GET /v1/ambient/state`

Current ambient/quiet bookkeeping for ops.

```bash
curl -s "$BASE/v1/ambient/state" | jq
```

### `POST /v1/ambient/quiet`

Toggle quiet mode. Body: `{"on":true|false}`.

```bash
curl -s -X POST "$BASE/v1/ambient/quiet" \
  -H 'Content-Type: application/json' \
  -d '{"on":true}'
```

---

## Sensor

### `POST /v1/sensor_reaction`

Pickup / putdown / pet reactions. Body: `event` (`pickup`|`putdown`|`pet`); optional `avoid[]` (recent phrases not to repeat). **LLM cost.**

```bash
curl -s -X POST "$BASE/v1/sensor_reaction" \
  -H 'Content-Type: application/json' \
  -d '{"event":"pet"}'
```

---

## Face

### `POST /v1/state/face_seen`

Chipper reports a face. Body: `face_id` (int); `name` optional (empty/missing → stranger). **Mutates face cache + presence** (`note_face` / `note_person`; sticky occupancy). **Does not arm Work Day.** No LLM.

Enrolled (`face_id > 0` and a name) may run a **weather-only** tick and return `intent` (`intent_weather_extend` or `""`). Stranger → `{ok, is_stranger: true, intent: ""}` (no tick). Response is `{ok, is_stranger, intent}` — no `speak`. Chipper `notifyFaceSeen` fires `FireWeatherIntent` when `intent` is the weather string; ignores `speak`.

```bash
curl -s -X POST "$BASE/v1/state/face_seen" \
  -H 'Content-Type: application/json' \
  -d '{"face_id":1,"name":"Cam"}'
```

### `GET /v1/state/face`

Current face cache / soft identity.

```bash
curl -s "$BASE/v1/state/face" | jq
```

---

## Greeting

### `POST /v1/proactive_greeting`

Known-person proactive hello. Body: `face_id` + `name` required (soft-fail to `{"text":""}` if `face_id<=0` or empty name — not always 422).

**Always updates face/presence clocks.** Speaks only if “arrived”: long visual absence (~10m `GREETING_ABSENCE_GAP`), not sticky continuous desk session, no recent voice/convo. Otherwise `{"text":""}` and **no LLM**. **LLM cost only when text is non-empty.**

```bash
curl -s -X POST "$BASE/v1/proactive_greeting" \
  -H 'Content-Type: application/json' \
  -d '{"face_id":1,"name":"Cam"}'
```

---

## Mood

### `GET /v1/mood`

Current mood digest / state.

```bash
curl -s "$BASE/v1/mood" | jq
```

### `POST /v1/mood/reflect`

Force LLM mood refresh. No body. **LLM cost.** Persists mood (soul / process state / memory state key).

```bash
curl -s -X POST "$BASE/v1/mood/reflect"
```

---

## Memory

### `GET /v1/memory/list`

List stored memories.

```bash
curl -s "$BASE/v1/memory/list" | jq
```

### `POST /v1/memory/remember`

Add a memory. Body: `{"text":"..."}`.

```bash
curl -s -X POST "$BASE/v1/memory/remember" \
  -H 'Content-Type: application/json' \
  -d '{"text":"Cam likes coffee"}'
```

### `POST /v1/memory/forget`

Remove by id or substring. Body: `{"target":"id-or-substring"}`.

```bash
curl -s -X POST "$BASE/v1/memory/forget" \
  -H 'Content-Type: application/json' \
  -d '{"target":"coffee"}'
```

### `POST /v1/memory/clear`

**Destructive** — wipe all memories. No body.

```bash
curl -s -X POST "$BASE/v1/memory/clear"
```

---

## Soul

### `GET /v1/soul`

Ops `llm_view` (long snake_case keys; no raw unix / no base64 frames).

```bash
curl -s "$BASE/v1/soul" | jq
```

### `PATCH /v1/soul`

Patch goals and/or dream; force-saves `soul.json`. Body must include **at least one** of (empty body → 400):

- `three_goals_i_currently_hold`: string array (normalized to 3)
- `last_night_i_dreamed_about`: string

```bash
curl -s -X PATCH "$BASE/v1/soul" \
  -H 'Content-Type: application/json' \
  -d '{"last_night_i_dreamed_about":"test dream"}'
```

### `POST /v1/soul/dream`

Run dream+goals LLM job. Optional body: `{"force":true}` (default true; bypasses 20h debounce). **Costly / mutates soul.**

```bash
curl -s -X POST "$BASE/v1/soul/dream" \
  -H 'Content-Type: application/json' \
  -d '{"force":true}'
```

### `POST /v1/soul/reflect_goals`

Goals-focused refresh (same job family, cooler temp). Optional `{"force":true}`. **Costly / mutates soul.**

```bash
curl -s -X POST "$BASE/v1/soul/reflect_goals" \
  -H 'Content-Type: application/json' \
  -d '{"force":true}'
```

---

## Behaviors

### `POST /v1/behaviors/tick`

Chipper presence tick: advance FSMs; may return one `speak` line, a firmware `intent` (e.g. `intent_weather_extend`), and/or `need_identity`. All fields optional (`{}` ok):

- `occupied` (bool)
- `face` `{face_id, name, is_stranger}`
- `on_charger` (bool)
- `voice_recent` (bool)

```bash
curl -s -X POST "$BASE/v1/behaviors/tick" \
  -H 'Content-Type: application/json' \
  -d '{"occupied":true,"face":{"face_id":1,"name":"Cam","is_stranger":false}}'
```

Empty tick:

```bash
curl -s -X POST "$BASE/v1/behaviors/tick" \
  -H 'Content-Type: application/json' \
  -d '{}'
```

Arbiter allow for morning weather **returns** `intent` but does **not** burn the daily lock. Chipper POSTs the commit after a successful robot inject (or speak fallback):

### `POST /v1/behaviors/morning_weather/fired`

```bash
curl -s -X POST "$BASE/v1/behaviors/morning_weather/fired"
```

### `GET /v1/behaviors/state`

Multi-FSM index envelope v1 (`presence` / `arbiter` / `behaviors.<id>` cards). Does not speak.

```bash
curl -s "$BASE/v1/behaviors/state" | jq
```

### `GET /v1/behaviors/{behavior_id}`

Per-FSM ops detail. Registered ids include `workday`, `joke_idle`, `morning_weather`. 404 if unknown. Does not speak or advance tick policy.

```bash
curl -s "$BASE/v1/behaviors/workday" | jq
curl -s "$BASE/v1/behaviors/joke_idle" | jq
curl -s "$BASE/v1/behaviors/morning_weather" | jq
```

---

## Dump helper

Read-only GETs in one shot (not an HTTP route; shell helper):

```bash
# from VectorIntelligence repo
./linux/dump-vector-ai-state.sh --port 8090
./linux/dump-vector-ai-state.sh --base http://127.0.0.1:8090 --session-id default
./linux/dump-vector-ai-state.sh --openapi   # also /openapi.json discovery
```

---

## Side-effect cheat sheet

| Endpoint | Risk |
|----------|------|
| `POST /v1/memory/clear` | **Destructive** — all memories gone |
| `POST /v1/memory/remember` / `forget` | Mutates SQLite memories |
| `DELETE /v1/chat/session` | Clears that session history |
| `POST /v1/ambient` | Vision + LLM when not quiet; may speak / presence; sleep gap may clear sticky + schedule dream |
| `POST /v1/ambient/quiet` | Toggles quiet (gates ambient vision/LLM) |
| `POST /v1/chat/completions` | LLM; SSE stream |
| `POST /v1/chat/session/message` | LLM; mutates session |
| `POST /v1/sensor_reaction` | LLM speak line |
| `POST /v1/state/face_seen` | Face + presence mutation; enrolled may return weather `intent` (does **not** arm Work Day; no LLM) |
| `POST /v1/proactive_greeting` | Always updates face/presence; LLM only if greets |
| `POST /v1/mood/reflect` | LLM mood rewrite + persist |
| `POST /v1/soul/dream` | **Costly** LLM job; mutates soul + save |
| `POST /v1/soul/reflect_goals` | **Costly** LLM job; mutates soul + save |
| `PATCH /v1/soul` | Persists goals/dream to disk |
| `POST /v1/behaviors/tick` | Advances FSM clocks; may return `speak` |
