"""TDD: GET /api/terminal-status must NOT silently re-add agents the user
explicitly removed (×) from the dashboard list.

The old "heal a truncated _order" logic union-merged the stored list with
all SESSIONS, which meant any agent the user closed would reappear on the
very next poll / page refresh.

New contract:
  - stored _order is missing → return all SESSION ids (fresh install)
  - stored _order is an empty list → also return all SESSION ids
  - stored _order is a non-empty list → return EXACTLY that list, filtered
    only against the set of valid SESSION ids (drops dead entries like an
    old "k1" reference, but does NOT add anything)
"""
from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


TERMINAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TERMINAL_DIR))


def _load():
    sys.modules.pop("status_server", None)
    return importlib.import_module("status_server")


def _do_get(mod):
    """Invoke do_GET via a handler shim and return the parsed JSON response."""
    h = mod.Handler.__new__(mod.Handler)

    captured: dict = {}

    def _resp(code, data):
        captured["code"] = code
        captured["data"] = data

    h._json_response = _resp  # type: ignore[attr-defined]
    h.do_GET()
    return captured


def _stub_subprocess(mod):
    """Stub subprocess.run so do_GET doesn't actually shell out to tmux/pgrep."""
    def fake_run(cmd, *a, **kw):
        # Pretend no tmux session exists for any sid
        return MagicMock(returncode=1, stdout="", stderr="")
    return patch.object(mod.subprocess, "run", side_effect=fake_run)


# --- the failing-then-passing test ---------------------------------------


def test_user_removed_agents_stay_removed(tmp_path, monkeypatch):
    """User saved _order=['1','2']. GET must echo ['1','2'], not all 12."""
    mod = _load()

    agents_file = tmp_path / "agents.json"
    agents_file.write_text(json.dumps({
        "1": {"project": "Foo"},
        "_order": ["1", "2"],
    }))
    monkeypatch.setattr(mod, "AGENTS_FILE", str(agents_file))

    with _stub_subprocess(mod):
        out = _do_get(mod)

    assert out["code"] == 200
    order = out["data"].get("_order")
    assert order == ["1", "2"], (
        f"GET must respect user's saved _order; got {order} "
        "— the healing logic is re-adding closed agents."
    )


def test_missing_order_returns_first_four(tmp_path, monkeypatch):
    """Fresh install (no _order key) → start with the first four agents."""
    mod = _load()
    agents_file = tmp_path / "agents.json"
    agents_file.write_text("{}")
    monkeypatch.setattr(mod, "AGENTS_FILE", str(agents_file))

    with _stub_subprocess(mod):
        out = _do_get(mod)

    all_ids = [t["id"] for t in mod.SESSIONS]
    assert out["data"]["_order"] == all_ids[:4]


def test_empty_order_returns_first_four(tmp_path, monkeypatch):
    """An explicit empty list is treated the same as missing — the first four agents."""
    mod = _load()
    agents_file = tmp_path / "agents.json"
    agents_file.write_text(json.dumps({"_order": []}))
    monkeypatch.setattr(mod, "AGENTS_FILE", str(agents_file))

    with _stub_subprocess(mod):
        out = _do_get(mod)

    all_ids = [t["id"] for t in mod.SESSIONS]
    assert out["data"]["_order"] == all_ids[:4]


def test_order_with_stale_ids_is_filtered_not_extended(tmp_path, monkeypatch):
    """Drop unknown ids ('k1' from the removed kosha setup) but DON'T add any
    new ones."""
    mod = _load()
    agents_file = tmp_path / "agents.json"
    agents_file.write_text(json.dumps({"_order": ["1", "k1", "2", "k2"]}))
    monkeypatch.setattr(mod, "AGENTS_FILE", str(agents_file))

    with _stub_subprocess(mod):
        out = _do_get(mod)

    assert out["data"]["_order"] == ["1", "2"]
