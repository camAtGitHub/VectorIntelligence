# Vector Intelligence

Turn an Anki / Digital Dream Labs **Vector** robot into a smarter companion:
personality, per-person memory, conversation recaps, and (with a full build)
ambient awareness and sensor reactions.

- **Robot + STT + pairing:** Wire-Pod on your PC  
- **Brain:** `vector-ai` on localhost (persona, memory, streaming)  
- **LLM:** [OpenRouter](https://openrouter.ai) by default (OpenAI-compatible HTTP)

Tested on **Windows 10/11** and Debian-family Linux.

**Practical how-to (companion today, compile chipper later):** see **[NEXT_STEPS.md](NEXT_STEPS.md)**.

---

## What Vector can do

### Multi-user memory

Enrol a face once; facts and chat recaps stay **per person**, plus shared
household notes. With the **full (patched) chipper**, a face probe runs while
you speak so he knows who is talking before the reply.

### Conversation + vision memory

Chats are distilled into short recaps. Camera use stores a **short text note**
of what he saw (images are not saved).

### Autonomous behaviour (full patched chipper only)

Ambient desk glances, pickup/pet reactions, proactive greetings, mood tint.
**Companion mode** (stock packaged Wire-Pod) gets LLM chat + memory, not these
loops.

### Personality

Dry, sardonic default - edit **`persona.txt`** (not `.env`).

### Voice

Wake word and STT run in Wire-Pod (VOSK or Whisper). The LLM path is:

```text
Wire-Pod  ->  http://127.0.0.1:8090/v1  (vector-ai)  ->  OpenRouter
```

That is a normal Wire-Pod **custom knowledge** endpoint, not a network MITM.

---

## Architecture

### Full install (this project builds chipper)

```text
Vector  <-->  supervisor
                chipper (patched Wire-Pod) :443
                vector-ai                   :8090
                     |
                     +--> OpenRouter (HTTPS)
                mDNS escapepod.local
```

### Companion mode (packaged Wire-Pod + this brain)

```text
Vector  <-->  your chipper.exe (Program Files)
                    |
                    | knowledge.provider = custom
                    | knowledge.endpoint = http://127.0.0.1:8090/v1
                    v
              vector-ai (vector-pod, OpenRouter)
```

Supervisor scheduled task **VectorPod-Supervisor** keeps **vector-ai** up.
It does **not** start/stop packaged Wire-Pod when `EXTERNAL_CHIPPER=1`.

### Windows path split (packaged Wire-Pod)

| Path | Role |
|------|------|
| `C:\Program Files\wire-pod\` | Install: `chipper.exe`, DLLs, web UI files |
| `%APPDATA%\wire-pod\` | Live data: `apiConfig.json`, certs, jdocs, **VOSK models** |
| `%USERPROFILE%\vector-pod\` | **This project's runtime** (vector-ai, supervisor, logs, `.env`) |
| This git repo (e.g. `C:\apps\VectorAI\`) | Source only - not what the scheduled task runs |

**API key must go in** `%USERPROFILE%\vector-pod\vector-ai\.env`  
(not only in the repo's `shared\vector-ai\.env`).

---

## Installation

### Requirements

- Same LAN as Vector (2.4 GHz WiFi); outbound HTTPS for OpenRouter  
- [OpenRouter API key](https://openrouter.ai/keys)  
- Windows: Python 3.11 for companion; full build also needs Go, MSYS2, Git  
- Linux: full stack via `linux/install.sh`

### Windows A - Companion (packaged Wire-Pod)

Keep your existing Wire-Pod; only add the brain.

```powershell
# Admin PowerShell, from this repo:
.\windows\setup-companion.ps1 -WebPort 9080
# if needed (typical packaged install):
.\windows\setup-companion.ps1 -WebPort 9080 `
  -WirePodDir "C:\Program Files\wire-pod" `
  -DataDir "$env:APPDATA\wire-pod"
```

Then:

1. Edit **`%USERPROFILE%\vector-pod\vector-ai\.env`** -> `OPENROUTER_API_KEY=...`  
2. Start packaged Wire-Pod as usual (UI often **http://localhost:9080**)  
3. `.\windows\start-companion.ps1`  
4. Restart Wire-Pod once if knowledge was just merged (reload `apiConfig.json`)  
5. Say "Hey Vector"

Stop brain only: `.\windows\stop-vector.ps1` (does not kill packaged chipper).

| Companion includes | Needs full patched build |
|--------------------|---------------------------|
| Chat via OpenRouter, persona, SQLite memory | Ambient, sensor LLM quips, concurrent face probe |
| Larger VOSK model (your AppData models folder) | Whisper STT (`chipper-whisper.exe`) |

### Windows B - Full install (build patched chipper)

```powershell
# Admin:
.\windows\install.ps1
# optional: -WebPort 9080 -AiPort 8090
```

Then set `.env` key, `start-vector.ps1`, `initial-setup.ps1`,
`apply-wirepod-config.ps1`, pair Vector. Details: **[NEXT_STEPS.md](NEXT_STEPS.md)**.

### Linux

```bash
cd linux && bash install.sh   # --web-port / --ai-port optional
# edit ~/vector-ai/.env -> OPENROUTER_API_KEY
bash start-vector.sh
bash apply-wirepod-config.sh
# pair via web UI
```

---

## Daily operation

| | Companion (Windows) | Full stack Windows | Linux |
|--|---------------------|--------------------|-------|
| **Start** | Wire-Pod as usual + `start-companion.ps1` | `start-vector.cmd` | `start-vector.sh` |
| **Stop brain / stack** | `stop-vector.ps1` (brain only) | `stop-vector.cmd` | `stop-vector.sh` |

Nothing auto-starts with Windows login unless you add that yourself.

### Config cheatsheet

| Want | File |
|------|------|
| OpenRouter key / models / history cap | `%USERPROFILE%\vector-pod\vector-ai\.env` (Windows) or `~/vector-ai/.env` |
| Personality | `persona.txt` next to that `.env` |
| Knowledge endpoint (custom -> :8090) | `%APPDATA%\wire-pod\apiConfig.json` (packaged) or chipper's apiConfig |
| Ports / companion flags | `%USERPROFILE%\vector-pod\pod.conf` |

**OpenRouter `.env` variables:**

| Variable | Purpose |
|----------|---------|
| `OPENROUTER_API_KEY` | Required |
| `LLM_BASE_URL` | Default `https://openrouter.ai/api/v1` |
| `LLM_MODEL` | Main multimodal model slug |
| `LLM_SUMMARY_MODEL` | Mood + conversation summary |
| `LLM_MAX_HISTORY_MESSAGES` | Turns sent upstream (default 24) |

**Work Day Mode** (optional accountability; **default off**). Needs full install with the behavior-tick chipper patch. Set in the same vector-ai `.env`, then restart vector-ai:

| Variable | Default | Purpose |
|----------|---------|---------|
| `WORKDAY_ENABLED` | `0` | Master switch (`1` to enable) |
| `WORKDAY_TZ` | host `TZ` / UTC | Local windows (e.g. `Australia/Sydney`) |
| `WORKDAY_START_BEGIN` / `WORKDAY_START_END` | `09:00` / `10:30` | Morning arm window (named face) |
| `WORKDAY_END` | `18:00` | Stop pokes for the day |
| `WORKDAY_POKE_INTERVAL_S` | `5400` | On-task poke interval (~90m) |
| `WORKDAY_AWAY_S` | `1800` | Away scold after empty desk (~30m) |
| `SPEECH_MIN_GAP_S` | `90` | Global proactive speech gap |
| `SPEECH_SUPPRESS_AFTER_VOICE_S` | `120` | Quiet after chat |

Holiday / other users: leave `WORKDAY_ENABLED=0`. Design: `docs/superpowers/specs/2026-07-18-vector-aliveness-workday-design.md`.

**pod.conf (created by setup):**

| Key | Meaning |
|-----|---------|
| `WEB_PORT` | Wire-Pod UI (e.g. `9080`) |
| `AI_PORT` | vector-ai (default `8090`) |
| `EXTERNAL_CHIPPER=1` | Companion: do not start/stop chipper |
| `WIREPOD_DIR` | Install root (`Program Files\wire-pod`) |
| `WIREPOD_DATA_DIR` | Live data (`%APPDATA%\wire-pod`) |

Voice/pairing ports **443 / 80 / 8084** stay fixed (Vector expects them).

### Logs

`%USERPROFILE%\vector-pod\` (Windows) or `~/vector-pod/`:

- `supervisor.log` - process manager (timestamped)  
- `vector-ai.log` - brain (timestamped); `/health` access lines suppressed  
- `chipper.log` - full install only  

Rotated at ~**10 MB** (`.old` kept).

Health checks use TCP/process (not noisy HTTP spam). Confirm brain:

```text
http://127.0.0.1:8090/health
```

`api_key_set` should be `true`.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `OPENROUTER_API_KEY is empty` | Key in **runtime** `%USERPROFILE%\vector-pod\vector-ai\.env`, then restart companion |
| Wire-Pod not calling the brain | Knowledge = **custom**, endpoint `http://127.0.0.1:8090/v1` in AppData `apiConfig.json`; restart Wire-Pod |
| Wrong `.env` edited | Repo `shared\vector-ai\.env` is a **template**; live copy is under `vector-pod\` |
| WiFi / exclamation on Vector | LAN / 2.4 GHz; `ping` robot; not OpenRouter |
| Proactor `WinError 10054` in vector-ai | Usually harmless Windows socket teardown (not issue #8 robot wedge) |
| Slow first reply | Cloud latency; try a faster `LLM_MODEL` |

---

## Repo layout

```text
windows/          setup-companion, install, start/stop, apply-config
linux/            install + start/stop + apply-config
shared/
  vector-ai/      service.py, memory, persona, .env template
  patches/        applied only by full install (chipper source)
  supervisor.py   process manager
  config/         Wire-Pod apiConfig / intents templates
NEXT_STEPS.md     companion checklist + how to compile chipper
```

---

*Built on [Wire-Pod](https://github.com/kercre123/wire-pod). Vector and the
robot's firmware are the property of Digital Dream Labs / Anki.*
