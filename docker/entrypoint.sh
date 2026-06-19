#!/usr/bin/env bash
# Start the AgentDeck stack inside the container: backend (status_server, all API
# ports) + a ttyd terminal per agent + Caddy (login + automatic HTTPS) in front.
set -uo pipefail
cd /app

if [ -z "${AGENTDECK_PASSWORD:-}" ]; then
  echo "[agentdeck] ERROR: AGENTDECK_PASSWORD is not set — refusing to start an open dashboard." >&2
  echo "            Set it in .env / docker-compose.yml (the dashboard exposes live terminals)." >&2
  exit 1
fi
export AGENTDECK_USER="${AGENTDECK_USER:-admin}"
export AGENTDECK_PASS_HASH="$(caddy hash-password --plaintext "$AGENTDECK_PASSWORD")"
echo "[agentdeck] login user: ${AGENTDECK_USER}"

echo "[agentdeck] backend: status_server.py"
python3 status_server.py &
sleep 1

declare -A PORT=( [1]=3005 [2]=3006 [3]=3008 [4]=3009 [5]=3012 [6]=3013 [7]=3015 [8]=3016 )
for id in 1 2 3 4 5 6 7 8; do
  script="launch-claude.sh"; [ "$id" != "1" ] && script="launch-claude-$id.sh"
  [ -f "$script" ] || continue
  base="/terminal"; [ "$id" != "1" ] && base="/terminal$id"
  echo "[agentdeck] ttyd :${PORT[$id]} ($base) -> $script"
  ttyd -W -i lo -p "${PORT[$id]}" --base-path "$base" "bash ./$script" &
done

echo "[agentdeck] caddy -> ${AGENTDECK_SITE:-:80}  (automatic HTTPS if it's a domain)"
exec caddy run --config /app/docker/Caddyfile --adapter caddyfile
