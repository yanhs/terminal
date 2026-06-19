#!/usr/bin/env bash
# Start the whole AgentDeck stack WITHOUT Docker, in one command:
#   1. status_server.py     — one process, all backend API ports
#   2. one ttyd per agent    — each with the right --base-path /terminalN
#   3. Caddy                 — login (basic auth) + optional HTTPS, on :8765
#
# A LOGIN IS REQUIRED — the dashboard exposes live agent terminals. Set a password:
#   echo 'AGENTDECK_PASSWORD=your-secret' >> .env     (or: export AGENTDECK_PASSWORD=...)
# Optional:
#   AGENTDECK_USER   the login name (default: admin)
#   AGENTDECK_SITE   ":<port>" (default ":8765") or your domain -> automatic HTTPS
#
# Needs: python3, tmux, ttyd, the `claude` CLI, and `caddy`.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]:-$0}")"

[ -f .env ] && { set -a; . ./.env; set +a; }

if ! command -v caddy >/dev/null 2>&1; then
  echo "[agentdeck] need 'caddy' (one binary, no sudo): https://caddyserver.com/download" >&2
  exit 1
fi
if [ -z "${AGENTDECK_PASSWORD:-}" ]; then
  echo "[agentdeck] set a dashboard password first — it exposes live terminals:" >&2
  echo "            echo 'AGENTDECK_PASSWORD=your-secret' >> .env" >&2
  exit 1
fi
export AGENTDECK_USER="${AGENTDECK_USER:-admin}"
export AGENTDECK_PASS_HASH="$(caddy hash-password --plaintext "$AGENTDECK_PASSWORD")"

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

echo "[agentdeck] caddy (login: ${AGENTDECK_USER}) -> ${AGENTDECK_SITE:-:8765}"
caddy run --config Caddyfile --adapter caddyfile & pids+=($!)
wait
