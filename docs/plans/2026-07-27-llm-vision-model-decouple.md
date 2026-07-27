# Plan: Decouple ambient / vision from `LLM_MODEL` via `LLM_VISION_MODEL`

**Date:** 2026-07-27  
**Goal:** Let `LLM_MODEL` be a powerful (possibly expensive) conversation model only. Ambient + chat-with-image use `LLM_VISION_MODEL`. Proactive one-shots (greeting, sensor) and other background text use `LLM_SUMMARY_MODEL`.  
**Repo:** VectorIntelligence (`shared/vector-ai/`)  
**Do not use `LLM_SUMMARY_MODEL` for images** — summary is text-only cheap work; many lite models are weak or non-multimodal.

**How to execute:** Run phases in order with `/do` or a fresh agent per phase. Each phase is self-contained with doc references and a verification checklist.

---

## Phase 0 — Documentation discovery (consolidated)

### Allowed APIs (existing only — do not invent)

| API | Source | Notes |
|-----|--------|--------|
| `MODEL = os.getenv("LLM_MODEL", …)` | `shared/vector-ai/llm.py:31` | Main model; today used for chat **and** ambient vision |
| `SUMMARY_MODEL = os.getenv("LLM_SUMMARY_MODEL", MODEL)` | `llm.py:33` | Text-only cheap path; fallback to `MODEL` |
| `llm_chat_once(..., model=Optional[str], …)` | `llm.py:148–165` | Body uses `model or MODEL` — **already supports overrides** |
| `one_shot_spoken_line(..., model=…)` | `llm.py:208+` | Greeting/sensor wrapper; already accepts `model=` |
| `llm_sentence_stream(messages, temperature=…)` | `llm.py:301–333` | **Hardcodes** `"model": MODEL` — no override today |
| `stream_sentences_with_filler(messages, temperature, …)` | `chat_flow.py:51–58` | Wraps `llm_sentence_stream` |
| `generate(messages, temperature)` | `chat_flow.py:204+` | Detects `has_image`; streams via filler wrapper |
| Ambient call | `routes/ambient.py:296–308` | `llm_chat_once(..., model=MODEL, tag="ambient")` |
| Greeting call | `routes/greeting.py:102–114` | `one_shot_spoken_line(..., model=MODEL, tag="greeting")` |
| Sensor call | `routes/sensor.py:68–80` | `one_shot_spoken_line(..., model=MODEL, tag="sensor_reaction")` |
| Health | `routes/health.py:24–44` | Exposes `model`, `summary_model` |
| Env template | `shared/vector-ai/env-default:14–20` | Documents models as `.env` only |
| Config ownership | `AGENTS.md` ~253–273 | `LLM_*` stays in `.env`; never migrate to `pod.conf` |
| Joke dual-model (pattern only) | `behaviors/config.py` + `joke_sources.py` | Optional overrides; empty → `MODEL` — **do not copy into pod.conf for vision** |

### Current call map (evidence)

| Path | Model today | Temp | Multimodal? | Target model |
|------|-------------|------|-------------|--------------|
| Chat stream (text) | `MODEL` | `LLM_TEMPERATURE` | no | **`MODEL`** (unchanged) |
| Chat stream (getImage reply) | `MODEL` | same | **yes** | **`VISION_MODEL`** |
| Ambient glance | `MODEL` | `LLM_AMBIENT_TEMPERATURE` | **yes** | **`VISION_MODEL`** |
| Mood / convo summary / desk extract | `SUMMARY_MODEL` | summary/mood | no | **`SUMMARY_MODEL`** |
| Greeting | `MODEL` | `LLM_GREETING_TEMPERATURE` | no | **`SUMMARY_MODEL`** |
| Sensor | `MODEL` | `LLM_SENSOR_TEMPERATURE` | no | **`SUMMARY_MODEL`** |

### Anti-patterns (do not do)

- Do **not** point ambient at `SUMMARY_MODEL` by default (lite may lack vision).
- Do **not** put `LLM_VISION_MODEL` in `pod.conf` / `BEHAVIOR_RELOCATE_KEYS` (same rule as `LLM_MODEL` — `pod_conf_io.py`).
- Do **not** invent a new HTTP client; use `llm_chat_once` / `one_shot_spoken_line` / `llm_sentence_stream`.
- Do **not** honor `ChatRequest.model` from clients (existing design: env owns models — `routes/chat.py`).
- Do **not** change chipper / Wire-Pod Knowledge.Model — brain ignores it for custom endpoint.
- Do **not** use desk_extract for images — it already correctly uses `SUMMARY_MODEL` on **text**.

### Product decision (locked for this plan)

| Role | Env key | Default if unset | Callers |
|------|---------|------------------|---------|
| **Conversation only** (text chat stream) | `LLM_MODEL` | `google/gemini-2.0-flash` | `llm_sentence_stream` when no image |
| **Proactive + background text** | `LLM_SUMMARY_MODEL` | falls back to `MODEL` | mood, convo summary, desk extract, **greeting**, **sensor** |
| **Multimodal** | **`LLM_VISION_MODEL`** | falls back to `MODEL` | ambient + chat-with-image |

**Template recommendation** in `env-default`: keep vision multimodal-capable; set summary to a cheap text model; operators raise `LLM_MODEL` for conversation quality without paying that rate for greetings, sensor quips, or ambient.

---

## Phase 1 — Add `VISION_MODEL` constant + user-facing config

### What to implement

1. **Copy the SUMMARY_MODEL binding pattern** from `llm.py:31–33` and add:

```python
# Multimodal path (ambient glances + chat getImage replies).
# Falls back to MODEL so existing .env installs behave unchanged.
VISION_MODEL = os.getenv("LLM_VISION_MODEL", MODEL)
```

2. **Copy `env-default` model block** (`env-default:14–20`). Rewrite comments so roles are clear:

```env
# Main conversation model (interactive chat only).
# Can be a stronger / non-vision model if LLM_VISION_MODEL is set.
LLM_MODEL=google/gemini-2.0-flash

# Multimodal vision (ambient camera glances + chat replies that include a photo).
# Defaults to LLM_MODEL if unset. Prefer a vision-capable OpenRouter slug.
LLM_VISION_MODEL=google/gemini-2.0-flash

# Cheap/fast text model for: mood, conversation summaries, desk extract,
# proactive greetings, and sensor quips (pickup/pet/fall). Not for images.
LLM_SUMMARY_MODEL=google/gemini-2.0-flash-lite
```

3. **Expose on health** — copy `routes/health.py:24–29` pattern; add `"vision_model": VISION_MODEL` and import `VISION_MODEL` from `llm`.

4. **Docs tables** (same row style as `LLM_SUMMARY_MODEL`):
   - `AGENTS.md` LLM table (~253–273) — update `LLM_MODEL` purpose to “interactive chat”; expand `LLM_SUMMARY_MODEL` to include greeting + sensor; add `LLM_VISION_MODEL`
   - `README.md` OpenRouter / env table (~207–218)
   - `NEXT_STEPS.md` sample `.env` if present
   - Install hint comments only if they already list `LLM_SUMMARY_MODEL` (`linux/install.sh`, `windows/install.ps1`)

### Documentation references

- Pattern twin: `SUMMARY_MODEL` in `llm.py:32–33`
- Template: `env-default:14–20`
- Ownership rule: `AGENTS.md` “LLM (`.env` only)”
- Joke tiering narrative (optional prose): `docs/FSM-jokes-at-idle.md` — **keep vision key in `.env`**, not pod.conf

### Verification checklist

- [ ] `grep -n 'VISION_MODEL\|LLM_VISION_MODEL' shared/vector-ai/llm.py shared/vector-ai/env-default shared/vector-ai/routes/health.py`
- [ ] `grep -n 'LLM_VISION_MODEL' AGENTS.md README.md` (docs updated)
- [ ] Docs say greeting/sensor use summary model
- [ ] Confirm `LLM_VISION_MODEL` is **not** added to `BEHAVIOR_RELOCATE_KEYS` in `shared/pod_conf_io.py`
- [ ] No call sites changed yet — ambient/greeting/sensor still use `MODEL` until later phases

### Anti-pattern guards

- No default that points vision at `SUMMARY_MODEL`
- No pod.conf key
- No runtime reloader — import-time env is fine (restart vector-ai after `.env` change; same as today)

---

## Phase 2 — Ambient path uses `VISION_MODEL`

### What to implement

In `routes/ambient.py`:

1. Change import from `llm` (today L13–19):

```python
from llm import (
    LLM_AMBIENT_TEMPERATURE,
    VISION_MODEL,  # was MODEL
    _llm_timeout,
    image_payload_too_large,
    llm_chat_once,
)
```

2. At the ambient LLM call (~L296–308), only change the model kwarg:

```python
    model=VISION_MODEL,  # was MODEL
    temperature=LLM_AMBIENT_TEMPERATURE,
    ...
    tag="ambient",
```

3. Leave post-vision **desk extract** on `SUMMARY_MODEL` (already correct in `desk_extract.py` — text only).

### Documentation references

- Call site: `routes/ambient.py:296–308`
- Transport: `llm_chat_once` already accepts `model=` (`llm.py:148–165`)
- Chipper remains `POST /v1/ambient` — **no Go changes**

### Verification checklist

- [ ] `rg -n 'model=MODEL|model=VISION_MODEL' shared/vector-ai/routes/ambient.py` → only `VISION_MODEL`
- [ ] Ambient tests still pass (monkeypatch `routes.ambient.llm_chat_once`)
- [ ] Optional: distinct `LLM_VISION_MODEL` slug shows up for tag `ambient`

### Anti-pattern guards

- Do not change ambient temperature keys
- Do not pass images through summary/mood paths
- Do not edit chipper ambient Go

---

## Phase 3 — Greeting and sensor use `SUMMARY_MODEL`

### What to implement

Both already call `one_shot_spoken_line(..., model=MODEL, ...)`. Swap to `SUMMARY_MODEL` only; keep path-specific temperatures.

**`routes/greeting.py`** (imports ~L10, call ~L102–114):

```python
from llm import LLM_GREETING_TEMPERATURE, SUMMARY_MODEL, _llm_timeout, one_shot_spoken_line
...
    line = await one_shot_spoken_line(
        [...],
        model=SUMMARY_MODEL,  # was MODEL
        temperature=LLM_GREETING_TEMPERATURE,
        ...
        tag="greeting",
    )
```

**`routes/sensor.py`** (same pattern ~L68–80):

```python
from llm import LLM_SENSOR_TEMPERATURE, SUMMARY_MODEL, _llm_timeout, one_shot_spoken_line
...
    clean = await one_shot_spoken_line(
        [...],
        model=SUMMARY_MODEL,  # was MODEL
        temperature=LLM_SENSOR_TEMPERATURE,
        ...
        tag="sensor_reaction",
    )
```

Temperatures stay on `LLM_GREETING_TEMPERATURE` / `LLM_SENSOR_TEMPERATURE` (creative sampling on a cheap model is fine).

### Documentation references

- Greeting: `routes/greeting.py:102–114`
- Sensor: `routes/sensor.py:68–80`
- Wrapper: `llm.one_shot_spoken_line` (`llm.py:208+`) — already forwards `model=` to `llm_chat_once`
- Existing summary consumers for consistency: `routes/mood.py`, `chat_flow._summarise_conversation`, `desk_extract.py`

### Verification checklist

- [ ] `rg -n 'model=MODEL|model=SUMMARY_MODEL' shared/vector-ai/routes/greeting.py shared/vector-ai/routes/sensor.py` → only `SUMMARY_MODEL`
- [ ] Sensor/greeting tests that monkeypatch `one_shot_spoken_line` still pass (`test_service_modules.py` ~822)
- [ ] Optional: assert `model=` kwarg in a focused test (Phase 5)

### Anti-pattern guards

- Do **not** point greeting/sensor at `VISION_MODEL` (text-only)
- Do **not** change greeting/sensor temperature env keys
- Do **not** change chat stream to summary for normal conversation

---

## Phase 4 — Chat-with-image uses `VISION_MODEL` (stream override)

**Why:** If `LLM_MODEL` becomes a strong text model that is not multimodal, getImage replies break unless the stream can select `VISION_MODEL` when `has_image`.

### What to implement

1. **Extend `llm_sentence_stream`** — copy signature style from `llm_chat_once` (`model: Optional[str] = None`):

```python
async def llm_sentence_stream(
    messages: list,
    temperature: Optional[float] = None,
    *,
    model: Optional[str] = None,
) -> AsyncIterator[str]:
    ...
    body = {
        "model": model or MODEL,
        "messages": messages,
        ...
    }
```

2. **Thread through `stream_sentences_with_filler`** (`chat_flow.py:51–58`):

```python
async def stream_sentences_with_filler(
    messages, temperature, filler_enabled=True, *, model=None
):
    agen = llm_sentence_stream(messages, temperature, model=model).__aiter__()
    ...
```

3. **In `generate`**, after `has_image` is finalized (post-sanitize):

```python
stream_model = VISION_MODEL if has_image else None  # None → MODEL in stream
async for sentence in stream_sentences_with_filler(
    prepared, temp, filler_enabled=not has_image, model=stream_model
):
```

4. Import `VISION_MODEL` in `chat_flow.py` next to other `llm` imports.

5. **SSE debug label** (optional): if chat_flow echoes model in SSE chunks, use the effective stream model for image turns.

### Documentation references

- Stream body today: `llm.py:326–333` (hardcoded `MODEL` → `model or MODEL`)
- Filler wrapper: `chat_flow.py:51–58`
- `has_image` branch: `chat_flow.py:209–273`
- Text-only chat must remain on `MODEL` (pass `model=None`)

### Verification checklist

- [ ] Text-only chat: stream body model is still `MODEL` / `LLM_MODEL`
- [ ] `has_image=True` after sanitize: stream body model is `VISION_MODEL`
- [ ] Existing stream fakes accept `model=None` / `**kwargs` (`test_service_modules.py`)
- [ ] Vision-intent early return unchanged — `chat_flow.py:233–238`

### Anti-pattern guards

- Do not use vision model for text-only chat
- Do not use client `req.model`
- Do not change filler disable logic for images
- Greeting/sensor are **not** on this path (already handled in Phase 3)

---

## Phase 5 — Tests

### What to implement

**A. Ambient asserts vision model kwarg**

```python
def test_ambient_passes_vision_model(monkeypatch, tmp_path):
    # Setup MEMORY / quiet / ambient state like other ambient tests
    seen = {}
    async def _llm(messages, *, model=None, tag=None, **kw):
        seen["model"] = model
        seen["tag"] = tag
        return "PRESENCE: empty\nNOTHING"
    monkeypatch.setattr("routes.ambient.llm_chat_once", _llm)
    monkeypatch.setattr("routes.ambient.VISION_MODEL", "vision/test-slug", raising=False)
    asyncio.run(ambient(AmbientRequest(image="fakejpg")))
    assert seen.get("model") == "vision/test-slug"
    assert seen.get("tag") == "ambient"
```

**B. Greeting / sensor assert summary model**

```python
# Monkeypatch one_shot_spoken_line at routes.greeting / routes.sensor
# Set routes.greeting.SUMMARY_MODEL = "summary/test-slug"
# Assert seen["model"] == "summary/test-slug" (and tag greeting / sensor_reaction)
```

**C. Health exposes vision_model**

```python
# assert "vision_model" in body alongside model / summary_model
```

**D. Stream model override** — fake stream capturing `model=` when last message has image parts; text-only leaves model `None` / `MODEL`.

**E. Migration guard**

```python
assert "LLM_VISION_MODEL" not in BEHAVIOR_RELOCATE_KEYS
```

### Documentation references

- Ambient tests: `test_ambient_presence.py`
- Sensor fake: `test_service_modules.py` ~822 (`routes.sensor.one_shot_spoken_line`)
- Joke `model=` fake: `test_joke_idle.py:342`

### Verification checklist

- [ ] Ambient + greeting + sensor model-assert tests green
- [ ] Health test green
- [ ] No live OpenRouter required

### Anti-pattern guards

- Prefer patching `routes.*.VISION_MODEL` / `routes.*.SUMMARY_MODEL` (import bindings)
- Do not assert live network model IDs

---

## Phase 6 — Verification (full)

### Checklist

1. **Config surface**
   - [ ] `LLM_VISION_MODEL` in `llm.py`, `env-default`, `/health`, AGENTS/README
   - [ ] Fallback: unset `LLM_VISION_MODEL` → same slug as `LLM_MODEL`
   - [ ] Docs: summary covers mood + summary + desk + **greeting + sensor**

2. **Call sites**

| Path | Expected model |
|------|----------------|
| Ambient | `VISION_MODEL` |
| Chat + image | `VISION_MODEL` |
| Chat text-only | `MODEL` |
| Mood / convo summary / desk_extract | `SUMMARY_MODEL` |
| Greeting | `SUMMARY_MODEL` |
| Sensor | `SUMMARY_MODEL` |

3. **Anti-pattern greps**

```bash
rg -n 'model=MODEL' shared/vector-ai/routes/ambient.py    # expect 0
rg -n 'model=MODEL' shared/vector-ai/routes/greeting.py   # expect 0
rg -n 'model=MODEL' shared/vector-ai/routes/sensor.py     # expect 0
rg -n 'SUMMARY_MODEL' shared/vector-ai/routes/ambient.py  # expect 0 (no images on summary)
rg -n 'LLM_VISION_MODEL' shared/pod_conf_io.py            # expect 0
rg -n 'model or MODEL' shared/vector-ai/llm.py            # stream override present
```

4. **Tests** — ambient / greeting / sensor / health / stream green; full suite if available

5. **Manual operator recipe**

```env
# Strong talker — interactive chat only
LLM_MODEL=anthropic/claude-sonnet-4

# Cheap multimodal for desk glances + photo answers
LLM_VISION_MODEL=google/gemini-2.0-flash

# Cheap text for mood, summaries, desk extract, greetings, sensor quips
LLM_SUMMARY_MODEL=google/gemini-2.0-flash-lite
```

```bash
curl -s http://127.0.0.1:8090/health | jq '{model, vision_model, summary_model}'
```

### Success criteria

- Raising `LLM_MODEL` only affects interactive chat cost/quality.
- Ambient uses vision model; greeting/sensor use summary model.
- Zero behavior change when `LLM_VISION_MODEL` is omitted (defaults to `MODEL`). Summary paths already default to `MODEL` if `LLM_SUMMARY_MODEL` unset.

---

## Out of scope

- Chipper ambient Go / getImage capture changes
- Hot-reload of env constants
- Client-supplied model on `/v1/chat/completions`
- Pointing vision at `LLM_SUMMARY_MODEL` by default
- Separate models per sensor event or greeting subtype
- OpenRouter model ranking / cost analytics
- Joke generate/critic still fall back to `MODEL` unless pod.conf overrides (unchanged; optional follow-up)

---

## Suggested commit sequence

1. `feat(llm): add LLM_VISION_MODEL + health/docs` (Phase 1)
2. `feat(ambient): route glances through VISION_MODEL` (Phase 2)
3. `feat(proactive): greeting and sensor use SUMMARY_MODEL` (Phase 3)
4. `feat(chat): use VISION_MODEL for multimodal stream turns` (Phase 4)
5. `test(llm): assert model selection for ambient/greeting/sensor/chat` (Phase 5)

---

## Confidence notes

| Item | Confidence |
|------|------------|
| Ambient is primary image consumer of `MODEL` | High |
| Greeting/sensor already pass `model=` to `one_shot_spoken_line` | High |
| Stream must gain `model=` for chat vision split | High |
| SUMMARY must not become default vision | High |
| Greeting/sensor on summary is product-correct for cost | High (operator decision) |
