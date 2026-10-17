"""Read-only web dashboard. It only reads the files the bot writes in data/,
so it can never place, cancel or change anything.

    python -m bot dashboard      (Docker runs this as the "dashboard" service)
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .state import load_state, read_jsonl_tail

log = logging.getLogger(__name__)

PAGE = (Path(__file__).parent / "dashboard.html").read_bytes()
HISTORY_POINTS = 12 * 24 * 90   # 90 days of 5-minute points
TRADE_ROWS = 50

# Price chart ranges: name -> (candle size, number of candles)
CANDLE_RANGES = {
    "1h": ("1m", 60),
    "6h": ("5m", 72),
    "24h": ("15m", 96),
    "7d": ("1h", 168),
    "30d": ("4h", 180),
}
CANDLE_CACHE_SECONDS = 10


class CandleSource:
    """Fetches price candles from Kraken's public API, cached so that several
    open browser tabs don't multiply requests to Kraken."""

    def __init__(self, symbol, fetch_ohlcv=None):
        self.symbol = symbol
        self._fetch = fetch_ohlcv
        self._cache = {}
        self._lock = threading.Lock()

    def _fetch_ohlcv(self, timeframe, limit):
        if self._fetch is None:
            import ccxt
            client = ccxt.kraken({"enableRateLimit": True, "timeout": 15000})
            self._fetch = lambda tf, n: client.fetch_ohlcv(self.symbol, tf, limit=n)
        return self._fetch(timeframe, limit)

    def get(self, range_name):
        if range_name not in CANDLE_RANGES:
            return {"error": f"unknown range {range_name!r}"}
        timeframe, count = CANDLE_RANGES[range_name]
        with self._lock:
            cached = self._cache.get(range_name)
            if cached and time.time() - cached[0] < CANDLE_CACHE_SECONDS:
                return cached[1]
            try:
                rows = self._fetch_ohlcv(timeframe, count)
            except Exception as e:
                log.warning("could not fetch candles: %s", e)
                if cached:
                    return cached[1]  # better a slightly old chart than none
                return {"error": "could not reach Kraken for price data"}
            result = {
                "symbol": self.symbol,
                "range": range_name,
                "timeframe": timeframe,
                "candles": [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows[-count:]],
            }
            self._cache[range_name] = (time.time(), result)
            return result


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


def make_handler(cfg, candles=None):
    candles = candles or CandleSource(cfg.symbol)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE)
            elif path == "/api/state":
                body = json.dumps(snapshot(cfg)).encode()
                self._send(200, "application/json", body)
            elif path == "/api/candles":
                params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                body = json.dumps(candles.get(params.get("range", "24h"))).encode()
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


STALE_AFTER = 300  # seconds without a state update before the watchdog alerts


class Watchdog:
    """Alerts if the bot stops saving its state (crashed, stuck, or restarting
    over and over). It runs in the dashboard container, so it keeps working
    when the bot container doesn't."""

    def __init__(self, cfg, notifier, clock=time.time):
        self.cfg = cfg
        self.notifier = notifier
        self.clock = clock
        self.alerted = False

    def check(self):
        try:
            age = self.clock() - os.path.getmtime(self.cfg.state_path)
        except OSError:
            return  # bot hasn't run yet
        if age > STALE_AFTER and not self.alerted:
            self.alerted = True
            self.notifier.send(
                "Bot stopped updating",
                f"The bot hasn't saved anything for {age / 60:.0f} minutes. It may have crashed or be stuck.\n"
                "Check with: docker compose ps  and  docker compose logs --tail 50 bot\n"
                "(Or ask Claude to look.)",
                priority="urgent",
            )
        elif age <= STALE_AFTER and self.alerted:
            self.alerted = False
            self.notifier.send("Bot is running again", "The bot is saving updates normally again.")

    def run_forever(self, interval=60):
        while True:
            try:
                self.check()
            except Exception:
                log.exception("watchdog check failed")
            time.sleep(interval)


def serve(cfg, host="0.0.0.0", port=8080, notifier=None):
    if notifier is not None:
        threading.Thread(target=Watchdog(cfg, notifier).run_forever, daemon=True).start()
    server = ThreadingHTTPServer((host, port), make_handler(cfg))
    log.info("dashboard on http://%s:%d", host, port)
    server.serve_forever()
