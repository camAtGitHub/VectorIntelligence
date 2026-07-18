# Work Day Mode companion guide

Optional **desk accountability** for Vector: he notices when your work day starts, checks in on a schedule, and asks after long absences — without replacing ambient glances, pet/pickup lines, or proactive greetings.

**Default: off.** Safe for holidays, guests, and other users.

**Requires:** full Vector Intelligence install with a **patched chipper** (behavior-tick loop). Companion mode (stock Wire-Pod) does not run presence ticks, so Work Day will not speak on its own there.

Related design (deeper “why”): `docs/superpowers/specs/2026-07-18-vector-aliveness-workday-design.md`

---

## What it does (not how)

Think of a small daily script Vector follows when the feature is enabled:

### A normal day at the desk

1. **Morning window** (default ~9:00–10:30, your timezone)  
   If he recognizes **you** (enrolled face) at the desk in that window, the day is marked as **working**.

2. **On-task check-ins**  
   About every **90 minutes** while you’re still around, he may ask a short line like “still on task?”  
   He stays quiet if you just talked to him, if quiet mode is on, or if he spoke recently.

3. **Away from the desk** (during work hours, default from ~9:30 until end of day)  
   If the desk looks empty for about **30 minutes**, he may ask once whether you should be working.  
   Coming back clears that stretch; he won’t nag every minute.

4. **End of day** (default 18:00)  
   Work Day speech for that calendar day stops.

### Days you don’t show up in the morning

- If the morning window ends with **no** recognized start, he treats the day as **no show** (e.g. off sick) and **does not** run on-task or away pokes.
- If you appear later, he may ask once: what happened this morning / are you working this afternoon?
  - **Yes** → accountability arms until end of day.
  - **No** (or you ignore it long enough) → he stays quiet for the rest of that day (no re-asking).

### You stay in control

- **Master off** for holiday / guests (`WORKDAY_ENABLED=0`).
- **Pause** mid-day (“break until 2”, via chat tags the model can emit).
- **Quiet mode** and normal conversation still suppress unprompted work nags.
- Ambient desk novelty, sensors, and greetings keep doing their own jobs; Work Day is an **extra** optional layer.

### Continuity (what makes it feel like “the same day”)

He remembers, for that local date: whether work started, late arm, absences, pauses — and can mention the shape of the day when you **chat**, not by talking constantly.

### What it does *not* do

- Does not read your browser, Slack, or calendar (no house brain).
- Does not require face recognition on every glance — only when arming the day (and similar key moments).
- Does not replace ambient / greeting / sensor behavior.
- Does not run useful presence ticks on **stock companion** chipper without a rebuild.

---

## Where to set configuration

All knobs are **environment variables** read by **vector-ai** at process start.

| Platform | Live config file (edit this, not only the repo template) |
|----------|----------------------------------------------------------|
| **Windows** | `%USERPROFILE%\vector-pod\vector-ai\.env` |
| **Linux** | `~/vector-ai/.env` (typical full install) |

Template / defaults shipped in the repo:

- `shared/vector-ai/env-default`  
  (copied into the runtime tree at install; **runtime** `.env` is what the running service uses)

After any change: **restart vector-ai** (e.g. stop then start Vector / companion so the supervisor reloads the brain).

Optional: system or shell `TZ` is used only if `WORKDAY_TZ` is unset.

There is no separate Work Day section in `pod.conf` for these timers; ports still come from install/`pod.conf` as usual (`VECTORAI_PORT` for chipper → vector-ai).

---

## All configurable variables

Values are strings in `.env`. Booleans: `1` / `true` / `yes` / `on` = enabled. Times are **24h `HH:MM`** in the Work Day timezone.

### Master switch

| Variable | Default | What it does |
|----------|---------|----------------|
| `WORKDAY_ENABLED` | `0` | Turns Work Day Mode on (`1`) or off (`0`). **Leave off** for holidays, guests, or anyone who does not want accountability. |

### Timezone and daily windows

| Variable | Default | What it does |
|----------|---------|----------------|
| `WORKDAY_TZ` | host `TZ`, else `UTC` | Timezone for all Work Day clocks (e.g. `Australia/Sydney`). Use this so morning/end match *your* desk, not a UTC server clock. |
| `WORKDAY_START_BEGIN` | `09:00` | Start of the morning “did work begin?” window. |
| `WORKDAY_START_END` | `10:30` | End of that window. After this with no start → “no show” (silent pokes until late arm). |
| `WORKDAY_AWAY_WINDOW_BEGIN` | `09:30` | Earliest time **away** scolds are allowed. |
| `WORKDAY_END` | `18:00` | End of Work Day speech for the calendar day. |

### Pacing (how often he bothers you)

| Variable | Default | What it does |
|----------|---------|----------------|
| `WORKDAY_POKE_INTERVAL_S` | `5400` | Seconds between **on-task** check-ins (~**90 minutes**). |
| `WORKDAY_AWAY_S` | `1800` | Seconds the desk must look empty before an **away** scold (~**30 minutes**). One scold per absence stretch. |
| `WORKDAY_LATE_CHECK_TIMEOUT_S` | `900` | If he asks the late-arrival question and you don’t answer, how long until he gives up for the day (~**15 minutes**). |
| `WORKDAY_REID_AFTER_AWAY_S` | `3600` | After a long absence (~**1 hour**), optionally re-check who is at the desk before treating them as the same work day. Set `0` to never re-identify. |

### Speech manners (shared proactive behavior)

These apply to Work Day (and the multi-behavior runtime generally):

| Variable | Default | What it does |
|----------|---------|----------------|
| `SPEECH_MIN_GAP_S` | `90` | Minimum seconds between any two proactive lines so he doesn’t stack nags. |
| `SPEECH_SUPPRESS_AFTER_VOICE_S` | `120` | After you and Vector talk, suppress unprompted Work Day speech for this many seconds. |

### Presence / multi-behavior runtime

| Variable | Default | What it does |
|----------|---------|----------------|
| `BEHAVIORS_ENABLED` | `workday` | Comma-separated list of behavior ids to load. Keep `workday` here for Work Day. Future FSMs add names to this list. |
| `FACE_CACHE_MAX_AGE_S` | `120` | How long a face recognition result is reused before needing a new look (~**2 minutes**). |
| `IMAGE_CACHE_MAX_AGE_S` | `45` | How long a shared camera frame may be reused by behaviors (~**45 seconds**). Reserved for multi-behavior sharing. |
| `WORKDAY_PRIORITY` | `80` | Speech priority vs other future behaviors (higher wins if two want to talk). Rarely needs changing. |
| `WORKDAY_IDENTITY_REJECT_COOLDOWN_S` | `600` | After a stranger / wrong person is seen when arming, wait this long (~**10 minutes**) before asking for face ID again (avoids probe spam). |

### Example `.env` block

```env
# --- Work Day Mode ---
WORKDAY_ENABLED=1
WORKDAY_TZ=Australia/Sydney

# Optional overrides (defaults shown as comments):
# WORKDAY_START_BEGIN=09:00
# WORKDAY_START_END=10:30
# WORKDAY_AWAY_WINDOW_BEGIN=09:30
# WORKDAY_END=18:00
# WORKDAY_POKE_INTERVAL_S=5400
# WORKDAY_AWAY_S=1800
# WORKDAY_LATE_CHECK_TIMEOUT_S=900
# WORKDAY_REID_AFTER_AWAY_S=3600
# WORKDAY_PRIORITY=80
# WORKDAY_IDENTITY_REJECT_COOLDOWN_S=600
# FACE_CACHE_MAX_AGE_S=120
# IMAGE_CACHE_MAX_AGE_S=45
# SPEECH_MIN_GAP_S=90
# SPEECH_SUPPRESS_AFTER_VOICE_S=120
# BEHAVIORS_ENABLED=workday
```

### Chat controls (not env vars)

When you talk to Vector, the model may emit short tags (stripped from speech) that control Work Day:

| Tag | Meaning |
|-----|---------|
| `{{workAfternoon\|\|yes}}` | Confirm afternoon work after a late-arrival question |
| `{{workAfternoon\|\|no}}` | Decline (only while that late question is open) |
| `{{workPause\|\|until=14:00}}` | Pause pokes until that local time |
| `{{workResume}}` | Resume after a pause |

You can also set quiet mode as today for ambient-style hush; Work Day respects quiet for unprompted lines.

---

## How to build / install it

Work Day has two parts:

1. **Brain** — vector-ai code (ships with this repo; no special compile).  
2. **Body loop** — chipper **patch** so the robot reports desk presence and can speak lines (only in a **full** Wire-Pod build from this project).

### Prerequisites

- Vector Intelligence repo (this tree).  
- Full install path that **builds patched chipper** (not companion-only stock Wire-Pod).  
- OpenRouter (or other LLM) key already working for normal chat.  
- Ideally: your face **enrolled** so morning/late arm can recognize you.

### Windows (full install)

From an **Admin** PowerShell in the repo (or as your install docs specify):

```powershell
# Full install applies all patches including add-behavior-tick.py, then builds chipper
.\windows\install.ps1
# optional ports: -WebPort 9080 -AiPort 8090
```

Then:

1. Ensure runtime env exists:  
   `%USERPROFILE%\vector-pod\vector-ai\.env`  
2. Add `WORKDAY_ENABLED=1` and `WORKDAY_TZ=...` (see above).  
3. Start the stack: `.\windows\start-vector.ps1` (or your usual supervisor start).  
4. Pair / use as normal.

**Already installed earlier without this feature?** Re-run full install (or re-apply patches + rebuild chipper) so `shared/patches/add-behavior-tick.py` is applied, then restart. Env-only enable without the patch will not produce presence-driven speech.

### Linux (full install)

```bash
# From repo; install applies patches including behavior-tick and builds chipper
./linux/install.sh
```

Then:

1. Edit `~/vector-ai/.env` (or your install’s vector-ai path).  
2. Set `WORKDAY_ENABLED=1` and `WORKDAY_TZ=...`.  
3. `./linux/start-vector.sh` (or systemd unit if you use it).  

### Confirm it’s live

With vector-ai running (default port **8090**):

```bash
curl -s http://127.0.0.1:8090/v1/behaviors/state
```

You should see Work Day enabled (and related state) when `WORKDAY_ENABLED=1`.  
`/health` should still be healthy for normal chat.

### Holiday / guests

```env
WORKDAY_ENABLED=0
```

Restart vector-ai. No rebuild required to turn off.

### Developer / unit tests (no robot)

From the repo:

```bash
cd shared/vector-ai
python3 test_behaviors.py
```

Expect `ALL PASS`. This validates the brain FSM and config without a robot.

---

## Quick troubleshooting

| Symptom | What to check |
|---------|----------------|
| Nothing ever work-related | `WORKDAY_ENABLED=1`? Restarted vector-ai? **Full** patched chipper (not companion-only)? |
| Always silent on “sick” days | Expected if no morning face arm and no late “yes”. |
| Wrong morning time | Set `WORKDAY_TZ` to your zone, not only wall-clock on the PC if TZ differs. |
| Too chatty | Raise `WORKDAY_POKE_INTERVAL_S` / `WORKDAY_AWAY_S`, or use quiet / pause, or disable. |
| Arms for someone else | Only enrolled primary-style recognition should arm; strangers should not start the day. |
| Companion install only | Need full install + rebuild for the tick loop. |

---

## Summary

| Question | Answer |
|----------|--------|
| What is it? | Optional desk work accountability + day continuity for Vector. |
| On by default? | **No.** |
| Where configured? | Runtime `vector-ai/.env` (Windows `vector-pod\vector-ai\.env`, Linux `~/vector-ai/.env`). |
| How to build? | Full Vector Intelligence install (patch + build chipper); enable env; restart. |
| Ambient broken? | No — Work Day is additive. |
