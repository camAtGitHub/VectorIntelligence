# Vector Intelligence

Make an Anki / Digital Dream Labs **Vector** a smarter desk companion: personality,
per-person memory, vision notes, ambient awareness, and optional work-day
accountability — all driven by a local brain that talks to a **cloud LLM**.

| Piece | What it is |
|-------|------------|
| **Wire-Pod** | Robot pairing, wake word, STT, TTS (on your PC) |
| **vector-ai** | Local FastAPI “brain” — persona, memory, mood, behaviors |
| **LLM** | **[OpenRouter](https://openrouter.ai)** by default (OpenAI-compatible HTTPS) |

No local Ollama install. Wire-Pod still calls **vector-ai** on localhost; only
the upstream behind vector-ai is the cloud provider.

Tested on **Windows 10/11** and Debian-family Linux.

**Hands-on checklist** (companion first, full chipper later): **[NEXT_STEPS.md](NEXT_STEPS.md)**.

---

## Features

### Conversation + personality

- Streaming chat through Wire-Pod’s **custom knowledge** endpoint
- Personality in plain prose: edit **`persona.txt`** (not `.env`)
- Default tone: dry, sardonic, not sycophantic

### Multi-user memory

- Enrol a face once; facts and chat recaps stay **per person**, plus shared household notes
- With the **full (patched) chipper**, a face probe runs while you speak so he knows who is talking before the reply
- Conversation recaps distilled after chats; camera use stores a **short text note** of what he saw (images are not kept)

### Full-stack aliveness (patched chipper only)

These loops ship as install-time Go patches; stock packaged Wire-Pod does not run them.

| Behavior | What it does |
|----------|----------------|
| **Ambient** | Occasional desk glances + short commentary when something changes |
| **Sensors** | Pickup / pet / fall-style reactions with LLM quips |
| **Proactive greetings** | Notice you arriving without a wake word |
| **Face probe** | Concurrent identity while you talk |
| **Speech volume duck** | Desired level in jdoc; idle quieter; raise only while speaking/hold |
| **Behavior tick** | Thin presence loop for Work Day Mode and future FSMs |

**Companion mode** (packaged Wire-Pod + this brain) still gets OpenRouter chat,
persona, and SQLite memory — not the autonomous loops above.

### Work Day Mode (optional, default **off**)

Desk accountability on a multi-behavior runtime: morning arm (named face),
on-task pokes, away scolds, late-arrival check-in, chat continuity strip, and
voice tags for pause / resume / afternoon yes-no.

- Master switch: `WORKDAY_ENABLED=0` (safe for holidays and guests)
- Needs full install with the **behavior-tick** chipper patch
- User guide: [docs/FSM-workday-companion.md](docs/FSM-workday-companion.md)
- Extending with more FSMs: [docs/FSM-implementation.md](docs/FSM-implementation.md)

### Operations polish

- Supervisor keeps **vector-ai** (and full-stack chipper/mDNS) healthy; log rotation ~10 MB
- Timestamped **vector-ai** logs; health access spam suppressed
- Cheap TCP health checks (not a noisy HTTP GET every tick)
- Windows: `tzdata` shipped so Work Day timezones (`zoneinfo`) work without a system tzdb

---

## Architecture

```text
You speak  →  Wire-Pod STT  →  POST http://127.0.0.1:8090/v1/chat/completions
                                    │
                              vector-ai (persona, memory, behaviors)
                                    │
                              OpenRouter  (HTTPS)
                                    │
                              stream back  →  Wire-Pod TTS  →  Vector
```

That is a normal Wire-Pod **custom knowledge** endpoint, not a network MITM.

### Full install (this project builds patched chipper)

```text
Vector  <-->  supervisor
                ├── chipper (patched Wire-Pod)  :443 / web UI
                ├── vector-ai                   :8090
                │        └── OpenRouter
                └── mDNS escapepod.local
```

### Companion mode (keep packaged Wire-Pod)

```text
Vector  <-->  chipper.exe (Program Files)
                    │  knowledge.provider = custom
                    │  knowledge.endpoint = http://127.0.0.1:8090/v1
                    v
              vector-ai  →  OpenRouter
```

Scheduled task **VectorPod-Supervisor** keeps **vector-ai** up. With
`EXTERNAL_CHIPPER=1` it does **not** start or stop packaged Wire-Pod.

### Windows path split (packaged Wire-Pod)

| Path | Role |
|------|------|
| `C:\Program Files\wire-pod\` | Install: `chipper.exe`, DLLs, web UI |
| `%APPDATA%\wire-pod\` | Live data: `apiConfig.json`, certs, jdocs, **VOSK models** |
| `%USERPROFILE%\vector-pod\` | **This project’s runtime** (vector-ai, supervisor, logs, `.env`) |
| This git repo | Source only — not what the scheduled task runs |

**API key goes in** `%USERPROFILE%\vector-pod\vector-ai\.env`  
(not only the repo template under `shared/vector-ai/`).

---

## Installation

### Requirements

- Same LAN as Vector (2.4 GHz Wi‑Fi); outbound HTTPS for OpenRouter
- [OpenRouter API key](https://openrouter.ai/keys)
- **Windows companion:** Python 3.11 + existing packaged Wire-Pod
- **Windows full build:** Python, Go, MSYS2, Git (see install script)
- **Linux:** full stack via `linux/install.sh`

### Windows A — Companion (recommended first)

Keep your existing Wire-Pod; only add the brain.

```powershell
# Admin PowerShell, from this repo:
.\windows\setup-companion.ps1 -WebPort 9080
# if paths differ:
.\windows\setup-companion.ps1 -WebPort 9080 `
  -WirePodDir "C:\Program Files\wire-pod" `
  -DataDir "$env:APPDATA\wire-pod"
```

Then:

1. Edit **`%USERPROFILE%\vector-pod\vector-ai\.env`** → `OPENROUTER_API_KEY=...`
2. Start packaged Wire-Pod as usual (UI often **http://localhost:9080**)
3. `.\windows\start-companion.ps1`
4. Restart Wire-Pod once if knowledge was just merged (reload `apiConfig.json`)
5. Say “Hey Vector”

Stop brain only: `.\windows\stop-vector.ps1` (does not kill packaged chipper).

| Companion includes | Needs full patched build |
|--------------------|---------------------------|
| Chat via OpenRouter, persona, SQLite memory | Ambient, sensors, face probe, behavior tick |
| Larger VOSK model (AppData models) | Whisper STT (`chipper-whisper.exe`) |
| | Work Day Mode proactive speech |

### Windows B — Full install (build patched chipper)

```powershell
# Admin:
.\windows\install.ps1
# optional: -WebPort 9080 -AiPort 8090
```

Set the runtime `.env` key, then `start-vector.ps1`, `initial-setup.ps1`,
`apply-wirepod-config.ps1`, pair Vector. Details: **[NEXT_STEPS.md](NEXT_STEPS.md)**.

### Linux

```bash
cd linux && bash install.sh   # --web-port / --ai-port optional
# edit ~/vector-ai/.env  →  OPENROUTER_API_KEY
bash start-vector.sh
bash apply-wirepod-config.sh
# pair via web UI
```

---

## Daily operation

| | Companion (Windows) | Full stack Windows | Linux |
|--|---------------------|--------------------|-------|
| **Start** | Wire-Pod as usual + `start-companion.ps1` | `start-vector.cmd` | `start-vector.sh` |
| **Stop** | `stop-vector.ps1` (brain only) | `stop-vector.cmd` | `stop-vector.sh` |

Nothing auto-starts at login unless you add that yourself.

### Config cheatsheet

| Want | File |
|------|------|
| OpenRouter key / models / history | `%USERPROFILE%\vector-pod\vector-ai\.env` (Windows) or `~/vector-ai/.env` |
| Personality | `persona.txt` next to that `.env` |
| Knowledge endpoint (custom → :8090) | `%APPDATA%\wire-pod\apiConfig.json` or chipper’s apiConfig |
| Ports / companion flags / FSM knobs | `%USERPROFILE%\vector-pod\pod.conf` (Linux: `~/vector-pod/pod.conf`) |

**OpenRouter / LLM (`.env` only):**

| Variable | Purpose |
|----------|---------|
| `OPENROUTER_API_KEY` | Required |
| `LLM_BASE_URL` | Default `https://openrouter.ai/api/v1` |
| `LLM_MODEL` | Main multimodal model (default `google/gemini-2.0-flash`) |
| `LLM_SUMMARY_MODEL` | Mood + conversation summary |
| `LLM_MAX_HISTORY_MESSAGES` | Turns sent upstream (default 24) |
| `LLM_TIMEOUT_CONNECT` / `LLM_TIMEOUT_READ` | HTTP timeouts |

Any OpenAI-compatible base URL works if you point `LLM_BASE_URL` and the key
at another provider; OpenRouter is the documented default.

**Work Day Mode** (optional; default off). Full install + behavior-tick patch.
Configure in **`pod.conf`** (not `.env`). Restart vector-ai after changes:

| Variable | Default | Purpose |
|----------|---------|---------|
| `WORKDAY_ENABLED` | off | Master switch (`1` to enable; also need `workday` in `BEHAVIORS_ENABLED`) |
| `WORKDAY_TZ` | host `TZ` / UTC | Local windows (e.g. `Australia/Sydney`) |
| `WORKDAY_START_BEGIN` / `WORKDAY_START_END` | `09:00` / `10:30` | Morning arm window (named face) |
| `WORKDAY_END` | `18:00` | Stop work pokes for the day |
| `WORKDAY_POKE_INTERVAL_S` | `5400` | On-task poke interval (~90m) |
| `WORKDAY_AWAY_S` | `1800` | Away scold after empty desk (~30m) |
| `SPEECH_MIN_GAP_S` | `90` | Global proactive speech gap |
| `SPEECH_SUPPRESS_AFTER_VOICE_S` | `120` | Quiet after chat |

Example (`pod.conf`):

```conf
BEHAVIORS_ENABLED=workday
WORKDAY_ENABLED=1
WORKDAY_TZ=Australia/Sydney
```

Full commented template: `shared/config/pod.conf-default`. Guide:
[docs/FSM-workday-companion.md](docs/FSM-workday-companion.md).

Chat tags the model may emit (stripped before speech):  
`{{workAfternoon||yes|no}}`, `{{workPause||until=HH:MM}}`, `{{workResume}}`.

**pod.conf (created by setup; install upserts ports only — never wipes FSM keys):**

| Key | Meaning |
|-----|---------|
| `WEB_PORT` | Wire-Pod UI (e.g. `9080`) |
| `AI_PORT` | vector-ai (default `8090`) |
| `EXTERNAL_CHIPPER=1` | Companion: do not start/stop chipper |
| `WIREPOD_DIR` | Install root (`Program Files\wire-pod`) |
| `WIREPOD_DATA_DIR` | Live data (`%APPDATA%\wire-pod`) |
| `WORKDAY_*` / `JOKE_*` / `BEHAVIORS_ENABLED` / `SPEECH_*` | Behavior FSMs |

Voice/pairing ports **443 / 80 / 8084** stay fixed (Vector expects them).

### Logs

`%USERPROFILE%\vector-pod\` (Windows) or `~/vector-pod/`:

| File | Content |
|------|---------|
| `supervisor.log` | Process manager (timestamped) |
| `vector-ai.log` | Brain (timestamped); `/health` access lines suppressed |
| `chipper.log` | Full install only |

Rotated at ~**10 MB** (`.old` kept).

Confirm the brain:

```text
http://127.0.0.1:8090/health
```

`api_key_set` should be `true`. Useful debug routes: `/v1/mood`,
`/v1/behaviors/state`, `/v1/memory/list`.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `OPENROUTER_API_KEY is empty` / `api_key_set: false` | Key in **runtime** `%USERPROFILE%\vector-pod\vector-ai\.env`, restart companion / stack |
| Wire-Pod not calling the brain | Knowledge = **custom**, endpoint `http://127.0.0.1:8090/v1` in AppData `apiConfig.json`; restart Wire-Pod |
| Wrong `.env` edited | Repo `shared/vector-ai/` is a **template**; live copy is under `vector-pod\` |
| Wi‑Fi / exclamation on Vector | LAN / 2.4 GHz; `ping` robot — not OpenRouter |
| Work Day never speaks | Full patched chipper required; `WORKDAY_ENABLED=1`; restart vector-ai; see workday guide |
| `ZoneInfo` / timezone errors on Windows | Re-run install/setup so `tzdata` is in the venv; set `WORKDAY_TZ` to an IANA name |
| Ambient `DeadlineExceeded` on calm/sleep | Fixed in ambient patch (skip calm power mode; single capture budget) — rebuild/reinstall full stack |
| Proactor `WinError 10054` in vector-ai | Usually harmless Windows socket teardown |
| Slow first reply | Cloud latency; try a faster `LLM_MODEL` on OpenRouter |

---

## Repo layout

```text
windows/                 setup-companion, install, start/stop, apply-config
linux/                   install + start/stop + apply-config
shared/
  vector-ai/             FastAPI brain (service, memory, persona, env-default)
    behaviors/           Multi-behavior runtime + Work Day FSM
  patches/               Applied only by full install (chipper source)
  supervisor.py          Process manager / health / mDNS
  config/                Wire-Pod apiConfig + intents templates
docs/
  FSM-workday-companion.md   User-facing Work Day guide
  FSM-implementation.md      How to add more behavior FSMs
  superpowers/               Design + implementation plan notes
NEXT_STEPS.md            Companion checklist + how to compile chipper
```

Chipper patches of note (full install):

- Ambient loop, sensor reactions, face probe, proactive greeting hooks  
- **Behavior tick** (`add-behavior-tick.py`) — presence → `/v1/behaviors/tick`  
- **Speech volume** (`add-speech-volume-bump.py`) — desired in `wirepod.SpeechVolume` jdoc; quiet idle, loud speech/hold  

- Connection / stream leak fixes, wake-word mute during camera, etc.

---

## Docs map

| Doc | Audience |
|-----|----------|
| [NEXT_STEPS.md](NEXT_STEPS.md) | Install & day-to-day setup |
| [docs/FSM-workday-companion.md](docs/FSM-workday-companion.md) | Enable and live with Work Day Mode |
| [docs/FSM-implementation.md](docs/FSM-implementation.md) | Add another behavior without clobbering ambient |
| [docs/superpowers/specs/2026-07-18-vector-aliveness-workday-design.md](docs/superpowers/specs/2026-07-18-vector-aliveness-workday-design.md) | Product design (continuity, identity junctures) |

---

*Built on [Wire-Pod](https://github.com/kercre123/wire-pod). Vector and the
robot’s firmware are the property of Digital Dream Labs / Anki.*
