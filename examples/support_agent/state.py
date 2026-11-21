"""
State for the support_agent stub tools: one JSON file per session,
{working_dir}/{session_id}/support_state.json:

    {"orders":  {"<id>": {...}},
     "tickets": {"<id>": {"order_id", "status", "notes": [...]}},
     "refunds": [{"refund_id", "order_id", "amount", "method", "eta"}]}

Eval fixtures seed it before a run and read it afterwards (outside the agent's tools).
"""
import json
import os
from pathlib import Path

STATE_FILE = "support_state.json"


def state_path(session_dir) -> Path:
    return Path(session_dir) / STATE_FILE


def empty_state() -> dict:
    return {"orders": {}, "tickets": {}, "refunds": []}


def load_state(session_dir) -> dict:
    path = state_path(session_dir)
    if not path.exists():
        return empty_state()
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(session_dir, state: dict) -> None:
    path = state_path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)
