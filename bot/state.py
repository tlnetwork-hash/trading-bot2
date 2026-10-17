"""Saving the bot's memory to disk so it can pick up where it left off."""

import json
import os
from collections import deque
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc)


def new_state(cfg):
    return {
        "version": 1,
        "mode": cfg.mode,
        "symbol": cfg.symbol,
        "status": "running",          # running | paused | halted
        "status_reason": "",
        "created_at": utcnow().isoformat(timespec="seconds"),
        "start_equity": cfg.capital_usd,
        "balances": {"quote": cfg.capital_usd, "base": 0.0},
        "grid": None,
        "orders": {},                 # grid level index -> order
        "deferred": [],               # counter orders waiting for their level to free up
        "stats": {"buys": 0, "sells": 0, "round_trips": 0, "grid_profit": 0.0, "fees": 0.0},
        "report": {"last_date": None, "equity": cfg.capital_usd, "fills": 0},
        "paper": {"seq": 0, "orders": {}},
        "last_price": None,
    }


def append_jsonl(path, record):
    """Add one line to a log file (equity history, trade log)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def read_jsonl_tail(path, max_lines):
    try:
        with open(path) as f:
            lines = deque(f, maxlen=max_lines)
    except FileNotFoundError:
        return []
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            pass  # a line cut short by a crash
    return records


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written file
