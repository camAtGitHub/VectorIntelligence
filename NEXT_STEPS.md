# Next steps - Vector Intelligence

This guide covers:

1. **Companion mode** (keep packaged Wire-Pod, add OpenRouter brain) - do this first  
2. **Full build** (compile patched chipper + optional Whisper) - when you want ambient/sensors/Whisper  

---

## Concepts you need

### Three different "Wire-Pod" locations (Windows package)

| Path | What it is |
|------|------------|
| **`C:\Program Files\wire-pod`** | Install: `chipper\chipper.exe`, DLLs, webroot |
| **`%APPDATA%\wire-pod`** | Live data: `apiConfig.json`, certs, jdocs, **VOSK models** |
| **`%USERPROFILE%\vector-pod`** | **Vector Intelligence runtime** (created by our scripts) |

This **git repo** (e.g. `C:\apps\VectorAI`) is **source only**. The scheduled task runs code under **`%USERPROFILE%\vector-pod`**, not from the repo tree.

### How the brain hooks in (no binary intercept)

Wire-Pod knowledge settings (in **AppData** `apiConfig.json`):

```json
"knowledge": {
  "enable": true,
  "provider": "custom",
  "endpoint": "http://127.0.0.1:8090/v1",
  "key": "placeholder"
}
```

```text
You speak -> chipper STT -> POST :8090/v1/chat/completions
         -> vector-ai (persona + memory) -> OpenRouter -> stream back -> TTS
```

### What companion vs full gives you

| Feature | Companion (stock chipper.exe) | Full patched build |
|---------|-------------------------------|--------------------|
| OpenRouter chat | Yes | Yes |
| persona.txt + SQLite memory | Yes | Yes |
| getImage / vision via LLM | Yes (if prompt/config allow) | Yes |
| Bigger VOSK model | Yes (AppData models) | Yes |
| Whisper STT | No (need chipper-whisper) | Yes |
| Ambient / sensor / face-probe loops | No | Yes |
| Work Day Mode (desk accountability) | No (needs behavior-tick patch) | Yes (default **off** in `.env`) |

---

## Part 1 - Companion mode (recommended first)

### Prerequisites

- Packaged Wire-Pod already working (UI e.g. **http://localhost:9080**)  
- Python **3.11** installed  
- [OpenRouter API key](https://openrouter.ai/keys)  
- This repo on disk  

### 1. Run companion setup (Admin PowerShell)

From the repo root:

```powershell
cd C:\apps\VectorAI   # or your clone path

.\windows\setup-companion.ps1 -WebPort 9080 `
  -WirePodDir "C:\Program Files\wire-pod" `
  -DataDir "$env:APPDATA\wire-pod"
```

Use your real UI port if not 9080.  
Elevated window: read the summary, then **press Enter** to close.

Creates:

- `%USERPROFILE%\vector-pod\vector-ai\` (service, venv, `.env`, persona)  
- `%USERPROFILE%\vector-pod\supervisor.py`  
- `%USERPROFILE%\vector-pod\pod.conf` (`EXTERNAL_CHIPPER=1`, paths, ports)  
- Scheduled task **`VectorPod-Supervisor`** (runs vector-ai only)  
- Merges knowledge endpoint into AppData `apiConfig.json` when possible  

### 2. Set the OpenRouter key (runtime .env)

**Not** only the repo template. Edit:

```text
%USERPROFILE%\vector-pod\vector-ai\.env
```

```env
OPENROUTER_API_KEY=sk-or-...
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=google/gemini-2.0-flash
LLM_SUMMARY_MODEL=google/gemini-2.0-flash-lite
LLM_MAX_HISTORY_MESSAGES=24
```

Optional personality: same folder, **`persona.txt`**.

If you only edited `repo\shared\vector-ai\.env`, **copy** it:

```powershell
Copy-Item C:\apps\VectorAI\shared\vector-ai\.env $env:USERPROFILE\vector-pod\vector-ai\.env -Force
```

### 3. Confirm knowledge points at vector-ai

Open `%APPDATA%\wire-pod\apiConfig.json` or Wire-Pod UI:

- Provider: **custom**  
- Endpoint: **`http://127.0.0.1:8090/v1`**  

Re-merge if needed:

```powershell
.\windows\apply-wirepod-config.ps1 `
  -WirePodDir "C:\Program Files\wire-pod" `
  -DataDir "$env:APPDATA\wire-pod"
```

**Restart packaged Wire-Pod** so it reloads config.

### 4. Daily start / stop

```text
1. Start packaged Wire-Pod (as you always do)
2. .\windows\start-companion.ps1
3. Check http://127.0.0.1:8090/health  -> api_key_set: true
4. Talk to Vector
```

Stop **brain only**:

```powershell
.\windows\stop-vector.ps1
```

(Does not stop Program Files chipper.)

### 5. Logs

```text
%USERPROFILE%\vector-pod\supervisor.log
%USERPROFILE%\vector-pod\vector-ai.log
%USERPROFILE%\vector-pod\vector-ai\vector-ai-debug.log   (only if debug on)
```

Timestamped; rotate ~10 MB.

**Debug payload logging** (what Wire-Pod sends / what goes to OpenRouter):

```env
# in %USERPROFILE%\vector-pod\vector-ai\.env
VECTORAI_DEBUG=1
# optional: VECTORAI_DEBUG_MAX_CHARS=4000
```

Restart companion after changing. Images are redacted (length only). Turn off when done (verbose + larger logs).

After updating code from the repo:

```powershell
Copy-Item C:\apps\VectorAI\shared\vector-ai\service.py $env:USERPROFILE\vector-pod\vector-ai\service.py -Force
Copy-Item C:\apps\VectorAI\shared\supervisor.py $env:USERPROFILE\vector-pod\supervisor.py -Force
.\windows\stop-vector.ps1
.\windows\start-companion.ps1
```

### 6. Optional: better VOSK (no recompile)

Stock `chipper.exe` is **VOSK**. Larger models live under AppData, e.g.:

```text
%APPDATA%\wire-pod\vosk\models\en-US\
```

Point Wire-Pod's en-US model at a bigger package (e.g. `vosk-model-en-us-0.22-lgraph`), restart chipper. No Vector Intelligence rebuild required.

---

## Part 2 - Compile patched Wire-Pod (full stack)

Do this when you want **Whisper**, **ambient**, **sensor reactions**, **face probe**, leak fixes, etc.

### What the build produces

Under `%USERPROFILE%\vector-pod\wire-pod\chipper\`:

| Output | Role |
|--------|------|
| **`chipper.exe`** | VOSK STT build (`go build` of `cmd/vosk`) |
| **`chipper-whisper.exe`** | Whisper STT build |
| Runtime DLLs | vosk / whisper / mingw / opus / ssl as needed |
| `whisper.cpp` models | e.g. `ggml-base.en.bin` |

Patches in `shared/patches/*.py` are applied to a **pinned** upstream commit, then Go compiles. There is no single "drop one .go file" deliverable.

### Can you swap into Program Files?

**Sometimes, carefully - not a one-file guarantee.**

- Package has only `chipper.exe` (VOSK) - swapping needs matching DLLs  
- Whisper needs `chipper-whisper.exe` + `libwhisper` + model paths  
- Prefer: run full VI tree **or** copy **exe + DLLs**, keep **AppData** config/certs  

Safer migration: copy pairing/config from AppData into the VI wire-pod tree, or keep AppData and only replace binaries after testing.

### Full install steps (Windows)

1. **Admin PowerShell**, repo root:

   ```powershell
   .\windows\install.ps1 -WebPort 9080
   ```

   Installs Go/Python/MSYS2 as needed, clones [kercre123/wire-pod](https://github.com/kercre123/wire-pod) at pinned commit, applies patches, builds both chippers, installs vector-ai, registers supervisor, firewall rules. ~15-25 minutes.

2. **OpenRouter key** in `%USERPROFILE%\vector-pod\vector-ai\.env` (same as companion).

3. **Start full stack:**

   ```powershell
   .\windows\start-vector.ps1
   ```

   Supervisor starts **its** chipper + vector-ai (not Program Files package). Stop packaged Wire-Pod first to avoid port 443 clashes.

4. **First-time Wire-Pod wizard** (if new tree):

   ```powershell
   .\windows\initial-setup.ps1
   .\windows\apply-wirepod-config.ps1
   ```

5. **Pair** Vector via UI (escape-pod BLE / stock firmware path). Do not use SSH-setup unless you know WireOS.

### Linux full install

```bash
cd linux && bash install.sh --web-port 8080
# edit ~/vector-ai/.env
bash start-vector.sh
bash apply-wirepod-config.sh
# pair in web UI
```

### After full build: pod.conf

Full install typically **does not** set `EXTERNAL_CHIPPER=1`. Supervisor owns chipper + mDNS.

If you previously used companion, either:

- Use full `start-vector` only, or  
- Clear `EXTERNAL_CHIPPER` from pod.conf when you want the built-in chipper managed again  

### STT notes after compile

| Binary | STT | Switch |
|--------|-----|--------|
| `chipper.exe` | VOSK | `STT_SERVICE=vosk` (env / install defaults) |
| `chipper-whisper.exe` | Whisper | `STT_SERVICE=whisper.cpp` (VI default) |

Whisper model size: `WHISPER_MODEL=base.en` (default) or `small.en` (better, slower).

### Work Day Mode (optional, full build)

Desk accountability: morning arm via named face, ~90m on-task pokes, ~30m away scolds, late-arrival arm. **Default off.** Does not replace ambient/greeting/sensor.

In runtime `vector-ai/.env`:

```env
WORKDAY_ENABLED=1
WORKDAY_TZ=Australia/Sydney   # your local TZ
# optional: WORKDAY_START_BEGIN=09:00 WORKDAY_START_END=10:30 WORKDAY_END=18:00
```

Restart vector-ai. Holiday / guests: set `WORKDAY_ENABLED=0` and restart. Spec: `docs/superpowers/specs/2026-07-18-vector-aliveness-workday-design.md`.

---

## Checklist - "make it work" minimum

### Companion

- [ ] `setup-companion.ps1` with correct WebPort + Program Files + AppData  
- [ ] Key in **`%USERPROFILE%\vector-pod\vector-ai\.env`**  
- [ ] Knowledge **custom** -> `http://127.0.0.1:8090/v1`  
- [ ] Restart Wire-Pod after config merge  
- [ ] Wire-Pod running + `start-companion.ps1`  
- [ ] `/health` shows `api_key_set: true`  
- [ ] "Hey Vector" works  

### Full build (later)

- [ ] Stop packaged chipper (free :443)  
- [ ] `install.ps1` completed without patch/build errors  
- [ ] Same `.env` / persona as companion (or re-enter key)  
- [ ] `start-vector.ps1`  
- [ ] Pair / apply-config if new wire-pod tree  
- [ ] Confirm ambient/sensor if you care about those features  

---

## Quick reference - scripts

| Script | Purpose |
|--------|---------|
| `windows/setup-companion.ps1` | Install brain only + task + link knowledge |
| `windows/start-companion.ps1` | Start vector-ai (EXTERNAL_CHIPPER) |
| `windows/stop-vector.ps1` | Stop supervisor (companion: brain only) |
| `windows/apply-wirepod-config.ps1` | Merge knowledge -> vector-ai |
| `windows/install.ps1` | Full clone, patch, compile, install |
| `windows/start-vector.ps1` | Full stack via supervisor |
| `windows/initial-setup.ps1` | Escape-pod / STT wizard helpers |

---

## Troubleshooting

| Problem | Action |
|---------|--------|
| Key empty | Runtime `.env` under `vector-pod\vector-ai`, not only repo |
| No LLM | custom + `:8090/v1` in **AppData** apiConfig; restart Wire-Pod |
| Port 443 in use | Two chippers fighting - only one Wire-Pod at a time |
| Build patches fail | Must use pinned Wire-Pod commit from `install.ps1` |
| WinError 10054 in vector-ai | Usually harmless HTTP reset (OpenRouter/client); not robot issue #8 |

---

## See also

- [README.md](README.md) - product overview and architecture  
- [openrouter.ai/docs](https://openrouter.ai/docs/quickstart) - API / models  
- [kercre123/wire-pod](https://github.com/kercre123/wire-pod) - upstream server  
