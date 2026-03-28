#!/usr/bin/env python3
"""Agent status server.

GET  /status  — live status (working/idle/offline) + agent metadata (project, task)
POST /agents  — update agent metadata  { "id": "1", "project": "...", "task": "..." }
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import subprocess
import time
import os

SESSIONS = [
    {"id": "1", "session": "claude-terminal",   "path": "terminal"},
    {"id": "2", "session": "claude-terminal-2",  "path": "terminal2"},
    {"id": "3", "session": "claude-terminal-3",  "path": "terminal3"},
    {"id": "4", "session": "claude-terminal-4",  "path": "terminal4"},
    {"id": "5", "session": "claude-terminal-5",  "path": "terminal5"},
    {"id": "6", "session": "claude-terminal-6",  "path": "terminal6"},
]

AGENTS_FILE = os.path.join(os.path.dirname(__file__), "agents.json")
SAMPLE_INTERVAL = 0.15
CPU_TICK_THRESHOLD = 2


def load_agents():
    try:
        with open(AGENTS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_agents(data):
    with open(AGENTS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_pane_pid(session_name):
    r = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        agents_meta = load_agents()

        # Phase 1: gather PIDs and first CPU sample
        session_data = {}
        for t in SESSIONS:
            sid = t["id"]
            r = subprocess.run(
                ["tmux", "has-session", "-t", t["session"]],
                capture_output=True,
            )
            if r.returncode != 0:
                session_data[sid] = {"active": False, "working": False}
                continue

            pane_pid = get_pane_pid(t["session"])
            if not pane_pid:
                session_data[sid] = {"active": True, "working": False}
                continue

            children = get_child_pids(pane_pid)
            ticks = read_cpu_ticks(pane_pid)
            session_data[sid] = {
                "pane_pid": pane_pid,
                "children": children,
                "ticks1": ticks,
            }

        # Phase 2: single sleep
        time.sleep(SAMPLE_INTERVAL)

        # Phase 3: compute status and merge with metadata
        result = {}
        for t in SESSIONS:
            sid = t["id"]
            data = session_data[sid]
            meta = agents_meta.get(sid, {})

            if "pane_pid" not in data:
                result[sid] = {
                    **data,
                    "path": t["path"],
                    "project": meta.get("project", ""),
                    "task": meta.get("task", ""),
                }
                continue

            ticks2 = read_cpu_ticks(data["pane_pid"])
            cpu_delta = ticks2 - data["ticks1"]
            has_children = len(data["children"]) > 0
            working = has_children or cpu_delta > CPU_TICK_THRESHOLD

            result[sid] = {
                "active": True,
                "working": working,
                "path": t["path"],
                "project": meta.get("project", ""),
                "task": meta.get("task", ""),
            }

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
        if "task" in body:
            agents[agent_id]["task"] = body["task"]
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


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 3011), Handler).serve_forever()
