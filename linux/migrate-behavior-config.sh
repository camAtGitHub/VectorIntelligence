#!/usr/bin/env bash
# Opt-in: move WORKDAY_*/JOKE_*/SPEECH_*/BEHAVIORS_* keys from vector-ai/.env
# into pod.conf (only if not already set in pod.conf). Never copies OpenRouter/LLM keys.
#
# Usage:
#   ./linux/migrate-behavior-config.sh
#   ./linux/migrate-behavior-config.sh /path/to/.env /path/to/pod.conf
#
# Creates timestamped backups: pod.conf.bak-migrate-*, .env.bak-migrate-*
# Does NOT run automatically on install.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "$SCRIPT_DIR/../shared" && pwd)"

ENV_PATH="${1:-$HOME/vector-ai/.env}"
# Full Linux install keeps supervisor under ~/vector-pod; companion may use that too.
POD_PATH="${2:-$HOME/vector-pod/pod.conf}"

if [ ! -f "$ENV_PATH" ]; then
    echo "[!] No .env at $ENV_PATH — nothing to migrate."
    exit 0
fi

if [ ! -f "$SHARED_DIR/pod_conf_io.py" ]; then
    echo "[X] Missing $SHARED_DIR/pod_conf_io.py" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "[X] python3 not found on PATH. Install python3 and re-run." >&2
    exit 1
fi

mkdir -p "$(dirname "$POD_PATH")"
if [ ! -f "$POD_PATH" ]; then
    : > "$POD_PATH"
fi

echo "[+] Migrating behavior knobs: $ENV_PATH → $POD_PATH"
out="$(python3 "$SHARED_DIR/pod_conf_io.py" migrate-env "$ENV_PATH" "$POD_PATH")"
echo "$out"
if echo "$out" | grep -q '^migrated:'; then
    echo "[+] Done. Restart vector-ai so supervisor reloads pod.conf."
else
    echo "[+] Done (nothing new to migrate)."
fi
