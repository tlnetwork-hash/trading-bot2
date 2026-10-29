"""Read-only web dashboard. It only reads the files the bot writes in data/,
so it can never place, cancel or change anything.

    python -m bot dashboard      (Docker runs this as the "dashboard" service)
"""

import json
import logging
import os
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import EXCHANGES
from .state import load_state, read_jsonl_tail
from .trader import profit_lock_floor, rebuy_after, rebuy_target

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
    """Fetches price candles from the exchange's public API, cached so that
    several open browser tabs don't multiply requests to the exchange."""

    def __init__(self, symbol, fetch_ohlcv=None, exchange="kraken"):
        self.symbol = symbol
        self.exchange = exchange
        self._fetch = fetch_ohlcv
        self._cache = {}
        self._lock = threading.Lock()

    def _fetch_ohlcv(self, timeframe, limit):
        if self._fetch is None:
            from .exchange import make_client
            client = make_client(self.exchange, timeout=15000)
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
                return {"error": "could not reach the exchange for price data"}
            result = {
                "symbol": self.symbol,
                "range": range_name,
                "timeframe": timeframe,
                "candles": [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows[-count:]],
            }
            self._cache[range_name] = (time.time(), result)
            return result


TICKER_CACHE_SECONDS = 2


class TickerSource:
    """Latest price from the bot's own exchange (public API, no key), cached
    briefly so open tabs polling every few seconds share one request."""

    def __init__(self, symbol, exchange):
        self.symbol, self.exchange = symbol, exchange
        self._client = None
        self._cache = None
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            if self._cache and time.time() - self._cache[0] < TICKER_CACHE_SECONDS:
                return self._cache[1]
            try:
                if self._client is None:
                    from .exchange import make_client
                    self._client = make_client(self.exchange, timeout=8000)
                t = self._client.fetch_ticker(self.symbol)
                result = {"symbol": self.symbol, "exchange": self.exchange,
                          "last": t.get("last"), "bid": t.get("bid"), "ask": t.get("ask"),
                          "time": t.get("timestamp") or int(time.time() * 1000)}
            except Exception as e:
                log.warning("could not fetch ticker: %s", e)
                if self._cache:
                    return dict(self._cache[1], stale=True)
                return {"error": "could not reach the exchange"}
            self._cache = (time.time(), result)
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
        "exchange": cfg.exchange,
        "exchange_name": cfg.exchange_name,
        "links": cfg.dashboard_links,
        "loss_log": _read_loss_log(cfg),
        "chart_exchange": cfg.chart_source,
        "chart_exchange_name": EXCHANGES[cfg.chart_source],
        "service": cfg.service_name,
        "symbol": state["symbol"],
        "status": state["status"],
        "status_reason": state["status_reason"],
        "created_at": state["created_at"],
        "price": price,
        "equity": b["quote"] + b["base"] * price,
        "start_equity": state["start_equity"],
        "kill_floor": state["start_equity"] * (1 - cfg.risk.max_drawdown_pct / 100),
        "breakout_action": cfg.risk.breakout_action,
        "sell_on_breakout_down": cfg.risk.sell_on_breakout_down,
        "profit_lock": _profit_lock(cfg, state),
        "rebuy": _rebuy(cfg, state),
        "fees": {"maker_pct": cfg.fees.maker_pct, "taker_pct": cfg.fees.taker_pct},
        "balances": b,
        "stats": state["stats"],
        "grid": grid and {k: grid[k] for k in
                          ("levels", "lower", "upper", "spacing_pct", "created_at", "created_price")},
        "orders": orders,
        "history": history,
        "trades": read_jsonl_tail(cfg.trades_path, TRADE_ROWS),
        "updated_seconds_ago": age,
        "trend": _trend(cfg, state),
    }


def _trend(cfg, state):
    """Per-coin positions and signals for the trend strategy (None for grid)."""
    if state.get("strategy") != "trend":
        return None
    rows = []
    for sym, sl in state.get("sleeves", {}).items():
        price = (state.get("prices") or {}).get(sym) or 0
        sig = (state.get("signals") or {}).get(sym) or {}
        rows.append({
            "symbol": sym, "coin": sym.split("/")[0], "price": price, "qty": sl["qty"], "cash": sl["cash"],
            "value": sl["cash"] + sl["qty"] * price, "close": sig.get("close"), "sma": sig.get("sma"),
            "up": sig.get("up"),
        })
    return {"sma_days": cfg.trend.sma_days, "rows": rows}


def _profit_lock(cfg, state):
    risk = cfg.risk
    if risk.profit_lock_start_pct <= 0:
        return None
    lock = state.get("lock") or {"base": state["start_equity"], "peak": state["start_equity"]}
    return {
        "base": lock["base"],
        "peak": lock["peak"],
        "arm_at": lock["base"] * (1 + risk.profit_lock_start_pct / 100),
        "floor": profit_lock_floor(risk, lock),
        "keep_pct": risk.profit_lock_keep_pct,
    }


def _rebuy(cfg, state):
    rebuy = state.get("rebuy")
    if not rebuy or state["status"] != "paused":
        return None
    return {
        "low": rebuy["low"],
        "sold_price": rebuy["sold_price"],
        "target": rebuy_target(cfg.risk, rebuy),
        "after": rebuy_after(cfg.risk, rebuy).isoformat(timespec="seconds"),
    }


NO_ACCESS = (b"Login not set up. This dashboard was reached through Cloudflare without a "
             b"Cloudflare Access login in front of it. Add this address to your Access "
             b"application (see README \"Viewing from any computer\").\n")


def make_handler(cfg, candles=None):
    # One candle source per coin the page can chart (the trend bot charts each of its coins).
    sources = {cfg.symbol: candles or CandleSource(cfg.symbol, exchange=cfg.chart_source)}
    if cfg.strategy == "trend":
        for sym in cfg.trend.symbols:
            sources.setdefault(sym, CandleSource(sym, exchange=cfg.chart_source))

    ticker = TickerSource(cfg.symbol, cfg.exchange)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path, _, query = self.path.partition("?")
            if self.headers.get("Cf-Ray") and not self.headers.get("Cf-Access-Jwt-Assertion"):
                # Came through a Cloudflare Tunnel without passing Cloudflare
                # Access, so the login step was never set up. Fail closed.
                self._send(403, "text/plain; charset=utf-8", NO_ACCESS)
            elif path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE)
            elif path == "/api/state":
                body = json.dumps(snapshot(cfg)).encode()
                self._send(200, "application/json", body)
            elif path == "/api/ticker":
                self._send(200, "application/json", json.dumps(ticker.get()).encode())
            elif path == "/api/candles":
                params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                sym = urllib.parse.unquote(params.get("symbol", "")) or cfg.symbol
                source = sources.get(sym)
                result = source.get(params.get("range", "24h")) if source else {"error": f"unknown symbol {sym!r}"}
                body = json.dumps(result).encode()
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


def _read_loss_log(cfg):
    """Contents of data/LOSS-LOG.txt if this bot has one, else None."""
    try:
        return (cfg.data_dir / "LOSS-LOG.txt").read_text()[-4000:]
    except OSError:
        return None
