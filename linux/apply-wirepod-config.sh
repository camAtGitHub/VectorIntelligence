#!/usr/bin/env bash
# apply-wirepod-config.sh — Apply AI settings to Wire-Pod after initial setup.
# Run this AFTER completing Wire-Pod's web UI setup (http://<pi-ip>:<web port, default 8080>).
# It merges our AI config into Wire-Pod's apiConfig.json without wiping
# the SSL/enrollment fields that the setup UI wrote.

set -euo pipefail

WIREPOD_DIR="$HOME/wire-pod"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "$SCRIPT_DIR/../shared" && pwd)"
CONFIG_SRC="$SHARED_DIR/config/wirepod-apiConfig.json"
CONFIG_DST="$WIREPOD_DIR/chipper/apiConfig.json"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }

if [ ! -f "$CONFIG_DST" ]; then
    warn "Wire-Pod config not found at $CONFIG_DST"
    warn "Has Wire-Pod initial setup been completed via the web UI?"
    exit 1
fi

# Merge our keys (knowledge, STT, weather) into the existing config,
# preserving server/cert fields that Wire-Pod wrote during initial setup.
python3 - <<PYEOF
import json, sys

with open("$CONFIG_SRC", encoding="utf-8") as f:
    our = json.load(f)
with open("$CONFIG_DST", encoding="utf-8") as f:
    live = json.load(f)

# Apply our sections; preserve anything Wire-Pod manages itself
for key in ("knowledge", "STT", "weather"):
    live[key] = our[key]

# Pin the LLM endpoint to vector-ai's actual port (pod.conf AI_PORT, default
# 8090) so a port chosen at install time carries through to chipper.
ai_port = "8090"
try:
    for line in open("$HOME/vector-pod/pod.conf", encoding="utf-8"):
        k, _, v = line.strip().partition("=")
        if k.strip() == "AI_PORT" and v.strip().isdigit():
            ai_port = v.strip()
except OSError:
    pass
live["knowledge"]["endpoint"] = "http://127.0.0.1:%s/v1" % ai_port

with open("$CONFIG_DST", "w", encoding="utf-8") as f:
    json.dump(live, f, indent=2, ensure_ascii=False)

print("Config merged OK (vector-ai endpoint :%s)." % ai_port)
PYEOF

# Restart the stack so chipper re-reads apiConfig.json. The supervisor owns
# chipper (and vector-ai / Ollama), so bouncing its service is how the new
# config takes effect — same fix as the Windows side's defunct-task bug (#3).
info "Restarting the supervisor..."
sudo systemctl restart vector-supervisor.service
sleep 2
sudo systemctl is-active vector-supervisor.service && info "Supervisor is running — give it ~15s to bring chipper back up." || warn "Supervisor failed to start — check: journalctl -u vector-supervisor -n 30"
