#!/usr/bin/env bash
# Start the AgentDeck stack inside the container: backend (status_server, all API ports)
# + a ttyd terminal per agent + Caddy (login gate + automatic HTTPS) in front.
# The dashboard password is set on first visit (no env needed) and stored in the volume.
set -uo pipefail
cd /app

# the login password lives here (persisted in the agentdeck-sessions volume)
export AGENTDECK_PASSFILE="${AGENTDECK_PASSFILE:-/app/.sessions/.dashpass}"

# OPTIONAL: pre-seed the password from an env var (otherwise set it on first visit)
if [ -n "${AGENTDECK_PASSWORD:-}" ] && [ ! -s "$AGENTDECK_PASSFILE" ]; then
  python3 - "$AGENTDECK_PASSFILE" "$AGENTDECK_PASSWORD" <<'PY'
import sys, os, hashlib
path, pw = sys.argv[1], sys.argv[2]
salt = os.urandom(16)
h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
os.umask(0o077); open(path, "w").write(f"{salt.hex()}${h.hex()}")
PY
  echo "[agentdeck] dashboard password seeded from AGENTDECK_PASSWORD"
fi
[ -s "$AGENTDECK_PASSFILE" ] && echo "[agentdeck] dashboard password is set" \
  || echo "[agentdeck] no password yet — open the dashboard and set one on first visit"

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

echo "[agentdeck] caddy -> ${AGENTDECK_SITE:-:8765}  (automatic HTTPS if it's a domain)"
exec caddy run --config /app/docker/Caddyfile --adapter caddyfile
