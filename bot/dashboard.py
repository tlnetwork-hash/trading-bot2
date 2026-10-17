"""Read-only web dashboard. It only reads the files the bot writes in data/,
so it can never place, cancel or change anything.

    python -m bot dashboard      (Docker runs this as the "dashboard" service)
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .state import load_state, read_jsonl_tail

log = logging.getLogger(__name__)

PAGE = (Path(__file__).parent / "dashboard.html").read_bytes()
HISTORY_POINTS = 12 * 24 * 90   # 90 days of 5-minute points
TRADE_ROWS = 50


def snapshot(cfg):
    state = load_state(cfg.state_path)
    if state is None:
        return {"error": f"no {cfg.mode} data yet - is the bot running?"}
    price = state.get("last_price") or 0
    b = state["balances"]
    grid = state.get("grid")
    orders = sorted(
        ({"index": int(k), "side": o["side"], "price": o["price"], "amount": o["amount"]}
         for k, o in state["orders"].items()),
        key=lambda o: o["index"],
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    history = [h for h in read_jsonl_tail(cfg.history_path, HISTORY_POINTS) if h["t"] >= cutoff]
    try:
        age = time.time() - os.path.getmtime(cfg.state_path)
    except OSError:
        age = None
    return {
        "mode": state["mode"],
        "symbol": state["symbol"],
        "status": state["status"],
        "status_reason": state["status_reason"],
        "created_at": state["created_at"],
        "price": price,
        "equity": b["quote"] + b["base"] * price,
        "start_equity": state["start_equity"],
        "kill_floor": state["start_equity"] * (1 - cfg.risk.max_drawdown_pct / 100),
        "breakout_action": cfg.risk.breakout_action,
        "fees": {"maker_pct": cfg.fees.maker_pct, "taker_pct": cfg.fees.taker_pct},
        "balances": b,
        "stats": state["stats"],
        "grid": grid and {k: grid[k] for k in
                          ("levels", "lower", "upper", "spacing_pct", "created_at", "created_price")},
        "orders": orders,
        "history": history,
        "trades": read_jsonl_tail(cfg.trades_path, TRADE_ROWS),
        "updated_seconds_ago": age,
    }


def make_handler(cfg):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE)
            elif path == "/api/state":
                body = json.dumps(snapshot(cfg)).encode()
                self._send(200, "application/json", body)
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code, content_type, body):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass  # keep the logs for trading activity

    return Handler


def serve(cfg, host="0.0.0.0", port=8080):
    server = ThreadingHTTPServer((host, port), make_handler(cfg))
    log.info("dashboard on http://%s:%d", host, port)
    server.serve_forever()
