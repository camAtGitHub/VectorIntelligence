#!/usr/bin/env bash
# Shut Vector's stack down. Stopping the supervisor unit tears down its whole
# cgroup (chipper + vector-ai die with it via KillMode).
set -e
GREEN='\033[0;32m'; NC='\033[0m'
ok() { echo -e "${GREEN}[+]${NC} $*"; }

sudo systemctl stop vector-supervisor.service 2>/dev/null || true

ok "Stopped. Stack down."
echo "Start again with start-vector.sh."
