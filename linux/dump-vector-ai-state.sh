#!/usr/bin/env bash
# dump-vector-ai-state.sh — GET every read-only vector-ai endpoint and dump bodies.
#
# Usage:
#   ./dump-vector-ai-state.sh
#   ./dump-vector-ai-state.sh --port 8090
#   ./dump-vector-ai-state.sh --base http://127.0.0.1:8090
#   ./dump-vector-ai-state.sh --session-id default --out /tmp/vector-ai-dump.json
#   AI_PORT=8090 ./dump-vector-ai-state.sh
#
# Resolves AI_PORT from (in order): --port / --base, $AI_PORT, ~/vector-pod/pod.conf, 8090.
# Requires: curl. Pretty-print prefers jq, then python3, else raw JSON.

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[+]${NC} $*" >&2; }
warn() { echo -e "${YELLOW}[!]${NC} $*" >&2; }
err()  { echo -e "${RED}[x]${NC} $*" >&2; }

BASE=""
PORT=""
SESSION_ID="default"
OUT_FILE=""
TIMEOUT=8
HOST="127.0.0.1"
INCLUDE_OPENAPI=0

usage() {
    cat <<'EOF'
Usage: dump-vector-ai-state.sh [options]

  --base URL          Full base URL (e.g. http://127.0.0.1:8090)
  --host HOST         Host only (default 127.0.0.1); used with --port
  --port N            AI_PORT (default: $AI_PORT, pod.conf, or 8090)
  --session-id ID     Query param for GET /v1/chat/session (default: default)
  --out FILE          Also write a combined JSON dump to FILE
  --timeout SEC       curl max-time per request (default: 8)
  --openapi           Also discover GETs from /openapi.json and include it
  -h, --help          Show this help

Dumps every vector-ai GET endpoint (static list + each registered behavior
detail from /v1/behaviors/state). Pass --openapi to also use OpenAPI
discovery and dump /openapi.json.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --base)        BASE="${2:-}"; shift 2 ;;
        --base=*)      BASE="${1#*=}"; shift ;;
        --host)        HOST="${2:-}"; shift 2 ;;
        --host=*)      HOST="${1#*=}"; shift ;;
        --port)        PORT="${2:-}"; shift 2 ;;
        --port=*)      PORT="${1#*=}"; shift ;;
        --session-id)  SESSION_ID="${2:-}"; shift 2 ;;
        --session-id=*) SESSION_ID="${1#*=}"; shift ;;
        --out)         OUT_FILE="${2:-}"; shift 2 ;;
        --out=*)       OUT_FILE="${1#*=}"; shift ;;
        --timeout)     TIMEOUT="${2:-}"; shift 2 ;;
        --timeout=*)   TIMEOUT="${1#*=}"; shift ;;
        --openapi)     INCLUDE_OPENAPI=1; shift ;;
        -h|--help)     usage; exit 0 ;;
        *) err "Unknown arg: $1"; usage; exit 2 ;;
    esac
done

# --- resolve base URL ---------------------------------------------------------
if [ -z "$BASE" ]; then
    if [ -z "$PORT" ]; then
        PORT="${AI_PORT:-}"
    fi
    if [ -z "$PORT" ]; then
        POD_CONF="${POD_CONF:-$HOME/vector-pod/pod.conf}"
        if [ -f "$POD_CONF" ]; then
            PORT=$(sed -n 's/^[[:space:]]*AI_PORT[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$POD_CONF" | head -1 || true)
        fi
    fi
    PORT="${PORT:-8090}"
    BASE="http://${HOST}:${PORT}"
fi
BASE="${BASE%/}"

if ! command -v curl >/dev/null 2>&1; then
    err "curl is required"
    exit 1
fi

pretty() {
    # stdin JSON -> pretty stdout
    if command -v jq >/dev/null 2>&1; then
        jq -C . 2>/dev/null || jq . 2>/dev/null || cat
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import sys,json; print(json.dumps(json.load(sys.stdin), indent=2, ensure_ascii=False, default=str))' 2>/dev/null || cat
    else
        cat
    fi
}

# Collect paths we will GET (path+query form for curl).
# Order is intentional: health first, behaviors/state before detail expansion.
declare -a PATHS=()

add_path() {
    local p="$1"
    local i
    for i in "${PATHS[@]+"${PATHS[@]}"}"; do
        [ "$i" = "$p" ] && return 0
    done
    PATHS+=("$p")
}

# Known GET routes (kept in sync with routes/* + test_service_modules EXPECTED_ROUTE_METHODS).
add_path "/health"
add_path "/v1/soul"
add_path "/v1/mood"
add_path "/v1/memory/list"
add_path "/v1/state/face"
add_path "/v1/ambient/state"
add_path "/v1/chat/session?session_id=${SESSION_ID}"
add_path "/v1/behaviors/state"

# OpenAPI discovery + dump — only when --openapi is passed.
if [ "$INCLUDE_OPENAPI" -eq 1 ]; then
    OPENAPI_TMP=""
    if OPENAPI_TMP=$(mktemp 2>/dev/null); then
        if curl -sS --max-time "$TIMEOUT" -o "$OPENAPI_TMP" -w "%{http_code}" \
            "${BASE}/openapi.json" 2>/dev/null | grep -q '^200$'; then
            if command -v python3 >/dev/null 2>&1; then
                while IFS= read -r p; do
                    [ -n "$p" ] || continue
                    # Skip path templates with unresolved braces (behaviors expanded later).
                    case "$p" in
                        *'{'*) continue ;;
                    esac
                    add_path "$p"
                done < <(python3 -c '
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
for path, methods in (doc.get("paths") or {}).items():
    if isinstance(methods, dict) and "get" in {k.lower() for k in methods}:
        print(path)
' "$OPENAPI_TMP")
                ok "Discovered GET paths from OpenAPI."
            fi
        fi
        rm -f "$OPENAPI_TMP"
    fi
    add_path "/openapi.json"
fi

TMPDIR_DUMP=$(mktemp -d)
trap 'rm -rf "$TMPDIR_DUMP"' EXIT

# path_safe for filenames
safe_name() {
    echo "$1" | sed 's#[/?&=]#_#g; s/#/_/g'
}

fetch_one() {
    local path_q="$1"
    local url="${BASE}${path_q}"
    local safe
    safe=$(safe_name "$path_q")
    local body="${TMPDIR_DUMP}/${safe}.body"
    local meta="${TMPDIR_DUMP}/${safe}.meta"
    local code
    code=$(curl -sS --max-time "$TIMEOUT" \
        -H 'Accept: application/json' \
        -o "$body" -w "%{http_code}" \
        "$url" 2>"${TMPDIR_DUMP}/${safe}.err" || echo "000")
    printf '%s\n' "$code" >"$meta"
    printf '%s\n' "$path_q|$code|$body"
}

echo -e "${CYAN}Vector AI state dump${NC}" >&2
echo "  base:       $BASE" >&2
echo "  session_id: $SESSION_ID" >&2
echo "  fetched:    $(date -Iseconds 2>/dev/null || date)" >&2
echo "" >&2

# First pass: fixed paths
declare -a RESULTS=()
for p in "${PATHS[@]}"; do
    RESULTS+=("$(fetch_one "$p")")
done

# Expand behavior detail GETs from /v1/behaviors/state
BEHAV_STATE_BODY=""
for r in "${RESULTS[@]}"; do
    path_q="${r%%|*}"
    rest="${r#*|}"
    code="${rest%%|*}"
    body="${rest#*|}"
    if [ "$path_q" = "/v1/behaviors/state" ] && [ "$code" = "200" ] && [ -f "$body" ]; then
        BEHAV_STATE_BODY="$body"
        break
    fi
done

declare -a BEHAV_IDS=()
if [ -n "$BEHAV_STATE_BODY" ] && command -v python3 >/dev/null 2>&1; then
    while IFS= read -r id; do
        [ -n "$id" ] || continue
        BEHAV_IDS+=("$id")
    done < <(python3 -c '
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(0)
behaviors = data.get("behaviors") or {}
if isinstance(behaviors, dict):
    for bid in behaviors:
        print(bid)
elif isinstance(behaviors, list):
    for b in behaviors:
        if isinstance(b, dict) and b.get("id"):
            print(b["id"])
' "$BEHAV_STATE_BODY")
fi

# Fallback known plugins if state empty / unreachable
if [ ${#BEHAV_IDS[@]} -eq 0 ]; then
    BEHAV_IDS=(workday joke_idle)
    warn "No behavior ids from /v1/behaviors/state; trying workday + joke_idle."
fi

for id in "${BEHAV_IDS[@]}"; do
    bp="/v1/behaviors/${id}"
    # skip if already listed
    already=0
    for p in "${PATHS[@]}"; do
        [ "$p" = "$bp" ] && already=1 && break
    done
    if [ "$already" -eq 0 ]; then
        PATHS+=("$bp")
        RESULTS+=("$(fetch_one "$bp")")
    fi
done

# --- human-readable sections to stdout ----------------------------------------
FAILS=0
for r in "${RESULTS[@]}"; do
    path_q="${r%%|*}"
    rest="${r#*|}"
    code="${rest%%|*}"
    body="${rest#*|}"
    if [ "$code" = "200" ]; then
        echo -e "${GREEN}=== GET ${path_q}  (HTTP ${code}) ===${NC}"
    elif [ "$code" = "404" ]; then
        echo -e "${YELLOW}=== GET ${path_q}  (HTTP ${code}) ===${NC}"
        FAILS=$((FAILS + 1))
    else
        echo -e "${RED}=== GET ${path_q}  (HTTP ${code}) ===${NC}"
        FAILS=$((FAILS + 1))
    fi
    if [ -f "$body" ] && [ -s "$body" ]; then
        if head -c 1 "$body" 2>/dev/null | grep -q '[{[]'; then
            pretty <"$body"
        else
            cat "$body"
            echo
        fi
    else
        err_file="${TMPDIR_DUMP}/$(safe_name "$path_q").err"
        if [ -f "$err_file" ] && [ -s "$err_file" ]; then
            cat "$err_file"
        else
            echo "(empty body)"
        fi
        echo
    fi
    echo
done

ok "Fetched ${#RESULTS[@]} GET endpoint(s). Non-OK responses: ${FAILS}."

# --- combined JSON file -------------------------------------------------------
if [ -n "$OUT_FILE" ]; then
    if ! command -v python3 >/dev/null 2>&1; then
        err "--out requires python3 to assemble combined JSON"
        exit 1
    fi
    # Build a manifest of path|code|bodyfile lines for python
    MANIFEST="${TMPDIR_DUMP}/manifest.txt"
    : >"$MANIFEST"
    for r in "${RESULTS[@]}"; do
        path_q="${r%%|*}"
        rest="${r#*|}"
        code="${rest%%|*}"
        body="${rest#*|}"
        printf '%s\t%s\t%s\n' "$path_q" "$code" "$body" >>"$MANIFEST"
    done
    python3 - "$BASE" "$SESSION_ID" "$MANIFEST" "$OUT_FILE" <<'PY'
import json, sys, datetime
from pathlib import Path

base, session_id, manifest, out_path = sys.argv[1:5]
endpoints = {}
for line in Path(manifest).read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    path, code, body_path = line.split("\t", 2)
    entry = {"http_status": int(code) if code.isdigit() else code}
    raw = Path(body_path).read_text(encoding="utf-8", errors="replace") if Path(body_path).is_file() else ""
    if raw.strip():
        try:
            entry["body"] = json.loads(raw)
        except Exception:
            entry["body_text"] = raw
    else:
        entry["body"] = None
    endpoints[path] = entry

doc = {
    "meta": {
        "base_url": base,
        "session_id": session_id,
        "fetched_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "endpoint_count": len(endpoints),
    },
    "endpoints": endpoints,
}
Path(out_path).write_text(
    json.dumps(doc, indent=2, ensure_ascii=False, default=str) + "\n",
    encoding="utf-8",
)
print(out_path)
PY
    ok "Wrote combined dump: $OUT_FILE"
fi

# Exit 0 if health worked; 1 if health failed; 2 if any other failures only.
HEALTH_CODE="000"
for r in "${RESULTS[@]}"; do
    path_q="${r%%|*}"
    rest="${r#*|}"
    code="${rest%%|*}"
    if [ "$path_q" = "/health" ]; then
        HEALTH_CODE="$code"
        break
    fi
done
if [ "$HEALTH_CODE" != "200" ]; then
    err "vector-ai health check failed (HTTP $HEALTH_CODE) at $BASE — is the service up?"
    exit 1
fi
if [ "$FAILS" -gt 0 ]; then
    warn "Some endpoints returned non-200 (often 404 for disabled behaviors)."
    exit 0
fi
exit 0
