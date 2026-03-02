# Web Terminal

A web-based terminal that provides browser access to a Claude Code session via ttyd. Launches Claude CLI inside a tmux session, allowing persistent terminal access through a web browser.

## Tech Stack

- **Terminal:** ttyd (web terminal emulator)
- **Session:** tmux (persistent terminal sessions)
- **AI:** Claude CLI

## How It Works

The `launch-claude.sh` script manages a tmux session named `claude-terminal`:
- If the session already exists, it attaches to it
- If not, it creates a new session with Claude CLI running inside
- All `CLAUDE*` environment variables are unset to prevent session detection conflicts

## Running

The launch script is designed to be invoked by ttyd:

```bash
ttyd -p <port> /home/ubuntu/pr/terminal/launch-claude.sh
```

Access via browser at the configured port.

## Files

```
launch-claude.sh    # tmux session launcher for Claude CLI
```
