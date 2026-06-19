#!/usr/bin/env bash
# Start the whole AgentDeck stack WITHOUT Docker, in one command:
#   1. status_server.py        — one process, all backend API ports
#   2. one ttyd per agent       — each with the right --base-path /terminalN
#   3. a local reverse proxy    — Caddy on :8080 (single binary, no sudo)
#
# Then open http://localhost:8080.  Ctrl-C stops everything.
#
# Needs on your machine: python3, tmux, ttyd, the `claude` CLI, and `caddy`
# (https://caddyserver.com/download — one binary, no install needed).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]:-$0}")"

PORT_HTTP="${PORT_HTTP:-8080}"
pids=()
cleanup() { echo; echo "[agentdeck] stopping…"; kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# 1) backend (status / auth / tmux-buffer / page-version / paste — one process)
echo "[agentdeck] backend: status_server.py"
python3 status_server.py & pids+=($!)
sleep 1

# 2) one ttyd web terminal per agent, base-path = its /terminalN route
declare -A PORT=( [1]=3005 [2]=3006 [3]=3008 [4]=3009 [5]=3012 [6]=3013 [7]=3015 [8]=3016 )
for id in 1 2 3 4 5 6 7 8; do
  script="launch-claude.sh"; [ "$id" != "1" ] && script="launch-claude-$id.sh"
  [ -f "$script" ] || continue
  base="/terminal"; [ "$id" != "1" ] && base="/terminal$id"
  echo "[agentdeck] ttyd :${PORT[$id]} ($base) -> $script"
  ttyd -W -i lo -p "${PORT[$id]}" --base-path "$base" "bash ./$script" & pids+=($!)
done

# 3) reverse proxy (terminals are WebSockets, so a proxy is required)
if command -v caddy >/dev/null 2>&1; then
  echo "[agentdeck] caddy on :$PORT_HTTP  ->  open http://localhost:$PORT_HTTP"
  PORT_HTTP="$PORT_HTTP" caddy run --config Caddyfile --adapter caddyfile & pids+=($!)
else
  echo "[agentdeck] 'caddy' not found — backend + terminals are up, but you need a proxy."
  echo "            Get caddy (one binary, no sudo): https://caddyserver.com/download"
  echo "            …or point your own nginx at these ports (nginx/agents-subdomain.conf)."
fi
wait
