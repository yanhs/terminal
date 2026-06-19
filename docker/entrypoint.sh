#!/usr/bin/env bash
# Start the whole AgentDeck stack inside the container: the status/auth backend
# (one process, all API ports) + a ttyd web terminal per agent + nginx in front.
set -uo pipefail
cd /app

echo "[agentdeck] backend: status_server.py (status/auth/buffer/page-version/paste)"
python3 status_server.py &
sleep 1

declare -A PORT=( [1]=3005 [2]=3006 [3]=3008 [4]=3009 [5]=3012 [6]=3013 [7]=3015 [8]=3016 )
for id in 1 2 3 4 5 6 7 8; do
  script="launch-claude.sh"; [ "$id" != "1" ] && script="launch-claude-$id.sh"
  [ -f "$script" ] || continue
  base="/terminal"; [ "$id" != "1" ] && base="/terminal$id"
  echo "[agentdeck] ttyd :${PORT[$id]} ($base) -> $script"
  # --base-path lets ttyd serve at /terminalN (matches the nginx route, no path strip);
  # -i lo keeps it on loopback (nginx in this container reaches it).
  ttyd -W -i lo -p "${PORT[$id]}" --base-path "$base" "bash ./$script" &
done

echo "[agentdeck] nginx :80  ->  open http://localhost:8080"
exec nginx -g 'daemon off;'
