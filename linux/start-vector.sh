#!/usr/bin/env bash
# Bring Vector's stack up. The vector-supervisor service owns everything —
# it launches Ollama, chipper and vector-ai, advertises mDNS, and
# auto-recovers from drops/sleep/IP changes. So "start" is just that.
set -e
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }

if ! systemctl list-unit-files | grep -q '^vector-supervisor\.service'; then
    warn "vector-supervisor.service not found — run install.sh first."
    exit 1
fi

sudo systemctl start vector-supervisor.service
ok "Supervisor starting — bringing up Ollama, Wire-Pod and vector-ai."

sleep 18
# vector-ai's port comes from pod.conf (AI_PORT, default 8090).
AI_PORT=8090
POD_CONF="$HOME/vector-pod/pod.conf"
if [ -f "$POD_CONF" ]; then
    PORT_FROM_CONF=$(sed -n 's/^[[:space:]]*AI_PORT[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$POD_CONF" | head -1 || true)
    [ -n "${PORT_FROM_CONF:-}" ] && AI_PORT="$PORT_FROM_CONF"
fi
if curl -s --max-time 5 "http://127.0.0.1:$AI_PORT/health" >/dev/null 2>&1; then
    ok "vector-ai up."
else
    warn "vector-ai not up yet (supervisor will keep retrying)."
fi
echo ""
echo "Say 'Hey Vector' to chat. Stop with stop-vector.sh when done."
