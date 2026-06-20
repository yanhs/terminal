# AgentDeck — everything in one container: status_server (Python stdlib) + a ttyd web
# terminal per agent + Caddy (login gate + automatic HTTPS). `docker compose up` builds
# this and serves the dashboard, password-protected, ready for an internet-facing VPS.
FROM node:22-slim

# system deps: python3 (status_server), tmux (terminals), curl/ca-certs (ttyd download),
# procps (status detection), uuid-runtime (session ids)
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip tmux curl ca-certificates procps uuid-runtime openssl \
    && rm -rf /var/lib/apt/lists/*

# ttyd — static prebuilt binary (turns each tmux terminal into a browser WebSocket)
RUN curl -fsSL -o /usr/local/bin/ttyd \
      https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64 \
    && chmod +x /usr/local/bin/ttyd

# the Claude Code CLI the agents run
RUN npm install -g @anthropic-ai/claude-code

# python-telegram-bot — lets the optional Telegram bridge run inside the container
RUN pip3 install --break-system-packages --no-cache-dir "python-telegram-bot==22.6"

# Caddy (login gate + automatic HTTPS) — grab the binary from the official image
COPY --from=caddy:2 /usr/bin/caddy /usr/local/bin/caddy

WORKDIR /app
COPY . /app

# IS_SANDBOX lets the agents run with --dangerously-skip-permissions inside the
# container (claude blocks that flag as root otherwise — the container IS the sandbox).
# LANG/LC_ALL give a UTF-8 locale so the tmux client renders Cyrillic + box-drawing
# instead of "?" (C.utf8 already ships in node:22-slim — no locale-gen needed).
ENV AGENTDECK_WORKDIR=/work IS_SANDBOX=1 LANG=C.UTF-8 LC_ALL=C.UTF-8
RUN mkdir -p /work /app/.sessions

EXPOSE 8765 80 443
ENTRYPOINT ["bash", "/app/docker/entrypoint.sh"]
