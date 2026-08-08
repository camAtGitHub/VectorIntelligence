# Vector Intelligence FAQ

## Is ambient mode called/triggered from wire-pod?

**Yes.** Ambient is **driven by Wire-Pod chipper**, not by vector-ai on a timer.

1. At chipper startup, `StartAmbientLoop()` is launched (patched into `startserver.go` via `shared/patches/add-ambient-loop.py` → `go ttr.StartAmbientLoop()`).
2. Per enrolled robot, `runAmbientLoop` sleeps on the 3‑minute cadence, applies gates, then `ambientObserveOnce` captures a JPEG via robotsession and **HTTP POSTs** it to vector-ai at `POST {vectorAIBase}/v1/ambient` with `{"image": "<base64>"}`.
3. vector-ai only **serves** that endpoint (vision/LLM + quiet mode). It does not schedule ambient glances; chipper is the trigger.

Source: `wire-pod/chipper/pkg/wirepod/ttr/ambient.go` (`askVectorAIAmbient` → `/v1/ambient`).

## How often does the ambient image trigger run? What gates does it have?

**How often:** Every **3 minutes** when idle (`ambientInterval = 3 * time.Minute` in chipper’s `ambient.go`). That is the glance / camera cadence (latency to *notice* a change), not how often he speaks. On consecutive capture/session failures the loop backs off: **3m → 6m → 12m → 24m**, then resets on the first success. The loop also waits **30 seconds** after chipper startup before starting.

**Chipper gates** (skip the glance / reaction if any apply), in order in `runAmbientLoop` / `ambientObserveOnce`:

| Gate | Value | Effect |
|------|--------|--------|
| Recent voice activity | **2 minutes** (`ambientVoiceCooldown`) | Skip glance if user/voice interaction (or ambient speech via `MarkVoiceActivity`) was recent |
| Night / sleep hours | **23:00–07:00** local host clock (`ambientNightStart`/`End`) | Skip glance; docking no longer blocks observation |
| Post-reaction cooldown | **15 minutes** (`ambientReactCooldown`) | After he *spoke* an ambient line, skip further glances until cooldown ends |
| Calm / sleep power mode | `IsCalmPowerMode()` | Skip camera (vision stack powered down); treated as success so no failure backoff |
| Session / capture failure | — | No image; increments fail streak / backoff |
| Empty LLM text | — | Nothing novel; no speak |
| Conversation started mid-think | re-check `recentlyConversed()` | Suppress speaking (observation may already be stored) |

**vector-ai gates** (`POST /v1/ambient`):

| Gate | Effect |
|------|--------|
| **Quiet mode** | Returns empty `text` with `quiet: true`; **no image / vision processing** |
| Quiet expiry | Lifts after **≥4h gap** since last ambient call (`AMBIENT_SLEEP_GAP`) or **24h** hard cap (`AMBIENT_QUIET_CAP`) |
| **Image too large** | Skip glance; `error: image_too_large` |
| LLM / eye error | Empty text; no invented presence |
| Model novelty | Default is “nothing”; only genuine novelty produces spoken `text` |

**Source of truth:** `wire-pod/chipper/pkg/wirepod/ttr/ambient.go` (and the VI patch `shared/patches/add-ambient-loop.py`); server side `shared/vector-ai/routes/ambient.py` + `process_state.py`.

## Does the dream mechanism run every 20 hours? Does vector-ai need to stay up for a straight 20 hours?

**No continuous 20-hour uptime is required.** The **20 hours is a debounce / cooldown**, not a wall-clock timer the process must sit through.

- Constant: `DREAM_DEBOUNCE_S = 20 * 3600` in `shared/vector-ai/soul/growth.py` — “at most about once per local night.”
- On each trigger, `run_dream_job` checks `last_dream_job_at`. If the last successful dream was **less than 20h ago** (and `force` is false), the job is **skipped**. If **≥20h** (or never dreamed), it runs immediately as a short LLM call, then updates soul and saves.
- `last_dream_job_at` is **persisted in `soul.json`**, so restarts keep the cooldown; you do not “lose progress” on a 20h countdown by bouncing the process.

**What actually triggers a dream (event-driven, not a 20h sleep):**

| Path | When |
|------|------|
| Ambient sleep gap | First ambient call after a long quiet gap (`AMBIENT_SLEEP_GAP`); schedules dream if debounce elapsed (`routes/ambient.py`, reason `ambient_sleep_gap`) |
| Chat tag | Assistant emits `{{soulDream}}` (stripped before speech; background schedule) |
| Ops HTTP | `POST /v1/soul/dream` (optional `force: true` bypasses debounce) |
| Goals-only | `POST /v1/soul/reflect_goals` (same job, cooler temperature) |

So vector-ai only needs to be **up when a trigger fires** (e.g. morning ambient after overnight quiet). It does **not** need to remain running for 20 straight hours waiting for a timer.

Source: `shared/vector-ai/soul/growth.py`, `routes/ambient.py`, `routes/soul.py`, `response_cleanup.py`, `soul/persist.py`.

## How do I manually trigger ambient for testing / debug?

**Yes — call vector-ai HTTP directly** (localhost `:8090` by default). Chipper’s 3‑minute loop is only one client of the same API.

| Endpoint | Use |
|----------|-----|
| `POST /v1/ambient` | **Full glance** — body `{"image": "<base64 JPEG>"}`. Runs VISION_MODEL, PRESENCE + SCENE, store/resend prior scene, optional spoken novelty. |
| `GET /v1/ambient/state` | Quiet mode + last ambient call / sleep-gap knobs |
| `POST /v1/ambient/quiet` | `{"on": true\|false}` — force quiet on/off for testing |
| `GET /v1/soul` | Ops view of presence/mood/last glance note (not a full SCENE dump) |
| `linux/dump-vector-ai-state.sh` | Bulk GET dump of read-only vector-ai endpoints |

Example (from a JPEG on disk):

```bash
# Ensure quiet is off
curl -s -X POST http://127.0.0.1:8090/v1/ambient/quiet \
  -H 'Content-Type: application/json' -d '{"on":false}'

# Trigger one ambient glance
B64=$(base64 -w0 /path/to/desk.jpg)   # macOS: base64 -i desk.jpg | tr -d '\n'
curl -s -X POST http://127.0.0.1:8090/v1/ambient \
  -H 'Content-Type: application/json' \
  -d "{\"image\":\"$B64\"}" | jq .
```

Response fields: `text` (spoken or `""`), `presence`, `name_hint`, `occupied`. Watch vector-ai logs for `[eye] scene stored`, `PREVIOUS SCENE` / baseline, and `[ambient] NOVELTY` / `nothing novel`.

**Notes:** Calling vector-ai **bypasses chipper gates** (night hours, voice cooldown, 15m post-react, calm power). First glance with no prior SCENE is baseline-only (no speech). There is **no separate debug-only ambient route** — production `POST /v1/ambient` is the test path. Prior scene lives in-memory + `soul.json` eye digests; `GET /v1/soul` does not currently print the full previous SCENE string.

## How does ambient know what changed between glances?

Ambient is still **one vision call** per glance, but the model must always emit a full **SCENE** inventory, which is **stored and resent** on the next glance so change detection has a real baseline.

**Protocol** (VISION_MODEL, `soul/eye.py`):

1. `PRESENCE: empty | person | person:<NameHint>`
2. `SCENE: …` — thorough LEFT→RIGHT inventory of everything visible *now*, with location tags (`item@zone`, e.g. `mug@left-of-monitor`) covering far-left through far-right (always required)
3. `NOTHING` **or** two-line novelty (memory note with what+where / from→to + spoken line)

**Prior scene:** `EyeService._last_scene_text` holds the last successful SCENE. The next user prompt includes it under **PREVIOUS SCENE** (full text, capped ~1400 chars). The model is told to walk that inventory by zone and treat appear / zone-move / vanish / clear state change **relative to that inventory** as news (prefer calling out an obvious side/zone shift over silent NOTHING).

**Baseline / sleep:** If there is no previous scene (first glance or after ambient sleep gap), the glance only establishes the baseline — spoken novelty is suppressed. `apply_sleep_clear` clears the stored scene so the next session re-baselines.

**Persistence:** SCENE is stored on each ambient `VisionDigest` and restored from `soul.json` eye digests so restarts can still compare when a prior ambient digest exists.

**Desk map:** After every successful SCENE (including NOTHING), durable `item → location` pairs are extracted into memory so the next glance’s “Known desk layout” block stays current—not only after spoken novelty.

Without the previous SCENE in the prompt, the model often “sees” objects and concludes NOTHING because it has no record that they were not already there — which is exactly why the prior entry is stored and resent in full.

Source: `shared/vector-ai/soul/eye.py` (`AMBIENT_SYSTEM`, `_build_ambient_user_text`, `scene_from_raw`); desk extract in `routes/ambient.py` + `desk_extract.py`.

## What is a “frame-hash gate” for ambient?

**Not implemented today** — it was a suggested optimization, not current behavior.

**Idea:** Before spending a vision-LLM call on an ambient JPEG, compute a cheap fingerprint of the frame (e.g. a perceptual hash or a coarse downscaled hash) and compare it to the **last glance’s** hash.

| Result | Gate action |
|--------|-------------|
| Hash almost identical | Skip vision LLM: reuse prior SCENE / return nothing novel (desk likely unchanged) |
| Hash clearly different | “Open the gate”: run full PRESENCE + SCENE + novelty path as usual |

**Why “wire”:** Plug that check into the ambient path (`POST /v1/ambient` / `EyeService._see_ambient`) as an early gate—same family of gates as quiet mode or image-too-large, but based on pixels rather than policy.

**Tradeoff:** Saves cost and latency when the room is static; risks missing subtle moves if the hash is too coarse (or firing too often if lighting noise flips the hash). Today every non-quiet ambient glance that has a valid image goes to `VISION_MODEL` regardless of how similar the frame looks.

**Related (also not a hash gate):** Chipper already gates *whether to capture* (voice cooldown, night hours, post-react cooldown). A frame-hash gate would gate *whether to pay for vision* after a frame already arrived.

## What software library does perceptual hashing?

For a Python ambient path (vector-ai), the usual pick is:

| Library | Notes |
|---------|--------|
| **[ImageHash](https://pypi.org/project/ImageHash/)** (`pip install ImageHash`) | De facto Python pHash helper on top of **Pillow** (+ numpy/scipy). Algorithms: `average_hash`, `phash`, `dhash`, `whash`. Compare with Hamming distance (`hash1 - hash2`). Best fit if you add a frame-hash gate here. |
| **Pillow only** | Enough for a DIY coarse hash (resize tiny → greyscale → bit pattern). No dedicated pHash API. |
| **OpenCV** (`opencv-python`) | Not a pHash package by itself; people implement aHash/dHash/pHash on `cv2` mats. Heavier dep. |
| **[pHash](https://www.phash.org/)** (C++, GPLv3) | Classic native library; bindings exist but awkward vs ImageHash for pure Python services. |
| **benhoyt/dhash** | Small Python **difference-hash** only — fine for “did the frame change?” |

**Minimal ImageHash sketch** (not in repo today):

```python
from PIL import Image
import imagehash
import io

h = imagehash.phash(Image.open(io.BytesIO(jpeg_bytes)))
# similar if (h - previous_h) <= threshold  # e.g. 5–10 for phash
```

For ambient, **`dhash` or `phash` + Hamming threshold** is the usual recipe; exact `hashlib.md5` of JPEG bytes is *not* perceptual (re-encode/noise always “changes”).
