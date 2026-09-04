# Vector Intelligence FAQ

## Is ambient mode called/triggered from wire-pod?

**Yes.** Ambient is **driven by Wire-Pod chipper**, not by vector-ai on a timer.

1. At chipper startup, `StartAmbientLoop()` is launched (patched into `startserver.go` via `shared/patches/add-ambient-loop.py` → `go ttr.StartAmbientLoop()`).
2. Per enrolled robot, `runAmbientLoop` sleeps on the 3‑minute cadence, applies gates, then `ambientObserveOnce` captures a JPEG via robotsession and **HTTP POSTs** it to vector-ai at `POST {vectorAIBase}/v1/ambient` with `{"image": "<base64>"}`.
3. vector-ai only **serves** that endpoint (vision/LLM + quiet mode). It does not schedule ambient glances; chipper is the trigger.

Source: `wire-pod/chipper/pkg/wirepod/ttr/ambient.go` (`askVectorAIAmbient` → `/v1/ambient`).

## Is `getImage` callable by the robot, or only a scheduled behavior?

**Callable by the robot (LLM / chat path).** It is a first-class Wire-Pod chipper command (`{{getImage||now}}`), not a VectorIntelligence behavior-tick scheduler function.

How it runs:

1. **LLM command catalog** — chipper advertises `getImage` in `kgsim_cmds.go` (`ActionGetImage` → `DoGetImage`). The model is told to end a short acknowledgement with that tag when it needs a fresh camera frame.
2. **Prompt** — `shared/config/wirepod-apiConfig.json` (`openai_prompt`) tells the model when to emit `{{getImage||…}}` vs describe an already-attached photo.
3. **VI backstops** — `shared/vector-ai/chat_flow.py` + `vision.py`: if the user clearly asks to look and no photo is attached, vector-ai **forces** `{{getImage||now}}.` without waiting for the LLM; if the LLM emits `getImage` mid-stream, chat switches to the camera; if a photo is already attached, any further `getImage` tags are stripped.

What it is **not**: ambient / presence glances. Those are scheduled in **chipper** (`StartAmbientLoop` → JPEG via robotsession → `POST /v1/ambient`). That path does not go through the `{{getImage||…}}` command.

Sources: `wire-pod/.../ttr/kgsim_cmds.go` (`getImage` / `DoGetImage`); `VectorIntelligence/shared/vector-ai/vision.py`, `chat_flow.py`; ambient FAQ entry above.

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

## How do I test whether the robot responds to `{{playAnimationWI||thinking}}` from the LLM?

**Yes — at a few layers.** `thinking` is a **valid** animation alias (maps to `anim_explorer_scan_short_04` in chipper’s `animationMap`). The live path is:

1. vector-ai streams text that still contains `{{playAnimationWI||thinking}}` (and similar) — `clean_response` does **not** strip playAnimation tags; only memory/quiet/soul/forbidden-hallucinated cmds go away.
2. Chipper KG sim chunks the stream, logs each sentence, then `GetActionsFromString` → `PerformActions` → `DoPlayAnimationWI` (animation plays **while** TTS continues).
3. Prerequisite: Wire-Pod **`knowledge.commands_enable: true`** (default in VI’s `shared/config/wirepod-apiConfig.json`).

### What works for testing

| Layer | How | Robot needed? |
|-------|-----|----------------|
| **Parse only** | Call `GetActionsFromString` (Go, package `ttr`) on a canned string | No |
| **vector-ai keeps the tag** | Chat completions / session message; inspect streamed SSE or logs for `{{playAnimationWI\|\|…}}` still present | No |
| **Live robot (full path)** | Wake Vector; talk so the model emits an animation; watch chipper log + eyes | Yes |
| **Unit tests today** | `test_cap_chunk_animations_edge` only checks animation **capping**, not robot play | No |

**Parse-only snippet** (from `wire-pod/chipper`, with module set up):

```go
// Package: github.com/kercre123/wire-pod/chipper/pkg/wirepod/ttr
acts := GetActionsFromString("{{playAnimationWI||thinking}} Give me a second.")
// Expect: ActionPlayAnimationWI + param "thinking", then ActionSayText "Give me a second."
```

There is **no** dedicated HTTP inject that feeds a fake LLM chunk straight into `PerformActions`. The SDK control page `/api-sdk/say_text` only speaks text — it does **not** parse `{{…}}` commands (pasting the tag would get spoken literally).

### Live robot checklist

1. Confirm commands are on (`commands_enable` in apiConfig / Wire-Pod Knowledge settings).
2. Talk to Vector (or anything that hits the knowledge/LLM path, not plain Say Text).
3. In chipper logs, a successful chunk looks like:
   - raw line: `{{playAnimationWI||thinking}} …spoken words…`
   - then animation lookup runs; unknown names log: `Animation provided by LLM doesn't exist: …`
4. **WI vs non-WI:** `playAnimationWI` = anim in a goroutine while speech continues; `playAnimation` = wait for anim before more speech (prompt says use that mainly for “do a dance/move” asks).

### What this does *not* prove

- That the **LLM will choose** `thinking` on a given turn (prompt guidance + temp only). To force the string you’d need a mock LLM or a temporary chipper/vector-ai stub; the repo doesn’t ship that harness.
- That **SDK Say Text** or ambient free-form speech without the KG action pipeline will animate from tags — they won’t.

Source: `wire-pod/chipper/pkg/wirepod/ttr/kgsim_cmds.go` (`ValidLLMCommands`, `animationMap`, `GetActionsFromString`, `DoPlayAnimationWI`); `kgsim.go` (stream → parse → `PerformActions`); vector-ai `response_cleanup.py`, `chat_flow.cap_chunk_animations`.

## How do I log in to Work Day in the morning? Do I say “Workday confirmed”?

**Morning login is face-only. You do not say anything.**

Default morning window is **09:00–10:30** local Work Day timezone (`WORKDAY_START_BEGIN` / `WORKDAY_START_END`). If Vector sees an **enrolled named face** (not a stranger) at an occupied desk **inside that window**, the day arms silently to `working` (`arm_source: morning`). There is no spoken “Workday confirmed”, no yes/no, and no chat tag for morning start.

What must all be true in the window (`behaviors/workday.py` `_tick_waiting_morning`):

1. Desk **occupied**
2. **Identity fresh** (named face recently recognized)
3. Face is a **named non-stranger** (`face_id > 0`)

If you’re at the desk but not yet identified, Work Day sets `need_identity` and waits for a face probe. Occupancy without a named face does **not** start the day.

### After 10:30 (your dump: `mode: no_show`)

If the window ends with no named-face start, mode becomes **`no_show`**. Face ID after that is **not** morning login. It only qualifies you for the **late / afternoon** path:

1. Vector should ask: *“Didn't see you this morning. What happened — working this afternoon?”* (speech-gated: quiet mode, recent voice, and min speech gap can delay it). Mode becomes `late_check` only after that line is actually spoken.
2. Then **talk to him** with a short yes (or decline).

You can also skip waiting for the question: wake him and say a yes-phrase while still `no_show`. That arms `late_working` immediately (`arm_source: late_yes`).

**“Workday confirmed” is not a recognized phrase.** The deterministic user-text backstop (`classify_afternoon_reply`) does **not** match “confirmed” / “Workday confirmed”. Phrases that **do** arm:

| Yes (arm afternoon) | No (stay quiet the rest of the day) |
|---------------------|-------------------------------------|
| yes, yeah, yep, yup, affirmative | no, nope, nah |
| working this afternoon | not working, not today |
| I’m here to work / I am working / I’ll work | day off, off sick, off today |
| half-day, report in / reporting in | |

The model may also emit `{{workAfternoon||yes}}` / `{{workAfternoon||no}}` (stripped from speech). Pause/resume still need tags (`{{workPause||until=HH:MM}}`, `{{workResume}}`), not the yes/no regex.

Check: `GET /v1/behaviors/workday` — morning success is `mode: working`, `arm_source: morning`. Late success is `mode: late_working`, `arm_source: late_yes`. `no_show` with `primary_face_id: null` and `started_at: null` means the day is **not** logged in yet.

Source: `docs/FSM-workday-companion.md`; `shared/vector-ai/behaviors/workday.py` (`_tick_waiting_morning`, `_tick_no_show`, `classify_afternoon_reply`).

## How can I trigger desk occupied for Work Day?

**Occupied means vector-ai’s presence cache says a person is at the desk.** Morning arm also needs a **fresh named face** on the same tick. Restarting vector-ai wipes occupancy (it is in-memory).

### Production (robot)

Chipper only reports `occupied: true` if **any face** was seen in the last **2 minutes** (`behaviorOccupancySticky`). Sit in camera range and look at Vector.

| Source | What happens |
|--------|----------------|
| Behavior tick sparse probe | Every **2 minutes**, `ProbeFace` (~4s). Saw a face → `NoteAnyFaceSeen` → next `POST /v1/behaviors/tick` has `occupied: true`. Miss → `NoteDeskEmpty`. |
| Greeting / sensor face | Same `NoteAnyFaceSeen` sticky. |
| Ambient `PRESENCE: person` | Sets sticky occupancy in vector-ai (~30 min `PRESENCE_STICKY_S`) without a named face. |

Work Day then runs on the **tick** (chipper ~60s), not on `face_seen` alone. Occupied without identity → `need_identity`; chipper probes on the next cycle.

Check: `GET /v1/behaviors/state` → `presence.occupied` and `presence.identity_fresh`.

### Manual (ops / after a restart)

This both marks the desk occupied **and** runs the Work Day FSM (use your enrolled `face_id` / name):

```bash
curl -s -X POST http://127.0.0.1:8090/v1/behaviors/tick \
  -H 'Content-Type: application/json' \
  -d '{"occupied":true,"face":{"face_id":2,"name":"Cam","is_stranger":false}}'
```

`POST /v1/state/face_seen` with the same body **sets occupancy + identity** but does **not** tick Work Day (enrolled may return a weather `intent`; it cannot arm `WORKING`). Morning arm waits for the next chipper tick (~60s), or you POST `/v1/behaviors/tick` yourself.

Do not POST `occupied: true` with no face if you need morning arm on this tick — identity will not be fresh unless a recent `face_seen` is still in cache (soft reuse is identity-only and does not force occupied).

If today’s `GET /v1/behaviors/workday` is already `no_show`, occupying the desk will not reopen the morning window for that date (clock never goes `no_show` → `waiting_morning`). Use a late yes, or wait until the next local date.

Source: `wire-pod/chipper/.../behavior_tick.go`; `shared/vector-ai/routes/behaviors_http.py`; `routes/face.py`; `behaviors/presence.py`.

## Can I run two Vectors on one Vector Intelligence brain?

**Not as-is.** Wire-Pod already treats robots as per-ESN (pairing, `RobotSession`, ambient / sensor / behavior-tick loops). **vector-ai does not.** The brain is a single desk: one `Soul`, one `PresenceCache`, one `BehaviorRuntime` / speech arbiter, one `soul.json`, one `workday.db` row per date, and HTTP bodies with no robot id (`POST /v1/ambient` is just `{"image"}`, tick has occupancy/face but no ESN).

Chipper also has process-global flags that last-writer-wins across robots: charger, calm/sleep, “recently conversed”, occupancy sticky, `need_identity`, ambient speak cooldown.

- **Two independent companions:** medium — either two vector-ai processes routed by ESN, or tenant-key the brain; plus make those chipper flags per-ESN.
- **One shared personality with isolated bodies:** hard — thread ESN through every `/v1/*` call and all Python singletons; firmware `face_id` is per-robot so memory/workday identity must not assume IDs match.
- **Coordinated pair (they know about each other):** a new product, not a tenancy patch.

Docs already warn: `docs/FSM-implementation.md` assumption 8; `AGENTS.md` “Multi-robot caution”.

## How hard is two personas + two chat threads if joke/workday can stay shared?

**Small.** Voice chat history is already per robot in chipper (`RememberedChats` keyed by ESN in `kgsim.go`). Both robots still get the same `persona.txt` because vector-ai `prepare_messages` always prepends the one global `PERSONA`, and `/v1/chat/completions` has no robot id.

To split voice: pass ESN (header or request field) on chat completions, load `persona-<esn>.txt` (fallback `persona.txt`), and pick it in `prepare_messages`. Do not tenant soul/workday/jokes.

The one thread-mix leak: if robot A tells a joke, vector-ai may inject that line as an assistant turn into **whoever chats next** (`pending_behavior_speech_text`). Chipper also appends proactive lines per-ESN via `RememberAssistant`. Shared last-writer-wins on the FSM is fine; that inject is what would make B think *it* told A’s joke. Key or skip that splice if the threads should stay clean.

## How do I diff two folders ignoring missing files and folders?

`diff -r` always reports paths that exist on only one side (`Only in …`). There is no GNU `diff` flag that skips those. Filter them out, or compare only the intersection of the two trees.

**Quick filter** (ignore any file or folder that is missing on one side; still print content diffs):

```bash
diff -rq folder1 folder2 | grep -v '^Only in'
```

Drop `-q` if you want the full unified patch of files that exist in both:

```bash
diff -ru folder1 folder2 | grep -v '^Only in'
```

(`grep -v` on a unified diff can also drop a few context lines that happen to start with `Only in`; the `-q` form is safer for a “what changed” list.)

**Intersection-only** (never even looks at extras; missing directories and their contents are skipped):

```bash
comm -12 \
  <(cd folder1 && find . -type f | sort) \
  <(cd folder2 && find . -type f | sort) \
| while IFS= read -r f; do
    diff -q "folder1/$f" "folder2/$f"
  done
```

Use `diff -u` instead of `diff -q` if you want the actual line changes.

**rsync dry-run** (checksum files that already exist on the destination; skip source-only paths):

```bash
rsync -rcn --existing folder1/ folder2/
```

`--existing` skips files/dirs that are missing on `folder2`. It does not report extras that exist only on `folder2`. Run it once in each direction if you care about both sides’ overlapping files.

Do **not** use `diff -N` / `--new-file` for this: that treats missing files as empty, so you still see a full-file add/delete instead of ignoring them.

## How do I create a `WebUI` branch in wire-pod with only `chipper/webroot/` changes after the original UI commit?

Goal: a branch named **`WebUI`** whose tree is the original UI commit **plus later `chipper/webroot/` changes**, with no other chipper/Go/config commits.

Original UI commit: `11e7b22095166ed35765e88a8a10ed3a6ce49d5c` (`optimize some gifs`). Ignore the separate `wire-pod-webUI/` folder on disk.

**In this repo today** that is already one commit: `e3cc3db` (`webroot: unified UI…`) is a **direct child of `11e7b22`** and only touches `chipper/webroot/`. `main`’s webroot still matches it. So:

```bash
cd wire-pod
git branch WebUI e3cc3db
```

`WebUI` stays at that snapshot; `main` is unchanged.

**General recipe** (when later commits mixed webroot with other files, or you just want current `main`’s webroot on top of `11e7b22`):

```bash
cd wire-pod
git switch -c WebUI 11e7b22095166ed35765e88a8a10ed3a6ce49d5c
git checkout main -- chipper/webroot
git add chipper/webroot
git commit -m "webroot: take current UI from main"
git switch main
```

`git checkout main -- chipper/webroot` copies the **current** webroot tree from `main`. It does **not** delete files that existed at `11e7b22` but were removed on `main`; if that happens, remove leftovers first (`git rm` the extra paths, or `git diff --name-status 11e7b22 main -- chipper/webroot` and drop the `D` files).

**Cherry-pick only webroot commits** (keeps original messages when those commits are path-clean):

```bash
git switch -c WebUI 11e7b22095166ed35765e88a8a10ed3a6ce49d5c
git log --oneline --reverse 11e7b22..main -- chipper/webroot
git cherry-pick <those SHAs>   # only if each commit’s other files are empty / you split them
```

**Check the branch is webroot-only:**

```bash
git diff --stat 11e7b22 WebUI                 # only chipper/webroot
git diff --stat 11e7b22 WebUI -- . ':!chipper/webroot'   # must be empty
git diff --stat main WebUI -- chipper/webroot            # empty if webroot matches main
```

## How can I pull from the original repo (not my fork) as a fast-forward merge?

`origin` is **your** fork (`camAtGitHub/wire-pod`). The original is [kercre123/wire-pod](https://github.com/kercre123/wire-pod). Point a second remote at that, fetch it, then merge with `--ff-only` so Git **refuses** instead of making a merge commit.

**One-time:** add `upstream` (skip if it already exists):

```bash
cd wire-pod
git remote add upstream git@github.com:kercre123/wire-pod.git
# HTTPS: git remote add upstream https://github.com/kercre123/wire-pod.git
git remote -v
```

**Each time** (on the branch you want to update, usually `main`):

```bash
git fetch upstream
git merge --ff-only upstream/main
```

Same thing in one step:

```bash
git pull --ff-only upstream main
```

`--ff-only` only succeeds if your current branch is **already an ancestor** of `upstream/main` (you have no unique commits). If you have diverged — Vector Intelligence patches on `main`, or the `WebUI` branch with `e3cc3db` — Git errors with `Not possible to fast-forward, aborting` and leaves the tree alone.

Then `git push origin` updates **your** GitHub fork; that does not change kercre123.

**Do not** `git pull` with no args if you want the original: this repo’s `main` tracks `origin/main` (your fork). Optional: `git config pull.ff only` so any pull (including `origin`) also refuses non-FF merges.

To **see** original commits without merging: `git fetch upstream && git log --oneline HEAD..upstream/main`.

## How do I enable morning weather?

**Both gates**, then restart vector-ai (and rebuild/restart chipper if this is the first time the tick response includes `intent`):

1. `morning_weather` listed in `BEHAVIORS_ENABLED` (e.g. `workday,morning_weather`)
2. `MORNING_WEATHER_ENABLED=1` in **pod.conf**

Work Day must actually arm that morning (`WORKING`). Vector then fires the built-in `intent_weather_extend` once between the morning window and lunch (default 12:30) when an **enrolled named face** is recent (default ≤90s). Not an LLM weather line.

Guide: [FSM-morning-weather.md](FSM-morning-weather.md).

## Can morning weather wait for an enrolled `face_seen` and fire close to that event?

**Yes.** Morning weather requires an **enrolled named face** (`face_id > 0`, not a stranger, Work Day primary when one is set) whose `face_ts` is within `MORNING_WEATHER_FACE_MAX_AGE_S` (default **90s** — not the 30-minute `FACE_CACHE_MAX_AGE_S`). Occupied desk, Work Day `WORKING`, morning–lunch window, daily lock, quiet / recent-voice / min-gap still apply.

Idle enrolled sighting comes from sparse / `need_identity` **ProbeFace**, not a continuous face stream and not a sensor loop that pings `face_seen` while a face is in view. First `[face] observed: id=… 'Cam' (enrolled)` is bounded by that glance (**≤2 min**); weather anim then follows in **seconds of that log line**, not of walking into view. Voice-start `ObserveFaceBriefly` still notifies; the arbiter `recent_voice` deny keeps weather off “hey Vector.”

The 60s tick is a fallback if the same probe also attaches `face` (and still waits `MORNING_WEATHER_MIN_DWELL_S`). Restricted `face_seen` **skips** that dwell.

Sources: `shared/vector-ai/behaviors/morning_weather.py`; `routes/face.py`; `wire-pod/chipper/pkg/wirepod/ttr/sensor_reactions.go` (`notifyFaceSeen`); `behavior_tick.go` (`behaviorProbeAnyFace`); `face_probe.go` (`ObserveFaceBriefly`).

## What is a restricted behavior tick after enrolled `face_seen`?

**Shipped.** After an enrolled `POST /v1/state/face_seen`, vector-ai runs a **weather-only** tick (`tick(only_ids={morning_weather})`) and returns `{ok, is_stranger, intent}`. Chipper `notifyFaceSeen` honors `intent_weather_extend` with `FireWeatherIntent` (WebViz `:8888`). Ignores `speak`. Does **not** full-tick Work Day from `face_seen`.

| Path | Who calls it | What it does | What it must not do |
|------|----------------|--------------|---------------------|
| `POST /v1/state/face_seen` | Idle enrolled ProbeFace (**before** the 60s tick) + voice-start `ObserveFaceBriefly` | `note_face` + `note_person`; enrolled → weather-only tick (`skip_dwell`); `{ok, is_stranger, intent}` | Full `tick()` / Work Day arm / joke idle / `ingest_tick_payload` / `clock_tick` / return `speak` |
| `POST /v1/behaviors/tick` | Chipper every ~60s | Occupancy ingest, **all** plugins, weather **keeps** dwell, arbiter | — Work Day still lives here |

- Allowlist is **only** `morning_weather`. Occupancy policy and Work Day stay on the 60s tick.
- Weather still needs `WORKING`. Restricted tick **cannot** arm the day; `WAITING_MORNING` → `intent ""`.
- Stranger `face_seen` (`face_id <= 0` or empty name) does not tick weather.
- Arbiter deny does **not** consume the daily lock.
- GETs (`/v1/behaviors/morning_weather`, `/v1/behaviors/state`) stay ops-only.

Sources: `routes/face.py`; `behaviors/runtime.py` (`only_ids`); `wire-pod/.../ttr/sensor_reactions.go` (`notifyFaceSeen`); `behavior_tick.go` (`FireWeatherIntent`). Guide: [FSM-morning-weather.md](FSM-morning-weather.md) · [FSM-implementation.md](FSM-implementation.md) §3.2.


