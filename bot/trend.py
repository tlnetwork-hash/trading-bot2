"""Trend-following basket (strategy: trend). Paper mode only for now.

Each coin in trend.symbols gets an equal slice of capital_usd. Once a day,
after the UTC daily candle closes, the bot checks each coin: if yesterday's
close is above its trend.sma_days daily average it holds that coin,
otherwise that slice sits in cash. Backtest notes: /root/backtest.

The state file keeps the same top-level keys as the grid bot so the
dashboard works unchanged (grid stays empty; balances.quote holds total
equity, and status_reason lists what is held).
"""

import logging
import threading
from datetime import datetime, timezone

import ccxt

from .exchange import make_client
from .state import append_jsonl, save_state, utcnow

log = logging.getLogger(__name__)

HISTORY_INTERVAL = 300
NETWORK_ALERT_AFTER = 20


def new_trend_state(cfg):
    syms = cfg.trend.symbols
    slice_usd = cfg.capital_usd / len(syms)
    return {
        "version": 1,
        "strategy": "trend",
        "mode": cfg.mode,
        "symbol": cfg.symbol,
        "status": "running",
        "status_reason": "",
        "created_at": utcnow().isoformat(timespec="seconds"),
        "start_equity": cfg.capital_usd,
        "sleeves": {s: {"cash": slice_usd, "qty": 0.0, "cost": 0.0} for s in syms},
        "signals": {},
        "last_signal_day": None,
        "prices": {},
        "balances": {"quote": cfg.capital_usd, "base": 0.0},
        "grid": None,
        "orders": {},
        "stats": {"buys": 0, "sells": 0, "round_trips": 0, "grid_profit": 0.0, "fees": 0.0},
        "report": {"last_date": None, "equity": cfg.capital_usd, "fills": 0},
        "last_price": None,
    }


class TrendBot:
    def __init__(self, cfg, notifier, state, client=None):
        self.cfg = cfg
        self.notifier = notifier
        self.state = state
        self.client = client or make_client(cfg.exchange)
        self.client.load_markets()
        for sym in cfg.trend.symbols:
            if sym not in self.client.markets:
                raise ValueError(f"{cfg.exchange_name} has no market {sym}")
        if sorted(state["sleeves"]) != sorted(cfg.trend.symbols):
            raise ValueError("trend.symbols changed since this run started; run 'reset' "
                             f"or delete {cfg.state_path}")
        self.last_history = None
        self.network_failures = 0
        self.last_error_email = None

    # ------------------------------------------------------------------ loop

    def run_forever(self, stop_event=None):
        stop_event = stop_event or threading.Event()
        self.notifier.send("Trend bot started", self.summary())
        while not stop_event.is_set():
            try:
                self.tick()
                self.network_failures = 0
            except ccxt.NetworkError as e:
                self.network_failures += 1
                log.warning("network problem (%d in a row): %s", self.network_failures, e)
                if self.network_failures == NETWORK_ALERT_AFTER:
                    self.notifier.send(f"Cannot reach {self.cfg.exchange_name}", str(e), priority="high")
            except Exception as e:
                log.exception("error during tick")
                self._error_email(e)
            stop_event.wait(self.cfg.poll_seconds)
        log.info("bot stopped")

    def tick(self):
        st = self.state
        prices = {s: float(self.client.fetch_ticker(s)["last"]) for s in self.cfg.trend.symbols}
        st["prices"] = prices
        st["last_price"] = prices.get(self.cfg.symbol) or next(iter(prices.values()))
        self._handle_command(prices)
        st = self.state
        today = utcnow().date().isoformat()
        if st["status"] == "running" and st["last_signal_day"] != today:
            self._evaluate(prices, today)
        self._check_kill_switch(prices)
        self._refresh(prices)
        self._maybe_daily_report(prices)
        self._record_history(prices)
        self.save()

    def save(self):
        save_state(self.cfg.state_path, self.state)

    # -------------------------------------------------------------- strategy

    def equity(self, prices=None):
        prices = prices or self.state["prices"]
        return sum(sl["cash"] + sl["qty"] * prices.get(s, 0) for s, sl in self.state["sleeves"].items())

    def _signal(self, sym):
        n = self.cfg.trend.sma_days
        rows = self.client.fetch_ohlcv(sym, "1d", limit=n + 5)
        today_ms = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                          microsecond=0).timestamp() * 1000)
        closed = [r for r in rows if r[0] < today_ms]
        if len(closed) < n:
            return None
        closes = [float(r[4]) for r in closed[-n:]]
        avg = sum(closes) / n
        return {"up": closes[-1] > avg, "close": closes[-1], "sma": round(avg, 4)}

    def _evaluate(self, prices, today):
        st = self.state
        for sym in self.cfg.trend.symbols:
            sig = self._signal(sym)
            if sig is None:
                log.warning("not enough daily candles for %s yet", sym)
                continue
            sig["day"] = today
            st["signals"][sym] = sig
            sl = st["sleeves"][sym]
            log.info("%s close %.2f vs %d-day avg %.2f -> %s", sym, sig["close"],
                     self.cfg.trend.sma_days, sig["sma"], "HOLD" if sig["up"] else "CASH")
            if sig["up"] and sl["qty"] == 0 and sl["cash"] > 0:
                self._trade(sym, "buy", prices[sym], "trend turned up")
            elif not sig["up"] and sl["qty"] > 0:
                self._trade(sym, "sell", prices[sym], "trend turned down")
        st["last_signal_day"] = today

    def _trade(self, sym, side, price, why):
        st, sl = self.state, self.state["sleeves"][sym]
        slip = self.cfg.paper_slippage_pct / 100
        fee_pct = self.cfg.fees.taker_pct / 100
        coin = sym.split("/")[0]
        profit = None
        if side == "buy":
            px = price * (1 + slip)
            fee = sl["cash"] * fee_pct
            qty = (sl["cash"] - fee) / px
            sl["cost"], sl["qty"], sl["cash"] = sl["cash"], qty, 0.0
            st["stats"]["buys"] += 1
        else:
            px = price * (1 - slip)
            qty = sl["qty"]
            gross = qty * px
            fee = gross * fee_pct
            sl["cash"], sl["qty"] = gross - fee, 0.0
            profit = sl["cash"] - sl["cost"]
            sl["cost"] = 0.0
            st["stats"]["sells"] += 1
            st["stats"]["round_trips"] += 1
            st["stats"]["grid_profit"] += profit
        st["stats"]["fees"] += fee
        st["report"]["fills"] += 1
        append_jsonl(self.cfg.trades_path, {
            "t": utcnow().isoformat(timespec="seconds"), "kind": f"trend {coin}", "side": side,
            "amount": qty, "price": px, "fee": fee, "profit": profit,
        })
        log.info("%s %.6f %s at %.2f (%s)", side.upper(), qty, coin, px, why)
        if self.cfg.alerts.email_on_every_fill:
            extra = f"\nProfit on this trade: ${profit:,.2f}" if profit is not None else ""
            self.notifier.send(f"{side.upper()} {coin} at ${px:,.2f}",
                               f"{why}.\n{qty:.6f} {coin}, fee ${fee:.2f}{extra}\n\n{self.summary()}")

    def _sell_everything(self, why):
        for sym, sl in self.state["sleeves"].items():
            if sl["qty"] > 0:
                self._trade(sym, "sell", self.state["prices"][sym], why)

    def _check_kill_switch(self, prices):
        st = self.state
        floor = st["start_equity"] * (1 - self.cfg.risk.max_drawdown_pct / 100)
        if st["status"] == "running" and self.equity(prices) < floor:
            self._sell_everything("kill switch")
            st["status"] = "halted"
            st["status_reason"] = f"equity fell below ${floor:,.2f}"
            self.notifier.send("Kill switch triggered", self.summary(), priority="urgent")

    def _refresh(self, prices):
        st = self.state
        st["balances"] = {"quote": round(self.equity(prices), 4), "base": 0.0}
        if st["status"] == "running":
            st["status_reason"] = self._holdings_text()

    def _holdings_text(self):
        held = [s.split("/")[0] for s, sl in self.state["sleeves"].items() if sl["qty"] > 0]
        cash = [s.split("/")[0] for s, sl in self.state["sleeves"].items() if sl["qty"] == 0]
        return f"holding {', '.join(held) or 'nothing'}; cash for {', '.join(cash) or 'nothing'}"

    # -------------------------------------------------------------- commands

    def _handle_command(self, prices):
        path = self.cfg.command_path
        if not path.exists():
            return
        command = path.read_text().strip().lower()
        path.unlink()
        log.info("received command: %s", command)
        st = self.state
        if command == "pause":
            self._sell_everything("paused by you")
            st["status"], st["status_reason"] = "paused", "paused by you (all cash)"
            self.notifier.send("Trend bot paused", self.summary())
        elif command == "resume":
            if st["status"] == "halted":
                st["start_equity"] = self.equity(prices)
            st["status"], st["status_reason"], st["last_signal_day"] = "running", "", None
            self.notifier.send("Trend bot resuming", "Re-checking trends now.")
        elif command == "reset":
            self._sell_everything("reset")
            final = self.equity(prices)
            self.state = new_trend_state(self.cfg)
            self.notifier.send("Trend bot reset", f"Previous run ended with ${final:,.2f}.")
        else:
            log.warning("unknown command %r (expected pause, resume or reset)", command)

    # --------------------------------------------------------------- reports

    def _record_history(self, prices):
        now = utcnow()
        if self.last_history and (now - self.last_history).total_seconds() < HISTORY_INTERVAL:
            return
        self.last_history = now
        append_jsonl(self.cfg.history_path, {
            "t": now.isoformat(timespec="seconds"), "price": self.state["last_price"],
            "equity": round(self.equity(prices), 4), "status": self.state["status"],
        })

    def _maybe_daily_report(self, prices):
        report = self.state["report"]
        now = utcnow()
        today = now.date().isoformat()
        if report["last_date"] == today or now.hour < self.cfg.alerts.daily_summary_hour_utc:
            return
        equity = self.equity(prices)
        if report["last_date"] is not None:
            self.notifier.send(f"Daily summary: ${equity:,.2f}", self.summary(report))
        self.state["report"] = {"last_date": today, "equity": equity, "fills": 0}

    def summary(self, report=None):
        st = self.state
        eq = self.equity()
        start = st["start_equity"]
        lines = [
            f"Mode: {st['mode'].upper()}   Strategy: trend ({self.cfg.trend.sma_days}-day average)",
            f"Status: {st['status']} {st['status_reason']}",
            f"Equity: ${eq:,.2f} ({(eq / start - 1) * 100:+.2f}% since start at ${start:,.2f})",
        ]
        if report:
            lines.append(f"Last 24h: {(eq / report['equity'] - 1) * 100:+.2f}%, {report['fills']} trades")
        for sym, sl in st["sleeves"].items():
            p = st["prices"].get(sym, 0)
            sig = st["signals"].get(sym)
            trend = (f"close ${sig['close']:,.2f} vs avg ${sig['sma']:,.2f} ({'up' if sig['up'] else 'down'})"
                     if sig else "not checked yet")
            held = f"{sl['qty']:.6f} (${sl['qty'] * p:,.2f})" if sl["qty"] > 0 else f"cash ${sl['cash']:,.2f}"
            lines.append(f"{sym}: {held}; {trend}")
        lines.append(f"Fees paid: ${st['stats']['fees']:.2f}   Kill switch at: "
                     f"${start * (1 - self.cfg.risk.max_drawdown_pct / 100):,.2f}")
        return "\n".join(lines)

    def _error_email(self, error):
        now = utcnow()
        if self.last_error_email and (now - self.last_error_email).total_seconds() < \
                self.cfg.alerts.error_email_cooldown_minutes * 60:
            return
        self.last_error_email = now
        self.notifier.send("Error", f"{type(error).__name__}: {error}\nThe bot keeps running and will retry.",
                           priority="high")
