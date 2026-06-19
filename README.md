# 🛰️ AgentDeck

> Run a fleet of **Claude Code** agents in parallel — one persistent `tmux` session each,
> supervised from your **browser**, driven from **Telegram**.

<p>
  <img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-green.svg">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue.svg">
  <img alt="PRs welcome" src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg">
  <img alt="Shell" src="https://img.shields.io/badge/shell-bash-89e051.svg">
</p>

AgentDeck is a small, self-hosted control panel for running **many Claude Code agents at
once**. Each agent runs inside its own persistent `tmux` session and is exposed to the
browser as a live web terminal (via [`ttyd`](https://github.com/tsl0922/ttyd)). A status
dashboard shows what every agent is doing, a cookie-session gate sits in front of
everything, and a Telegram bridge lets you read and steer any agent from your phone.

It's the actual setup the author uses to keep 8–10 Claude Code agents working in parallel
on a single VPS. It is opinionated and assumes a specific layout — treat it as a working
reference you adapt, not a turn-key installer.

![AgentDeck — run a fleet of Claude Code agents in parallel](docs/demo.gif)

---

## ✨ Features

- **Parallel agents, persistent sessions.** Each agent lives in its own `tmux` session and
  keeps running even after you **close the terminal tab, close the browser, or disconnect** —
  nothing stops the agent. Re-opening (or a restart) resumes the exact same conversation via
  `claude --resume`, because each agent's session id is kept in `.sessions/`. Nothing is lost.
- **Browser terminals, no SSH.** Every agent is a full interactive terminal in the browser
  through `ttyd` — type, scroll, paste, run anything.
- **Live status dashboard.** Per-agent CPU activity, idle/working detection, and
  auto-detected project/task pulled from the live `tmux` pane.
- **One login for everything.** An `nginx` `auth_request` gate backed by a tiny Python
  status server: one cookie-session login protects the dashboard, the terminals, and the
  status APIs (no basic-auth re-prompt storms).
- **Telegram bridge** — drive agents from your phone:
  - plain text is typed straight into the selected agent's terminal;
  - **voice notes** are transcribed locally with [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) and sent in;
  - **files & images** are saved and handed to the agent as a link + local path;
  - the agent's progress and final answer stream back, and interactive prompts show up as
    inline buttons.
- **Paste images from the clipboard** straight into an agent (great for screenshots).
- **Order gate.** A fresh agent only auto-spawns when you've enabled it in the dashboard —
  no accidental runaway sessions.
- **Shared task board.** A lightweight, SSE-pushed board (`tasks-dashboard/`) for tracking
  multi-step work across all agents from one place.

## 📸 Screenshots

| Login gate | Dashboard | Telegram bridge |
|---|---|---|
| ![Login](docs/screenshots/login.png) | ![Dashboard](docs/screenshots/dashboard.png) | ![Telegram](docs/screenshots/telegram.png) |

The dashboard is responsive — here's an agent's terminal running on a phone:

<p align="center">
  <img src="docs/screenshots/mobile-terminal.gif" width="270" alt="An agent's terminal running live on a phone: the agent list, the streaming terminal, and a mobile input bar with arrow keys">
</p>

## 🧱 Architecture

```
                         ┌───────────────────────────── nginx ─────────────────────────────┐
   Browser ───TLS───▶    │  auth_request ─▶ status_server.py (/login, signed cookie)         │
                         │  /            ─▶ web/index.html  (dashboard UI)                    │
                         │  /api/*       ─▶ status_server.py (status, tmux-buffer, paste)     │
                         │  /terminalN   ─▶ ttyd :300N  ──▶  launch-claude-N.sh               │
                         └──────────────────────────────────┬──────────────────────────────┘
                                                            │  tmux session  "claude-terminal[-N]"
                                                            ▼
                                                   ┌──────────────────┐
   Telegram ──long-poll──▶  tg_bridge.py  ──send-keys──▶│  claude (CLI)    │
        ▲                        │                        └──────────────────┘
        └──── reply ◀── reads session transcript (.jsonl) ────────┘
```

- **`launch-claude*.sh`** — one launcher per agent. Unsets nested-`CLAUDE_*` vars, creates or
  resumes a `tmux` session, runs the Claude CLI inside it. Invoked by `ttyd`.
- **`status_server.py`** — the auth gate + status/buffer/paste APIs.
- **`web/index.html`** — the dashboard front-end.
- **`tg_bridge.py`** — the Telegram ⇄ tmux bridge (+ `whisper_transcribe.py` for voice).
- **`tasks-dashboard/`** — the shared task board (own README inside).

## 🐳 Quick start (Docker — one command)

The fastest way — you only need **Docker** and your **Claude login**:

```bash
git clone https://github.com/yanhs/agentdeck.git && cd agentdeck
docker compose up          # builds + starts everything → open http://localhost:8080
```

`docker-compose.yml` mounts your `~/.claude` so the agents can sign in (run `claude` once on
the host first to log in). Then add agents with the **+ Claude** button in the dashboard. To
let agents read/write your project, mount it at `/work` (uncomment the line in `docker-compose.yml`).

## ✅ Requirements (for the manual setup)

- Linux, **Python 3.11+**
- [`tmux`](https://github.com/tmux/tmux) and [`ttyd`](https://github.com/tsl0922/ttyd)
- The **Claude Code CLI** (`claude`) — https://docs.claude.com/claude-code
- `nginx` (for TLS + the auth gate + reverse proxy)
- *(optional, for Telegram voice notes)* `faster-whisper`, `ffmpeg`
- *(optional, for the Telegram bridge)* `python-telegram-bot`

## 🔧 Manual setup (without Docker)

> The launch scripts work out of the box — `claude` is found on your `PATH`, agents start in
> `$AGENTDECK_WORKDIR` (default: the directory above the repo), and each agent gets a stable
> session id under `.sessions/`. Only `nginx` needs your own domain + TLS.
>
> **No agent list to edit by hand** — agents are added, removed and renamed in the dashboard
> itself (the **+ Claude** button), which writes `agents.json` for you.

```bash
git clone https://github.com/yanhs/agentdeck.git
cd agentdeck

# 1) (optional) Telegram bridge — only if you want to drive agents from your phone
cp .env.example .env

# 2) start the backend — ONE process serves every dashboard API
#    (status, auth/login, tmux-buffer, page-version, paste-image)
python3 status_server.py &

# 3) one ttyd web terminal per agent — --base-path must match its /terminalN route
ttyd -W -i lo -p 3005 --base-path /terminal  ./launch-claude.sh    # agent #1
ttyd -W -i lo -p 3006 --base-path /terminal2 ./launch-claude-2.sh  # agent #2
# ...one per agent (full port + base-path map in nginx/agents-subdomain.conf)

# 4) put nginx in front — TLS + the cookie login gate + one origin.
#    Browser terminals are WebSockets, so a reverse proxy is required.
#    See nginx/agents-subdomain.conf for the full, working vhost
```

Create the login credentials the gate checks against:

```bash
sudo htpasswd -c /etc/nginx/.htpasswd_agents <your-username>
```

For production, run `status_server.py` and `tg_bridge.py` as systemd services — see
`claude-tg-bridge.service` for a template.

## ⚙️ Configuration

| What | Where | Notes |
|---|---|---|
| Enabled agents + labels | `agents.json` | copy from `agents.example.json`; gitignored |
| Telegram bridge | `.env` | copy from `.env.example` |
| Auth credentials | `/etc/nginx/.htpasswd_agents` | created with `htpasswd` |
| Reverse proxy / TLS | `nginx/agents-subdomain.conf` | swap the domain for your own |
| Pretty project names | `PROJECT_MAP` in `status_server.py` | optional, cosmetic |
| Working directory | `$AGENTDECK_WORKDIR` env | where agents start; defaults to the directory above the repo |

## 🤖 Create & connect a Telegram bot

The bridge (`tg_bridge.py`) runs as a Telegram bot that relays your messages into the
selected `tmux` terminal. It is **owner-only**, so you need both a bot token and your own
numeric Telegram user id.

<p align="center">
  <img src="docs/screenshots/telegram-live.gif" width="300" alt="The Telegram bridge streaming an agent's reply in real time, then showing an interactive prompt as inline buttons">
</p>

### 1. Create a bot and copy the token

Message [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts, and copy
the token it gives you. This becomes `TG_BRIDGE_TOKEN`.

### 2. Find your numeric Telegram user id

Message one of these bots — they reply with your numeric **user id** (not your `@username`):

- [@userinfobot](https://t.me/userinfobot) — replies with your `Id`
- [@RawDataBot](https://t.me/RawDataBot) — your id is the `message.from.id` field
- [@myidbot](https://t.me/myidbot) — send `/getid`

This number becomes `TG_BRIDGE_OWNER`.

### 3. Put both into `.env`

```dotenv
TG_BRIDGE_TOKEN=123456:ABC-DEF...your-token   # required — the bridge won't start without it
TG_BRIDGE_OWNER=123456789                     # your numeric user id (default 0 = nobody allowed)

# optional — all have working defaults:
# TG_FILES_DIR=/path/to/uploads               # where uploaded files / voice notes are saved
# TG_FILES_URL=https://example.com/files      # public base URL those files are served at
# TG_WHISPER_PY=/path/to/venv/bin/python      # a Python that has faster-whisper (voice only)
```

> `TG_BRIDGE_TOKEN` has no default — the bridge exits at startup if it's missing.
> `TG_BRIDGE_OWNER` defaults to `0`, which matches no real user, so the bot ignores
> everyone until you set it.

### 4. Install dependencies

```bash
pip install python-telegram-bot
```

That's the only third-party package the bridge itself needs (plus the `tmux` binary).
**Voice transcription is optional:** voice notes are shelled out to a separate Python
(`TG_WHISPER_PY`) that has [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper)
installed, with `ffmpeg` on the system. Text and files work without it.

### 5. Run it

```bash
# development
python3 tg_bridge.py

# production — systemd user service (template: claude-tg-bridge.service)
cp claude-tg-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-tg-bridge.service
loginctl enable-linger "$USER"     # keep it running without an active login
```

Then pick a terminal with `/use N` and send text, voice or files.

> ⚠️ The bridge is **owner-only**: every handler is filtered by `TG_BRIDGE_OWNER`, so
> messages or button taps from any other Telegram user are silently ignored.

## 🧪 Tests

```bash
python3 -m pytest -q        # 125 tests
```

The launcher gate, the order gate, the image-paste flow, and the entire Telegram bridge
(send/receive, menu parsing, transcription, file handling) are covered.

## 🗂️ Project layout

```
launch-claude*.sh        per-agent tmux launchers (invoked by ttyd)
status_server.py         auth gate + status/buffer/paste APIs
web/index.html           dashboard front-end
tg_bridge.py             Telegram ⇄ tmux bridge
whisper_transcribe.py    voice-note transcription helper
nginx/                   reverse-proxy + auth vhost
tasks-dashboard/         shared SSE task board
tests/                   pytest suite
AGENTS.md                example "operating rules" loaded into agents
```

## 🤝 Contributing

Issues and PRs are welcome. The codebase is plain Python + Bash with no build step —
clone, run `pytest`, and go. If you adapt it for a different layout or add a feature,
a PR documenting the change is appreciated.

## 📄 License

[MIT](LICENSE) © yanhs
