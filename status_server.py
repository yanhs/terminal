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
    {"id": "11", "session": "claude-terminal-11",  "path": "terminal11"},
    {"id": "12", "session": "claude-terminal-12",  "path": "terminal12"},
]

AGENTS_FILE = os.path.join(os.path.dirname(__file__), "agents.json")
SAMPLE_INTERVAL = 0.15
_prev_cpu = None
CPU_TICK_THRESHOLD = 2

PROJECT_MAP = {
    "shiftmesh": "ShiftMesh",
    "cleaning": "CleanSlate",
    "hvac": "ClimatePro",
    "band": "Zolotoi Zvuk",
    "makeblitz": "MakeBlitz",
    "svetlota": "Svetlota",
    "claude-code-telegram": "Telegram Bot",
    "orchestra": "Agent Orchestra",
    "terminal": "Terminal",
    "agents": "Agents Dashboard",
    "yacht": "Yacht",
    "vpn": "VPN",
    "kolobokvpn": "KolobokVPN",
    "ianprog": "IanProg",
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

    # Find last meaningful line (bottom-up), skip all noise
    for line in reversed(lines):
        clean = strip_ansi(line).strip()
        if not clean:
            continue
        # Skip if line is ONLY box-drawing, whitespace, or block chars
        if not re.search(r'[a-zA-Z\u0400-\u04FF\u4e00-\u9fff0-9]', clean):
            continue
        # Skip Claude UI chrome
        if any(k in clean.lower() for k in (
            "bypass permissions", "shift+tab", "esc to interrupt",
            "ctrl+t to", "ctrl+c to",
        )):
            continue
        # Skip bare prompt
        if clean in ("\u276f", "❯", ">", "$"):
            continue
        # Skip Claude Code splash/animation (starts with block drawing chars)
        if re.match(r'^[▘▝▖▗▀▄█▌▐▛▜▙▟░▒▓▐]', clean):
            continue
        # Skip rating prompt like "1: Bad   2: Fine"
        if re.match(r'^\d+:\s*\w+\s+\d+:\s*\w+', clean):
            continue
        # Skip separator lines (with or without embedded project name)
        stripped_dashes = re.sub(r'[─━═\s]', '', clean)
        if not stripped_dashes or (
            len(stripped_dashes) < len(clean) * 0.3
            and re.match(r'^[─━═]', clean)
        ):
            continue
        # Got meaningful content
        if len(clean) > 100:
            clean = clean[:97] + "..."
        task = clean
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
            }

        result["_system"] = get_system_stats()
        self._json_response(200, result)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        agent_id = str(body.get("id", ""))
        if agent_id not in {t["id"] for t in SESSIONS}:
            self._json_response(400, {"error": "invalid id"})
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
        save_agents(agents)
        self._json_response(200, {"ok": True})

    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, *args):
        pass


WATCH_FILE = os.path.join(os.path.dirname(__file__), "..", "agents", "index.html")


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


if __name__ == "__main__":
    from threading import Thread
    Thread(target=lambda: HTTPServer(("127.0.0.1", 3014), LiveHandler).serve_forever(), daemon=True).start()
    HTTPServer(("127.0.0.1", 3011), Handler).serve_forever()
