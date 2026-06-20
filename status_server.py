#!/usr/bin/env python3
"""Agent status server.

GET  /  — live status + auto-detected project/task
POST /  — update manual overrides  { "id": "1", "project": "...", "task": "..." }
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import subprocess
import time
import os
import re
import hmac
import hashlib
from urllib.parse import urlparse, parse_qs

SESSIONS = [
    {"id": "1",  "session": "claude-terminal",    "path": "terminal"},
    {"id": "2",  "session": "claude-terminal-2",   "path": "terminal2"},
    {"id": "3",  "session": "claude-terminal-3",   "path": "terminal3"},
    {"id": "4",  "session": "claude-terminal-4",   "path": "terminal4"},
    {"id": "5",  "session": "claude-terminal-5",   "path": "terminal5"},
    {"id": "6",  "session": "claude-terminal-6",   "path": "terminal6"},
    {"id": "7",  "session": "claude-terminal-7",   "path": "terminal7"},
    {"id": "8",  "session": "claude-terminal-8",   "path": "terminal8"},
    {"id": "9",  "session": "orchestra-terminal-9",  "path": "terminal9"},
    {"id": "10", "session": "orchestra-terminal-10", "path": "terminal10"},
]

AGENTS_FILE = os.path.join(os.path.dirname(__file__), "agents.json")
SAMPLE_INTERVAL = 0.15
_prev_cpu = None
CPU_TICK_THRESHOLD = 2

# Optional: pretty display names for auto-detected project folders. Any folder
# not listed here simply shows its directory name, so this map is just for looks.
# Map your own "<folder>": "<Display Name>" entries here.
PROJECT_MAP = {
    "my-app": "My App",
    "docs-site": "Docs Site",
    "terminal": "Terminal",
    "orchestra": "Agent Orchestra",
}

ANSI_RE = re.compile(r'\x1b[\[\(][0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b.|\x0f|\x0e')
SEP_PROJECT_RE = re.compile(r'[─━═]{3,}\s+(.+?)\s+[─━═]{3,}')
PATH_PROJECT_RE = re.compile(r'(?:/home/ubuntu/pr|~/pr|/var/www)/([a-zA-Z0-9_-]+)')
SKIP_DIRS = frozenset({
    "tgimg", "tgfiles", "static", "node_modules",
    ".git", ".cache", ".claude", "pr",
})


def get_system_stats():
    global _prev_cpu
    # CPU
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        vals = [int(v) for v in parts]
        idle = vals[3] + vals[4]  # idle + iowait
        total = sum(vals)
        cpu_pct = 0
        if _prev_cpu:
            d_total = total - _prev_cpu[0]
            d_idle = idle - _prev_cpu[1]
            cpu_pct = round((1 - d_idle / d_total) * 100) if d_total else 0
        _prev_cpu = (total, idle)
    except Exception:
        cpu_pct = 0
    # RAM
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                mem[k.strip()] = int(v.split()[0])
        total_mb = mem["MemTotal"] // 1024
        avail_mb = mem.get("MemAvailable", mem["MemFree"]) // 1024
        used_mb = total_mb - avail_mb
        ram_pct = round(used_mb / total_mb * 100) if total_mb else 0
    except Exception:
        total_mb = used_mb = ram_pct = 0
    return {"cpu_pct": cpu_pct, "ram_used_mb": used_mb, "ram_total_mb": total_mb, "ram_pct": ram_pct}


def strip_ansi(s):
    return ANSI_RE.sub("", s)


def is_junk(line):
    """Return True if line is UI chrome, not meaningful content."""
    if not line:
        return True
    # Pure box-drawing / whitespace
    if re.match(r'^[\s─━═╭╮╰╯│┃┌┐└┘├┤┬┴┼╱╲░▒▓▘▝▖▗▀▄█▌▐]+$', line):
        return True
    # Claude Code prompt / status bar
    junk_markers = [
        "bypass permissions", "shift+tab", "esc to interrupt",
        "ctrl+t", "ctrl+c", "ctrl+r", "\u276f",
    ]
    low = line.lower()
    for m in junk_markers:
        if m in low:
            return True
    # Just a prompt char
    if line in (">", "$", "%", "\u276f", "❯"):
        return True
    # Rating prompt
    if re.match(r'^\d+:\s*\w+\s+\d+:', line):
        return True
    # Path-only line like ~/pr
    if re.match(r'^~[/\w]*$', line.strip()):
        return True
    return False


def load_agents():
    try:
        with open(AGENTS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_agents(data):
    with open(AGENTS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def reset_agent(agent_id, session_name):
    """Unload an agent from RAM by killing its tmux session.

    Both the Claude conversation JSONL and the agent's overrides entry
    (project / task / locked) are intentionally left untouched, so that
    when the user re-adds the agent via "+ Claude":
      - launch-claude-*.sh resumes the same Claude session id
      - the agent card immediately shows the same name (instead of blank)

    Idempotent: missing tmux session is fine.
    """
    out = {"tmux_killed": False}

    r = subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        capture_output=True, text=True,
    )
    out["tmux_killed"] = r.returncode == 0

    return out


def get_pane_pid(session):
    r = subprocess.run(
        ["tmux", "list-panes", "-t", session, "-F", "#{pane_pid}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return int(r.stdout.strip().split("\n")[0])


def get_child_pids(pid):
    r = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
    return [int(p) for p in r.stdout.strip().split("\n") if p.strip()]


def read_cpu_ticks(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().split(")")
            fields = parts[-1].strip().split()
            return int(fields[11]) + int(fields[12])
    except (FileNotFoundError, PermissionError, IndexError, ValueError):
        return 0


def get_cwd(session):
    r = subprocess.run(
        ["tmux", "display-message", "-t", session, "-p", "#{pane_current_path}"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def detect_project_from_cwd(cwd):
    if not cwd:
        return ""
    parts = cwd.rstrip("/").split("/")
    for p in reversed(parts):
        if p.lower() in PROJECT_MAP:
            return PROJECT_MAP[p.lower()]
    for p in reversed(parts):
        if p and p not in ("pr", "home", "ubuntu", ""):
            return p
    return ""


def detect_project_from_pane(text):
    """Find the project by weighting recent mentions more heavily."""
    matches = [m for m in PATH_PROJECT_RE.findall(text) if m not in SKIP_DIRS]
    if not matches:
        return ""
    # Weight recent mentions: last 1/4 of text counts 4x
    total = len(text)
    cutoff = total * 3 // 4
    recent_text = text[cutoff:]
    recent_matches = [m for m in PATH_PROJECT_RE.findall(recent_text) if m not in SKIP_DIRS]
    from collections import Counter
    counts = Counter(matches)
    # Boost recent mentions
    for m in recent_matches:
        counts[m] += 3
    top = counts.most_common(1)[0][0]
    return PROJECT_MAP.get(top.lower(), top)


def parse_pane(session):
    """Capture pane content, extract project name and last meaningful activity."""
    # Deep capture for project detection, shallow for task
    r_deep = subprocess.run(
        ["tmux", "capture-pane", "-t", session, "-p", "-S", "-300"],
        capture_output=True, text=True,
    )
    if r_deep.returncode != 0:
        return "", ""

    full_text = strip_ansi(r_deep.stdout)
    lines = full_text.split("\n")

    # 1) Project: most-mentioned path in scrollback
    project = detect_project_from_pane(full_text)
    task = ""

    # 2) Fallback: separator line  ──── name ────
    if not project:
        for line in lines:
            clean = line.strip()
            if not clean:
                continue
            dash_count = sum(1 for c in clean if c in "─━═")
            if dash_count < len(clean) * 0.6:
                continue
            m = SEP_PROJECT_RE.search(clean)
            if m:
                name = m.group(1).strip()
                low = name.lower().replace(" ", "-")
                project = PROJECT_MAP.get(low, name)

    # Find last user prompt (line starting with ❯)
    for line in reversed(lines):
        clean = strip_ansi(line).strip()
        if not clean:
            continue
        # Match prompt line: ❯ <user text>
        if clean.startswith("\u276f") or clean.startswith("❯"):
            prompt_text = clean.lstrip("❯\u276f ").strip()
            if prompt_text and len(prompt_text) > 1:
                if len(prompt_text) > 100:
                    prompt_text = prompt_text[:97] + "..."
                task = prompt_text
                break

    return project, task


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        agents_meta = load_agents()

        # Phase 1: gather data + first CPU sample
        session_data = {}
        for t in SESSIONS:
            sid = t["id"]
            session_name = t["session"]

            r = subprocess.run(
                ["tmux", "has-session", "-t", session_name],
                capture_output=True,
            )
            if r.returncode != 0:
                session_data[sid] = {"active": False}
                continue

            pane_pid = get_pane_pid(session_name)
            cwd = get_cwd(session_name)
            pane_project, pane_task = parse_pane(session_name)

            # CWD-based project as fallback
            cwd_project = detect_project_from_cwd(cwd)
            auto_project = pane_project or cwd_project
            auto_task = pane_task

            if not pane_pid:
                session_data[sid] = {
                    "active": True,
                    "auto_project": auto_project,
                    "auto_task": auto_task,
                }
                continue

            children = get_child_pids(pane_pid)
            ticks = read_cpu_ticks(pane_pid)
            for cpid in children:
                ticks += read_cpu_ticks(cpid)
                for gpid in get_child_pids(cpid):
                    ticks += read_cpu_ticks(gpid)
            session_data[sid] = {
                "active": True,
                "pane_pid": pane_pid,
                "ticks1": ticks,
                "auto_project": auto_project,
                "auto_task": auto_task,
            }

        # Phase 2: CPU sampling
        time.sleep(SAMPLE_INTERVAL)

        # Phase 3: build response
        result = {}
        for t in SESSIONS:
            sid = t["id"]
            data = session_data[sid]
            meta = agents_meta.get(sid, {})
            active = data.get("active", False)
            working = False

            if "pane_pid" in data:
                ticks2 = read_cpu_ticks(data["pane_pid"])
                children = get_child_pids(data["pane_pid"])
                for cpid in children:
                    ticks2 += read_cpu_ticks(cpid)
                    for gpid in get_child_pids(cpid):
                        ticks2 += read_cpu_ticks(gpid)
                cpu_delta = ticks2 - data["ticks1"]
                working = cpu_delta > CPU_TICK_THRESHOLD

            auto_proj = data.get("auto_project", "")
            auto_task = data.get("auto_task", "")

            saved_proj = meta.get("project", "")
            locked = meta.get("locked", False)

            # Auto-detect: set project and lock when found
            if not locked and auto_proj:
                if sid not in agents_meta:
                    agents_meta[sid] = {}
                agents_meta[sid]["project"] = auto_proj
                agents_meta[sid]["locked"] = True
                save_agents(agents_meta)
                saved_proj = auto_proj
                locked = True

            result[sid] = {
                "active": active,
                "working": working,
                "path": t["path"],
                "project": saved_proj,
                "auto_project": auto_proj,
                "task": meta.get("task") or auto_task,
                "locked": locked,
                # Per-agent overrides — UI uses these to render the model /
                # effort selectors. Empty string = "use default".
                "model": meta.get("model", ""),
                "effort": meta.get("effort", ""),
            }

        result["_system"] = get_system_stats()
        # Respect the user's saved _order:
        #   - missing key OR empty list → fresh install / new browser →
        #     show every known agent
        #   - non-empty list → echo it exactly, only stripping ids that no
        #     longer exist as a SESSION (e.g. leftover "k1" from a removed
        #     setup). DO NOT re-add ids the user explicitly removed with ×,
        #     or "closed" agents come back after every poll/refresh.
        all_ids = [t["id"] for t in SESSIONS]
        stored = agents_meta.get("_order")
        if isinstance(stored, list) and stored:
            result["_order"] = [i for i in stored if i in all_ids]
        else:
            result["_order"] = all_ids
        self._json_response(200, result)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))

        # Order sync
        if "_order" in body:
            agents = load_agents()
            agents["_order"] = body["_order"]
            save_agents(agents)
            self._json_response(200, {"ok": True})
            return

        agent_id = str(body.get("id", ""))
        valid_ids = {t["id"]: t["session"] for t in SESSIONS}
        if agent_id not in valid_ids:
            self._json_response(400, {"error": "invalid id"})
            return

        # Hard reset: wipe tmux session + conversation JSONL + override entry.
        if body.get("reset"):
            info = reset_agent(agent_id, valid_ids[agent_id])
            self._json_response(200, {"ok": True, **info})
            return

        # Fire-and-forget slash commands ("compact" so far). Pure runtime —
        # only sends if the tmux session is alive; nothing is persisted.
        VALID_ACTIONS = {"compact": "/compact"}
        if "action" in body:
            action = body["action"]
            if action not in VALID_ACTIONS:
                self._json_response(400, {"error": f"unknown action {action!r}"})
                return
            session_name = valid_ids[agent_id]
            r = subprocess.run(
                ["tmux", "has-session", "-t", session_name],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                subprocess.run(
                    ["tmux", "send-keys", "-t", session_name,
                     VALID_ACTIONS[action], "Enter"],
                    capture_output=True, text=True,
                )
            self._json_response(200, {"ok": True, "sent": r.returncode == 0})
            return

        # Effort must be one of these — validated BEFORE we touch the file
        # so a bad value never overwrites anything on disk.
        VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max", "auto"}
        if "effort" in body and body["effort"] not in ({""} | VALID_EFFORTS):
            self._json_response(400, {"error": f"invalid effort {body['effort']!r}"})
            return

        agents = load_agents()
        if agent_id not in agents:
            agents[agent_id] = {}
        if "project" in body:
            agents[agent_id]["project"] = body["project"]
            agents[agent_id]["locked"] = True
        if "task" in body:
            agents[agent_id]["task"] = body["task"]
        if "unlock" in body and body["unlock"]:
            agents[agent_id]["locked"] = False
        # Per-agent model override. Empty string clears the override so the
        # next launch falls back to the Claude subscription default.
        slash_cmds: list[str] = []
        if "model" in body:
            val = body["model"]
            if isinstance(val, str) and val:
                agents[agent_id]["model"] = val
                slash_cmds.append(f"/model {val}")
            else:
                agents[agent_id].pop("model", None)
        # Per-agent effort override. Empty string clears → next launch uses "auto".
        if "effort" in body:
            val = body["effort"]
            if isinstance(val, str) and val:
                agents[agent_id]["effort"] = val
                slash_cmds.append(f"/effort {val}")
            else:
                agents[agent_id].pop("effort", None)
        save_agents(agents)

        # Live-apply: if the agent's tmux session is alive, inject the slash
        # command(s) so the change takes effect immediately. Without this,
        # POSTing only persists to disk and the user wouldn't see anything
        # change in the terminal until the next launch.
        if slash_cmds:
            session_name = valid_ids[agent_id]
            r = subprocess.run(
                ["tmux", "has-session", "-t", session_name],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                for cmd in slash_cmds:
                    subprocess.run(
                        ["tmux", "send-keys", "-t", session_name, cmd, "Enter"],
                        capture_output=True, text=True,
                    )

        self._json_response(200, {"ok": True})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, *args):
        pass


class BufferHandler(BaseHTTPRequestHandler):
    """GET /  — return tmux paste buffer for a session."""
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        session = qs.get("session", [""])[0]
        if not session or not re.match(r'^[\w-]+$', session):
            self._resp(400, {"error": "bad session"})
            return
        r = subprocess.run(
            ["tmux", "show-buffer", "-t", session],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            # try without -t (global buffer)
            r = subprocess.run(["tmux", "show-buffer"], capture_output=True, text=True)
        self._resp(200, {"text": r.stdout if r.returncode == 0 else ""})

    def _resp(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, *args):
        pass


# the actually-served dashboard file (the old ../agents/index.html was archived,
# so page-version was stuck at "0" and live-reload never fired)
WATCH_FILE = os.path.join(os.path.dirname(__file__), "web", "index.html")


class LiveHandler(BaseHTTPRequestHandler):
    """Returns mtime of agents page for live-reload."""
    def do_GET(self):
        try:
            mtime = os.path.getmtime(WATCH_FILE)
        except OSError:
            mtime = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(str(mtime).encode())

    def log_message(self, *args):
        pass


# ── Cookie-session auth (replaces the nginx basic-auth browser dialog) ───────
# The basic-auth dialog re-prompts on every 401, so a burst of polling/reload
# requests showed it dozens of times ("can't even type the password"). A cookie
# session has NO native dialog: one login on a page → signed cookie → every
# request (fetch, iframe, ttyd ws) carries it automatically. Passwords are the
# SAME (verified against the existing /etc/nginx/.htpasswd_agents).

HTPASSWD_FILE = "/etc/nginx/.htpasswd_agents"
AUTH_SECRET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".agents_auth_secret")
AUTH_COOKIE = "agents_session"
AUTH_TTL = 30 * 24 * 3600          # 30 days


def _auth_secret():
    try:
        with open(AUTH_SECRET_FILE, "rb") as f:
            s = f.read().strip()
            if s:
                return s
    except OSError:
        pass
    s = os.urandom(32).hex().encode()
    old = os.umask(0o077)
    try:
        with open(AUTH_SECRET_FILE, "wb") as f:
            f.write(s)
    finally:
        os.umask(old)
    return s


AUTH_SECRET = _auth_secret()


def _sign_token(user, exp):
    sig = hmac.new(AUTH_SECRET, f"{user}.{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{user}.{exp}.{sig}"


def _verify_token(tok):
    if not tok or tok.count(".") < 2:
        return None
    user, exp, sig = tok.rsplit(".", 2)
    if not exp.isdigit() or int(exp) < int(time.time()):
        return None
    expect = hmac.new(AUTH_SECRET, f"{user}.{exp}".encode(), hashlib.sha256).hexdigest()
    return user if hmac.compare_digest(expect, sig) else None


# Standalone/Docker mode: keep a single dashboard password (pbkdf2 hash) in a file the
# user SETS on first run and can change in the UI. If AGENTDECK_PASSFILE is unset, behaviour
# is unchanged (verify against /etc/nginx/.htpasswd_agents) — the live nginx setup is untouched.
PASSFILE = os.environ.get("AGENTDECK_PASSFILE", "")


def _pw_is_set():
    try:
        return bool(PASSFILE) and os.path.getsize(PASSFILE) > 0
    except OSError:
        return False


def _pw_store(pw):
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    old = os.umask(0o077)
    try:
        with open(PASSFILE, "w") as f:
            f.write(f"{salt.hex()}${h.hex()}")
    finally:
        os.umask(old)


def _pw_verify(pw):
    try:
        with open(PASSFILE) as f:
            salt_hex, want = f.read().strip().split("$", 1)
        h = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), bytes.fromhex(salt_hex), 200_000)
        return hmac.compare_digest(h.hex(), want)
    except (OSError, ValueError):
        return False


def _check_password(user, pw):
    if not pw or len(pw) > 256:
        return False
    if PASSFILE:                       # standalone mode: one password, username ignored
        return _pw_verify(pw)
    if not re.match(r'^[A-Za-z0-9_.-]{1,32}$', user or ''):
        return False
    try:
        r = subprocess.run(["htpasswd", "-vb", HTPASSWD_FILE, user, pw],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Agents — login</title>
<style>
*{box-sizing:border-box} html,body{height:100%}
body{margin:0;display:flex;align-items:center;justify-content:center;background:#0d1117;
  color:#e6edf3;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
.card{width:min(92vw,340px);background:#161b22;border:1px solid #30363d;border-radius:14px;
  padding:28px 26px;box-shadow:0 10px 40px rgba(0,0,0,.4)}
h1{margin:0 0 4px;font-size:19px} .sub{margin:0 0 20px;color:#8b949e;font-size:13px}
label{display:block;font-size:12px;color:#8b949e;margin:12px 0 5px}
input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #30363d;background:#0d1117;
  color:#e6edf3;font-size:15px;outline:none}
input:focus{border-color:#388bfd;box-shadow:0 0 0 3px rgba(56,139,253,.25)}
button{width:100%;margin-top:18px;padding:11px;border:0;border-radius:8px;background:#238636;
  color:#fff;font-size:15px;font-weight:600;cursor:pointer}
button:hover{background:#2ea043}
.err{margin:14px 0 0;color:#ff7b72;font-size:13px;text-align:center}
</style></head><body>
<form class="card" method="POST" action="/login">
  <h1>🖥 Agents</h1><p class="sub">Sign in to open the panel</p>
  <label for="u">Username</label>
  <input id="u" name="user" autocomplete="username" autofocus>
  <label for="p">Password</label>
  <input id="p" name="pass" type="password" autocomplete="current-password">
  <button type="submit">Sign in</button>
  __ERR__
</form></body></html>"""


_AUTH_HEAD = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
  '<meta name="viewport" content="width=device-width,initial-scale=1"><title>AgentDeck</title><style>'
  '*{box-sizing:border-box}html,body{height:100%}'
  'body{margin:0;display:flex;align-items:center;justify-content:center;background:#0d1117;'
  'color:#e6edf3;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}'
  '.card{width:min(92vw,340px);background:#161b22;border:1px solid #30363d;border-radius:14px;'
  'padding:28px 26px;box-shadow:0 10px 40px rgba(0,0,0,.4)}'
  'h1{margin:0 0 4px;font-size:19px}.sub{margin:0 0 20px;color:#8b949e;font-size:13px}'
  'label{display:block;font-size:12px;color:#8b949e;margin:12px 0 5px}'
  'input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #30363d;background:#0d1117;'
  'color:#e6edf3;font-size:15px;outline:none}'
  'input:focus{border-color:#388bfd;box-shadow:0 0 0 3px rgba(56,139,253,.25)}'
  'button{width:100%;margin-top:18px;padding:11px;border:0;border-radius:8px;background:#238636;'
  'color:#fff;font-size:15px;font-weight:600;cursor:pointer}button:hover{background:#2ea043}'
  '.err{margin:14px 0 0;color:#ff7b72;font-size:13px;text-align:center}'
  '.ok{margin:14px 0 0;color:#3fb950;font-size:13px;text-align:center}'
  'a{color:#58a6ff}</style></head><body>')


def _setup_form(error=""):
    return (_AUTH_HEAD + '<form class="card" method="POST" action="/login">'
      '<h1>🛰 AgentDeck</h1><p class="sub">Set a password to protect the dashboard</p>'
      '<label for="p">New password</label>'
      '<input id="p" name="pass" type="password" autocomplete="new-password" autofocus>'
      '<label for="p2">Repeat password</label>'
      '<input id="p2" name="pass2" type="password" autocomplete="new-password">'
      '<button type="submit">Set password</button>'
      + (f'<p class="err">{error}</p>' if error else '') + '</form></body></html>')


def _change_form(error="", ok=""):
    return (_AUTH_HEAD + '<form class="card" method="POST" action="/change-password">'
      '<h1>Change password</h1><p class="sub">Enter your current and a new password</p>'
      '<label for="c">Current password</label>'
      '<input id="c" name="cur" type="password" autocomplete="current-password" autofocus>'
      '<label for="p">New password</label>'
      '<input id="p" name="pass" type="password" autocomplete="new-password">'
      '<label for="p2">Repeat new password</label>'
      '<input id="p2" name="pass2" type="password" autocomplete="new-password">'
      '<button type="submit">Change password</button>'
      + (f'<p class="err">{error}</p>' if error else '')
      + (f'<p class="ok">{ok}</p>' if ok else '')
      + '<p style="text-align:center;margin-top:14px"><a href="/">&larr; back to dashboard</a></p></form></body></html>')


def _cookie_flags(secure=True):
    # HttpOnly: JS can't read it. Secure (HTTPS-only) is dropped on plain http so the
    # cookie actually comes back (e.g. a Docker http port).
    return "Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s" % (AUTH_TTL, "; Secure" if secure else "")


class AuthHandler(BaseHTTPRequestHandler):
    """Cookie login. /check (auth probe), /login (form/verify; FIRST RUN in
    AGENTDECK_PASSFILE mode = set the password), /change-password, /logout."""

    def _secure(self):
        return self.headers.get("X-Forwarded-Proto", "") == "https"

    def _html(self, html, code=200):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_login(self, error=False):
        if PASSFILE and not _pw_is_set():
            return self._html(_setup_form("Passwords must match (6+ chars)" if error else ""),
                              401 if error else 200)
        html = LOGIN_HTML.replace("__ERR__", '<p class="err">Invalid username or password</p>' if error else '')
        self._html(html, 401 if error else 200)

    def _login_cookie(self, user):
        tok = _sign_token(user or "admin", int(time.time()) + AUTH_TTL)
        self.send_response(302)
        self.send_header("Set-Cookie", f"{AUTH_COOKIE}={tok}; {_cookie_flags(self._secure())}")
        self.send_header("Location", "/")
        self.end_headers()

    def _cookies(self):
        out = {}
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def _user(self):
        return _verify_token(self._cookies().get(AUTH_COOKIE, ""))

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/check":
            self.send_response(200 if self._user() else 401)
            self.end_headers()
            return
        if path == "/logout":
            self.send_response(302)
            self.send_header("Set-Cookie", f"{AUTH_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            self.send_header("Location", "/login")
            self.end_headers()
            return
        if path == "/change-password":
            if not (PASSFILE and self._user()):
                return self._redirect("/login")
            return self._html(_change_form())
        self._send_login()      # /login (or anything else) → the form

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length).decode("utf-8", "replace") if 0 < length <= 4096 else ""
        form = parse_qs(body)

        if path == "/change-password":
            if not (PASSFILE and self._user()):
                return self._redirect("/login")
            cur = form.get("cur", [""])[0]
            new, new2 = form.get("pass", [""])[0], form.get("pass2", [""])[0]
            if not _pw_verify(cur):
                return self._html(_change_form(error="Current password is wrong"))
            if len(new) < 6 or new != new2:
                return self._html(_change_form(error="New passwords must match (6+ chars)"))
            _pw_store(new)
            return self._html(_change_form(ok="Password changed."))

        # /login — FIRST RUN (passfile mode, no password yet) sets it; otherwise verify
        if PASSFILE and not _pw_is_set():
            new, new2 = form.get("pass", [""])[0], form.get("pass2", [""])[0]
            if len(new) < 6 or new != new2:
                return self._html(_setup_form("Passwords must match (6+ chars)"))
            _pw_store(new)
            return self._login_cookie("admin")

        user = (form.get("user", [""])[0]).strip()
        pw = form.get("pass", [""])[0]
        if _check_password(user, pw):
            self._login_cookie(user)
        else:
            self._send_login(error=True)

    def log_message(self, *args):
        pass


# ── Clipboard image paste → save to tgimg, hand the terminal a file path ─────
# A web terminal (ttyd/tmux) can't accept a pasted image — it's text-only. So the
# dashboard intercepts an image paste, POSTs the bytes here, and types the saved
# file PATH into the terminal so the agent (Claude Code) can Read the image.

PASTE_DIR = os.environ.get("TG_FILES_DIR_IMG", "/home/ubuntu/pr/tgimg")
PASTE_URL = "https://reimake.com/tgimg"          # canonical host, NEVER yanhs.stream
_PASTE_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/bmp": ".bmp", "image/svg+xml": ".svg",
}


def _paste_ext(ctype):
    return _PASTE_EXT.get((ctype or "").split(";")[0].strip().lower())


def save_paste_image(data: bytes, ctype: str, token: str, dest_dir: str = PASTE_DIR) -> dict:
    """Save pasted image bytes to dest_dir as paste_<token><ext>; return {path,url}."""
    ext = _paste_ext(ctype)
    if not ext:
        raise ValueError(f"unsupported content-type: {ctype!r}")
    if not data:
        raise ValueError("empty body")
    name = f"paste_{token}{ext}"
    os.makedirs(dest_dir, exist_ok=True)
    with open(os.path.join(dest_dir, name), "wb") as f:
        f.write(data)
    return {"path": os.path.join(dest_dir, name), "url": f"{PASTE_URL}/{name}"}


class PasteHandler(BaseHTTPRequestHandler):
    """POST raw image bytes (Content-Type: image/*) → save → {path, url} JSON."""

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            ctype = (self.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
            length = int(self.headers.get("Content-Length", "0") or "0")
            # diagnostic client log (so we can SEE what happens in the user's browser)
            if ctype == "application/json":
                body = self.rfile.read(length).decode("utf-8", "replace") if 0 < length <= 65536 else ""
                print("PASTE-CLIENTLOG", time.strftime("%H:%M:%S"), body[:600], flush=True)
                self._json(200, {"ok": True}); return
            if length <= 0 or length > 30 * 1024 * 1024:
                self._json(413, {"error": "empty or too large"}); return
            if not _paste_ext(ctype):
                self._json(415, {"error": "not an image"}); return
            data = self.rfile.read(length)
            res = save_paste_image(data, ctype, os.urandom(6).hex())
            print("PASTE-SAVED", time.strftime("%H:%M:%S"), res["path"], flush=True)
            self._json(200, res)
        except Exception as e:
            self._json(500, {"error": str(e)[:200]})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    from threading import Thread
    Thread(target=lambda: HTTPServer(("127.0.0.1", 3014), LiveHandler).serve_forever(), daemon=True).start()
    Thread(target=lambda: HTTPServer(("127.0.0.1", 3045), BufferHandler).serve_forever(), daemon=True).start()
    Thread(target=lambda: HTTPServer(("127.0.0.1", 3046), AuthHandler).serve_forever(), daemon=True).start()
    Thread(target=lambda: HTTPServer(("127.0.0.1", 3047), PasteHandler).serve_forever(), daemon=True).start()
    HTTPServer(("127.0.0.1", 3011), Handler).serve_forever()
