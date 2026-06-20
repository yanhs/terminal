#!/usr/bin/env bash
# Start the whole AgentDeck stack WITHOUT Docker, in one command:
#   1. status_server.py   — backend (all API ports) + the login gate
#   2. one ttyd per agent  — each with the right --base-path /terminalN
#   3. Caddy               — proxy + login, on :8765 (or a domain for HTTPS)
#
# The dashboard exposes live terminals, so it's behind a login. You SET the password
# the first time you open it in the browser (or pre-seed it: echo 'AGENTDECK_PASSWORD=...' >> .env).
# Optional: AGENTDECK_SITE=your-domain -> automatic HTTPS (default ":8765", http/local).
#
# Needs: python3, tmux, ttyd, the `claude` CLI, and `caddy`.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]:-$0}")"

[ -f .env ] && { set -a; . ./.env; set +a; }

if ! command -v caddy >/dev/null 2>&1; then
  echo "[agentdeck] need 'caddy' (one binary, no sudo): https://caddyserver.com/download" >&2
  exit 1
fi

mkdir -p .sessions
export AGENTDECK_PASSFILE="${AGENTDECK_PASSFILE:-$PWD/.sessions/.dashpass}"
# OPTIONAL: pre-seed the password from an env var (else set it on first visit)
if [ -n "${AGENTDECK_PASSWORD:-}" ] && [ ! -s "$AGENTDECK_PASSFILE" ]; then
  python3 - "$AGENTDECK_PASSFILE" "$AGENTDECK_PASSWORD" <<'PY'
import sys, os, hashlib
p, pw = sys.argv[1], sys.argv[2]
salt = os.urandom(16); h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
os.umask(0o077); open(p, "w").write(f"{salt.hex()}${h.hex()}")
PY
fi
[ -s "$AGENTDECK_PASSFILE" ] && echo "[agentdeck] dashboard password is set" \
  || echo "[agentdeck] no password yet — open the dashboard and set one on first visit"

pids=()
cleanup() { echo; echo "[agentdeck] stopping…"; kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "[agentdeck] backend: status_server.py"
python3 status_server.py & pids+=($!)
sleep 1

declare -A PORT=( [1]=3005 [2]=3006 [3]=3008 [4]=3009 [5]=3012 [6]=3013 [7]=3015 [8]=3016 )
for id in 1 2 3 4 5 6 7 8; do
  script="launch-claude.sh"; [ "$id" != "1" ] && script="launch-claude-$id.sh"
  [ -f "$script" ] || continue
  base="/terminal"; [ "$id" != "1" ] && base="/terminal$id"
  echo "[agentdeck] ttyd :${PORT[$id]} ($base) -> $script"
  ttyd -W -i lo -p "${PORT[$id]}" --base-path "$base" "bash ./$script" & pids+=($!)
done

echo "[agentdeck] caddy -> ${AGENTDECK_SITE:-:8765}"
caddy run --config Caddyfile --adapter caddyfile & pids+=($!)
wait
