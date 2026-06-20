#!/usr/bin/env bash
# Start the AgentDeck stack inside the container: backend (status_server, all API ports)
# + a ttyd terminal per agent + Caddy (login gate + automatic HTTPS) in front.
# The dashboard password is set on first visit (no env needed) and stored in the volume.
set -uo pipefail
cd /app

# A UTF-8 locale so the tmux client renders Cyrillic / box-drawing instead of "?".
# (The Dockerfile sets this as ENV too; this also covers a non-rebuilt image. ttyd runs a
# non-login bash that sources no profile, so the locale must be a real env var on the tree.)
export LANG="${LANG:-C.UTF-8}" LC_ALL="${LC_ALL:-C.UTF-8}"

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

# ~/.claude.json (claude's home-level config) is NOT inside the ~/.claude volume on its own —
# keep it persisted: store it in the volume and symlink it back, so a restart doesn't lose the
# login/onboarding (otherwise claude reports "configuration file not found").
[ -f /root/.claude.json ] && [ ! -L /root/.claude.json ] && mv -f /root/.claude.json /root/.claude/.claude.json
ln -sfn /root/.claude/.claude.json /root/.claude.json

echo "[agentdeck] backend: status_server.py"
python3 status_server.py &
sleep 1

declare -A PORT=( [1]=3005 [2]=3006 [3]=3008 [4]=3009 [5]=3012 [6]=3013 [7]=3015 [8]=3016 )
for id in 1 2 3 4 5 6 7 8; do
  script="launch-claude.sh"; [ "$id" != "1" ] && script="launch-claude-$id.sh"
  [ -f "$script" ] || continue
  base="/terminal"; [ "$id" != "1" ] && base="/terminal$id"
  echo "[agentdeck] ttyd :${PORT[$id]} ($base) -> $script"
  ttyd -W -i lo -p "${PORT[$id]}" --base-path "$base" bash "./$script" &
done

CADDYFILE=/app/docker/Caddyfile
# AGENTDECK_SITE=https://... (a port or IP, no real domain) → self-signed HTTPS:
# generate a certificate on first run (kept in the volume) and serve with it.
case "${AGENTDECK_SITE:-}" in
  https://*)
    CD=/app/.sessions/tls; mkdir -p "$CD"
    if [ ! -s "$CD/cert.pem" ]; then
      openssl req -x509 -newkey rsa:2048 -keyout "$CD/key.pem" -out "$CD/cert.pem" \
        -days 3650 -nodes -subj "/CN=agentdeck" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null
      echo "[agentdeck] generated a self-signed TLS certificate (first run)"
    fi
    CD="$CD" python3 -c 'import os;cd=os.environ["CD"];p="/app/docker/Caddyfile";s=open(p).read().replace("{$AGENTDECK_SITE::8765} {","{$AGENTDECK_SITE::8765} {\n\ttls "+cd+"/cert.pem "+cd+"/key.pem");open("/tmp/Caddyfile","w").write(s)'
    CADDYFILE=/tmp/Caddyfile
    ;;
esac
echo "[agentdeck] caddy -> ${AGENTDECK_SITE:-:8765}  (domain=auto-HTTPS, https://:port=self-signed)"
exec caddy run --config "$CADDYFILE" --adapter caddyfile
