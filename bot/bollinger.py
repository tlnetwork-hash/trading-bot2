"""Bollinger Bands mean-reversion bot (strategy: bollinger). Paper or live.

Added 2026-10-29 for the paper SOL bot (config-solgrid.yaml). Rules:
  * Bands from closed 1h candles: middle = 20-candle average, upper/lower =
    middle +/- 2 standard deviations. Recalculated on every new candle.
  * Buy only after a candle touches/dips below the lower band AND the next
    candle closes back above it (bounce confirmed).
  * Each buy sells at the middle band or +1%, whichever is higher. If the
    price reaches the upper band, everything still held is sold.
  * Trend filter: price below the 50-period 4h average and that average
    sloping down -> half-size buys. Back to full size when it flattens/turns up.
  * Squeeze (very narrow bands) -> smaller buys. Never pauses on breakouts.
  * Never more than 60% of the bot's money in the coin (40% always cash).
    Buys in a row without a sell each need a lower price than the last
    (0.5%, then 1%, then 1.5% ... below it); resets after a sell.
  * Only the drawdown kill switch stops it. No stop-loss sales.

Own files (state-bb-paper.json, trades-bb-paper.jsonl, ...) so the grid's
state and logs are kept untouched for comparison. Switch back to the grid by
setting strategy: grid in the config and restarting.
"""

import json
import logging
import statistics
import threading

import ccxt

from .exchange import LiveExchange, MarketData, make_client
from .remote import HELP, RemoteCommands
from .state import append_jsonl, save_state, utcnow

log = logging.getLogger(__name__)

TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
HISTORY_INTERVAL = 300
NETWORK_ALERT_AFTER = 20
CANDLE_REFRESH_SECONDS = 60
TREND_REFRESH_SECONDS = 300
SIGNAL_MAX_AGE_MS = 20 * 60_000   # don't act on a bounce whose candle closed long ago (e.g. after downtime)
SERIES_POINTS = 24 * 30           # 30 days of hourly bands for the dashboard chart


def new_bb_state(cfg):
    c = cfg.capital_usd
    return {
        "version": 1,
        "strategy": "bollinger",
        "mode": cfg.mode,
        "symbol": cfg.symbol,
        "status": "running",
        "status_reason": "",
        "created_at": utcnow().isoformat(timespec="seconds"),
        "start_equity": c,
        "balances": {"quote": c, "base": 0.0},
        "grid": None,
        "orders": {},            # display only: each open buy's sell target
        "lots": [],              # open buys waiting for their sell
        "streak": {"count": 0, "last_buy": None},
        "last_candle": None,     # open time (ms) of the last closed 1h candle processed
        "bands": None,
        "trend": None,
        "stats": {"buys": 0, "sells": 0, "round_trips": 0, "grid_profit": 0.0, "fees": 0.0,
                  "skipped_signals": 0},
        "report": {"last_date": None, "equity": c, "fills": 0},
        "last_price": None,
        "seq": 0,
    }


def band_series(closes, period, k):
    out = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        w = closes[i - period + 1:i + 1]
        m = sum(w) / period
        sd = statistics.pstdev(w)
        out[i] = (m + k * sd, m, m - k * sd)
    return out


def sma_series(closes, n):
    out = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        out[i] = sum(closes[i - n + 1:i + 1]) / n
    return out


def closed_only(rows, tf_ms, now_ms):
    return [r for r in rows if r[0] + tf_ms <= now_ms]


class BollingerBot:
    def __init__(self, cfg, notifier, state):
        self.cfg = cfg
        self.b = cfg.bollinger
        self.notifier = notifier
        self.state = state
        self.live = cfg.mode == "live"
        if self.live:   # real orders on the exchange
            self.market = LiveExchange(cfg.symbol, cfg.api_key, cfg.api_secret, exchange=cfg.exchange)
        else:           # trading price + order rules only; fills are simulated
            self.market = MarketData(cfg.symbol, exchange=cfg.exchange)
        self.remote = None
        if cfg.ntfy_command_url:
            self.remote = RemoteCommands(cfg.ntfy_command_url, cfg.ntfy_command_pin, cfg.exchange, state)
        self.ind_exchange = self.b.indicator_exchange or cfg.chart_source
        self.ind = make_client(self.ind_exchange)                         # candles for the indicators
        self.c1 = None
        self.c4 = None
        self.c1_at = self.c4_at = 0.0
        self.last_history = None
        self.network_failures = 0
        self.last_error_email = None

    # ------------------------------------------------------------------ loop

    def run_forever(self, stop_event=None):
        stop_event = stop_event or threading.Event()
        log.info("bollinger bot running (indicators from %s, trading price from %s)",
                 self.ind_exchange, self.cfg.exchange)
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
        price = self.market.price()
        self.state["last_price"] = price
        self._handle_command(price)
        self._handle_phone_commands(price)
        st = self.state
        new_candle = self._refresh_indicators(price)
        if st["status"] == "running":
            if new_candle:
                bought_before = self.state["stats"]["buys"]
                self._check_bounce(price)
                if self.b.pullback_buys and self.state["stats"]["buys"] == bought_before:
                    self._check_pullback(price)
                if self.b.top_buys and self.state["stats"]["buys"] == bought_before:
                    self._check_pullback(price, band=0, kind="upper-band pullback")
            self._check_sells(price)
            self._check_kill_switch(price)
        self._refresh_display(price)
        self._maybe_daily_report(price)
        self._record_history(price)
        self.save()

    def save(self):
        save_state(self.cfg.state_path, self.state)

    def equity(self, price=None):
        price = price or self.state.get("last_price") or 0
        b = self.state["balances"]
        return b["quote"] + b["base"] * price

    # ------------------------------------------------------------ indicators

    def _refresh_indicators(self, price):
        """Fetch candles when due and recompute the bands. True if a new 1h candle closed."""
        now = utcnow().timestamp()
        now_ms = int(now * 1000)
        b = self.b
        tf1 = TF_MS[b.timeframe]
        need = SERIES_POINTS + b.period + 5
        if self.c1 is None or now - self.c1_at >= CANDLE_REFRESH_SECONDS or \
                (self.c1 and now_ms >= self.c1[-1][0] + 2 * tf1):
            rows = self.ind.fetch_ohlcv(self.cfg.symbol, b.timeframe, limit=min(need, 720))
            self.c1, self.c1_at = closed_only(rows, tf1, now_ms), now
        if self.c4 is None or now - self.c4_at >= TREND_REFRESH_SECONDS:
            tf4 = TF_MS[b.trend_timeframe]
            rows = self.ind.fetch_ohlcv(self.cfg.symbol, b.trend_timeframe,
                                        limit=b.trend_period + b.trend_slope_candles + 200)
            self.c4, self.c4_at = closed_only(rows, tf4, now_ms), now

        c1 = self.c1
        if len(c1) < b.period + 2:
            raise RuntimeError(f"only {len(c1)} closed {b.timeframe} candles; need {b.period + 2}")
        closes = [float(r[4]) for r in c1]
        self.bands = band_series(closes, b.period, b.std_dev)
        upper, mid, lower = self.bands[-1]
        widths = [(u - l) / m * 100 for (u, m, l) in (x for x in self.bands if x)]
        recent = sorted(widths[-b.squeeze_lookback:])
        threshold = recent[min(len(recent) - 1, int(len(recent) * b.squeeze_percentile / 100))]
        width = widths[-1]
        self.state["bands"] = {
            "candle": c1[-1][0], "upper": round(upper, 4), "middle": round(mid, 4), "lower": round(lower, 4),
            "width_pct": round(width, 3), "squeeze": width <= threshold, "squeeze_below_pct": round(threshold, 3),
        }
        self.state["trend"] = self._trend(price)
        self._write_series()

        last = c1[-1][0]
        new = self.state["last_candle"] != last
        self.state["last_candle"] = last
        return new

    def _trend(self, price):
        b = self.b
        closes = [float(r[4]) for r in self.c4]
        sma = sma_series(closes, b.trend_period)
        self.sma4 = sma
        if len(closes) < b.trend_period + b.trend_slope_candles:
            return {"sma": None, "slope_pct": None, "down": False, "label": "not enough data (full size)"}
        now, before = sma[-1], sma[-1 - b.trend_slope_candles]
        slope = (now / before - 1) * 100
        below = price < now
        sloping_down = slope < -b.trend_flat_pct
        down = below and sloping_down
        shape = "down" if sloping_down else ("up" if slope > b.trend_flat_pct else "flat")
        label = (f"DOWNTREND: below the {b.trend_period}x{b.trend_timeframe} average and it's sloping down (half size)"
                 if down else
                 f"{'below' if below else 'above'} the {b.trend_period}x{b.trend_timeframe} average, sloping {shape} (full size)")
        return {"sma": round(now, 4), "slope_pct": round(slope, 3), "below": below, "down": down, "label": label}

    def _write_series(self):
        tf1 = TF_MS[self.b.timeframe]
        tf4 = TF_MS[self.b.trend_timeframe]
        bands = [[r[0] + tf1, round(x[0], 3), round(x[1], 3), round(x[2], 3)]
                 for r, x in zip(self.c1, self.bands) if x][-SERIES_POINTS:]
        sma = [[r[0] + tf4, round(v, 3)] for r, v in zip(self.c4, self.sma4) if v is not None][-200:]
        out = {"updated": utcnow().isoformat(timespec="seconds"), "bands": bands, "sma": sma,
               "now": self.state["bands"], "trend": self.state["trend"],
               "settings": {"period": self.b.period, "std_dev": self.b.std_dev,
                            "trend_period": self.b.trend_period, "trend_timeframe": self.b.trend_timeframe,
                            "max_coin_pct": self.b.max_coin_pct,
                            "pullback_buys": self.b.pullback_buys,
                            "top_buys": self.b.top_buys,
                            "top_profit_pct": self.b.top_profit_pct}}
        path = self.cfg.data_dir / "bb-series.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(out))
        tmp.replace(path)

    # --------------------------------------------------------------- trading

    def _size_mult(self):
        mult, why = 1.0, []
        if (self.state.get("trend") or {}).get("down"):
            mult *= self.b.downtrend_size
            why.append("downtrend")
        if (self.state.get("bands") or {}).get("squeeze"):
            mult *= self.b.squeeze_size
            why.append("squeeze")
        return mult, why

    def _next_buy_limit(self):
        s = self.state["streak"]
        if not s["count"] or not s["last_buy"]:
            return None
        return s["last_buy"] * (1 - self.b.step_down_pct * s["count"] / 100)

    def _check_bounce(self, price):
        c1, bands = self.c1, self.bands
        touch, conf = c1[-2], c1[-1]
        b_touch, b_conf = bands[-2], bands[-1]
        if not (b_touch and b_conf):
            return
        touched = float(touch[3]) <= b_touch[2]
        confirmed = float(conf[4]) > b_conf[2]
        if not (touched and confirmed):
            return
        age = utcnow().timestamp() * 1000 - (conf[0] + TF_MS[self.b.timeframe])
        if age > SIGNAL_MAX_AGE_MS:
            log.info("bounce signal is %.0f min old (bot was down?), skipping", age / 60000)
            return
        log.info("bounce confirmed: low %.2f <= lower %.2f, then close %.2f > lower %.2f",
                 float(touch[3]), b_touch[2], float(conf[4]), b_conf[2])
        self._try_buy(price, touch_low=float(touch[3]), confirm_close=float(conf[4]))

    def _check_pullback(self, price, band=1, kind="middle-band pullback"):
        """Oct 30: uptrend pullback. The candle before closed above the band (1 = middle,
        0 = upper), the candle that just closed dipped to/below it and still closed above it."""
        c1, bands = self.c1, self.bands
        prev, conf = c1[-2], c1[-1]
        b_prev, b_conf = bands[-2], bands[-1]
        if not (b_prev and b_conf):
            return False
        mid = b_conf[band]
        ok = float(prev[4]) > b_prev[band] and float(conf[3]) <= mid < float(conf[4])
        if not ok:
            return False
        age = utcnow().timestamp() * 1000 - (conf[0] + TF_MS[self.b.timeframe])
        if age > SIGNAL_MAX_AGE_MS:
            log.info("pullback signal is %.0f min old (bot was down?), skipping", age / 60000)
            return False
        log.info("%s: low %.2f <= band %.2f, close %.2f above it", kind, float(conf[3]), mid, float(conf[4]))
        self._try_buy(price, kind=kind, touch_low=float(conf[3]), confirm_close=float(conf[4]))
        return True

    def _skip(self, price, why, **extra):
        self.state["stats"]["skipped_signals"] += 1
        log.info("bounce signal skipped: %s", why)
        append_jsonl(self.cfg.data_dir / f"bb-signals-{self.cfg.mode}.jsonl",
                     {"t": utcnow().isoformat(timespec="seconds"), "action": "skipped", "why": why,
                      "price": price, **self._context(price), **extra})

    def _try_buy(self, price, kind="lower-band bounce", **signal):
        st, b = self.state, self.b
        signal["signal"] = kind
        limit = self._next_buy_limit()
        if limit is not None and price > limit:
            return self._skip(price, f"buy #{st['streak']['count'] + 1} in a row needs ${limit:,.2f} or lower "
                                     f"({b.step_down_pct * st['streak']['count']:.1f}% under the last buy)", **signal)
        mult, why = self._size_mult()
        want = self.cfg.capital_usd * b.buy_size_pct / 100 * mult
        eq = self.equity(price)
        room = eq * b.max_coin_pct / 100 - st["balances"]["base"] * price
        fee_pct = self.cfg.fees.taker_pct / 100
        usd = min(want, room, st["balances"]["quote"] / (1 + fee_pct))
        if usd < self._min_usd():
            return self._skip(price, f"at the {b.max_coin_pct:.0f}% coin cap (room ${max(room, 0):,.2f}); "
                                     "waiting for a sell to free up room", **signal)
        size = "full size" if mult == 1 else f"{mult:g}x size ({' + '.join(why)})"
        lot = self._buy(usd, price, f"{kind}, {size}", mult, **signal)
        if lot is None:
            return self._skip(price, "order too small or not enough cash on the exchange", **signal)
        st["streak"] = {"count": st["streak"]["count"] + 1, "last_buy": lot["price"]}
        if kind == "upper-band pullback":
            lot["top"] = True       # sells at +min_profit_pct, not at the upper band
            self.save()

    def _min_usd(self):
        return max(self.market.min_cost or 0, 1.0)

    def _fill(self, side, qty, price):
        """Buy/sell qty coins at market. Returns (coins added/removed, cash moved, avg price, fee $)
        or None if nothing filled. Paper simulates; live sends a real order."""
        if not self.live:
            px = price * (1 + (1 if side == "buy" else -1) * self.cfg.paper_slippage_pct / 100)
            fee = qty * px * self.cfg.fees.taker_pct / 100
            return qty, qty * px + (fee if side == "buy" else -fee), px, fee
        info = self.market.market_order(side, qty, price)
        if info.filled <= 0:
            log.error("%s order did not fill (status %s)", side, info.status)
            return None
        filled, px = info.filled, info.average
        if info.fee_currency == self.market.base:
            fee_usd, coin_fee, cash_fee = info.fee_cost * px, info.fee_cost, 0.0
        elif info.fee_currency == self.market.quote:
            fee_usd, coin_fee, cash_fee = info.fee_cost, 0.0, info.fee_cost
        else:   # paid in another coin (e.g. BNB): doesn't touch the bot's SOL/USD
            fee_usd, coin_fee, cash_fee = filled * px * self.cfg.fees.taker_pct / 100, 0.0, 0.0
        if side == "buy":
            return filled - coin_fee, filled * px + cash_fee, px, fee_usd
        return filled + coin_fee, filled * px - cash_fee, px, fee_usd

    def _buy(self, usd, price, why, mult=1.0, **signal):
        st = self.state
        usd = min(usd, st["balances"]["quote"] / (1 + self.cfg.fees.taker_pct / 100 + 0.005))
        if self.live:
            _, free_usd = self.market.free_balance()
            usd = min(usd, free_usd / 1.01)          # never more than is really there
        qty = self.market.round_amount(usd / price)
        if usd < self._min_usd() or qty <= 0 or qty < (self.market.min_amount or 0):
            return None
        res = self._fill("buy", qty, price)
        if res is None:
            return None
        coins, cash, px, fee = res
        st["balances"]["quote"] -= cash
        st["balances"]["base"] += coins
        st["seq"] += 1
        lot = {"id": st["seq"], "qty": coins, "price": px, "cost": cash,
               "t": utcnow().isoformat(timespec="seconds"), "size_mult": mult}
        st["lots"].append(lot)
        st["stats"]["buys"] += 1
        st["stats"]["fees"] += fee
        st["report"]["fills"] += 1
        self._log_trade("buy", lot, px, fee, None, why, **signal)
        self.save()
        return lot

    def _check_sells(self, price):
        st = self.state
        bands = st.get("bands")
        if not st["lots"] or not bands:
            return
        if price >= bands["upper"]:
            for lot in list(st["lots"]):
                if not lot.get("top"):
                    self._sell(lot, price, f"price reached the upper band ${bands['upper']:,.2f}")
        for lot in list(st["lots"]):
            target = self._target(lot)
            if price >= target:
                which = (f"top buy +{self.b.top_profit_pct:g}%" if lot.get("top") else
                         "middle band" if target == bands["middle"] else f"+{self.b.min_profit_pct:g}% minimum")
                self._sell(lot, price, f"reached its target ${target:,.2f} ({which})")

    def _target(self, lot):
        if lot.get("top"):
            return lot["price"] * (1 + self.b.top_profit_pct / 100)
        return max(self.state["bands"]["middle"], lot["price"] * (1 + self.b.min_profit_pct / 100))

    def _sell(self, lot, price, why):
        st = self.state
        want = min(lot["qty"], st["balances"]["base"])
        if self.live:
            free_coin, _ = self.market.free_balance()
            want = min(want, free_coin)
        qty = self.market.round_amount(want)
        if qty <= 0 or qty < (self.market.min_amount or 0) or qty * price < self._min_usd():
            log.warning("lot %s (%.6f) is below the exchange minimum; dropping it from the list "
                        "(the coins stay in the balance)", lot["id"], lot["qty"])
            st["lots"].remove(lot)
            return
        res = self._fill("sell", qty, price)
        if res is None:
            return
        coins, cash, px, fee = res
        profit = cash - lot["cost"] * min(1.0, qty / lot["qty"])
        st["balances"]["quote"] += cash
        st["balances"]["base"] = max(0.0, st["balances"]["base"] - coins)
        st["lots"].remove(lot)
        st["streak"] = {"count": 0, "last_buy": None}   # spacing resets after a sell
        s = st["stats"]
        s["sells"] += 1
        s["round_trips"] += 1
        s["grid_profit"] += profit
        s["fees"] += fee
        st["report"]["fills"] += 1
        self._log_trade("sell", dict(lot, qty=qty), px, fee, profit, why)
        self.save()

    def _context(self, price):
        st = self.state
        eq = self.equity(price)
        bands = st.get("bands") or {}
        trend = st.get("trend") or {}
        return {
            "bands": {k: bands.get(k) for k in ("upper", "middle", "lower", "width_pct", "squeeze")},
            "trend": {"sma50_4h": trend.get("sma"), "slope_pct_per_day": trend.get("slope_pct"),
                      "below_avg": trend.get("below"), "downtrend": trend.get("down")},
            "coin_pct": round(st["balances"]["base"] * price / eq * 100, 2) if eq else 0,
            "equity": round(eq, 4),
        }

    def _log_trade(self, side, lot, px, fee, profit, why, **signal):
        rec = {"t": utcnow().isoformat(timespec="seconds"), "kind": "bollinger", "side": side,
               "amount": lot["qty"], "price": px, "fee": fee, "profit": profit, "lot": lot["id"],
               "reason": why, "size_mult": lot.get("size_mult"), **self._context(px), **signal}
        append_jsonl(self.cfg.trades_path, rec)
        extra = f", profit ${profit:,.2f}" if profit is not None else ""
        log.info("%s %.4f SOL @ $%.2f (%s)%s | bands %s/%s/%s | trend %s | coin %.1f%% | equity $%.2f",
                 side.upper(), lot["qty"], px, why, extra, rec["bands"]["lower"], rec["bands"]["middle"],
                 rec["bands"]["upper"], "DOWN" if rec["trend"]["downtrend"] else "ok", rec["coin_pct"],
                 rec["equity"])
        # No per-trade alerts (Todd, Oct 28): daily summary only.

    def _check_kill_switch(self, price):
        st = self.state
        floor = st["start_equity"] * (1 - self.cfg.risk.max_drawdown_pct / 100)
        if self.equity(price) < floor:
            sells = self.cfg.risk.kill_switch_sells
            if sells:
                for lot in list(st["lots"]):
                    self._sell(lot, price, "kill switch")
            st["status"] = "halted"
            st["status_reason"] = (f"kill switch: equity fell below ${floor:,.2f} "
                                   + ("(sold to cash)" if sells else "(coins kept, nothing sold)")
                                   + "; send resume to start again")
            log.warning(st["status_reason"])
            self.notifier.send("Kill switch triggered", self.summary(price), priority="urgent")

    # --------------------------------------------------------------- display

    def _refresh_display(self, price):
        st = self.state
        st["orders"] = {str(i): {"side": "sell", "price": round(self._target(lot), 4), "amount": lot["qty"]}
                        for i, lot in enumerate(st["lots"])} if st.get("bands") else {}
        if st["status"] == "running":
            b = st.get("bands") or {}
            eq = self.equity(price)
            coin = st["balances"]["base"] * price / eq * 100 if eq else 0
            limit = self._next_buy_limit()
            mult, _ = self._size_mult()
            st["status_reason"] = (
                f"bands ${b.get('lower', 0):,.2f} / ${b.get('middle', 0):,.2f} / ${b.get('upper', 0):,.2f}"
                f"{' (squeeze)' if b.get('squeeze') else ''}; "
                f"{'downtrend' if (st.get('trend') or {}).get('down') else 'trend ok'}; "
                f"buy size {mult:g}x; SOL {coin:.0f}% of money (cap {self.b.max_coin_pct:g}%); "
                f"{len(st['lots'])} open buy(s)"
                + (f"; next buy needs <= ${limit:,.2f}" if limit else ""))

    # -------------------------------------------------------------- commands

    def _handle_phone_commands(self, price):
        if self.remote is None:
            return
        for parsed in self.remote.poll():
            if parsed.error or not parsed.for_me:
                continue
            log.info("phone command: %s", parsed.command)
            if parsed.command == "help":
                self.notifier.send("Phone commands", HELP)
            elif parsed.command == "status":
                self.notifier.send(f"Status: ${self.equity(price):,.2f}", self.summary(price))
            else:
                self._run_command(parsed.command, price)
                self.notifier.send(f"Done: {parsed.command}", self.summary(price))
        self.state["remote"] = self.remote.state

    def _manual(self, command, price):
        """buy <$|all> / sell <$|all>. Doesn't pause the bot; it keeps trading."""
        side, _, arg = command.partition(" ")
        arg = arg.strip().lstrip("$")
        st = self.state
        if side == "buy":
            usd = st["balances"]["quote"] if arg == "all" else float(arg)
            if self._buy(usd, price, "manual buy (you)") is None:
                log.warning("manual buy too small or not enough cash")
            return
        left = None if arg == "all" else float(arg)
        for lot in sorted(list(st["lots"]), key=lambda l: l["price"]):   # cheapest first
            if left is not None and left <= 0:
                break
            if left is not None and lot["qty"] * price > left:
                part = dict(lot, qty=left / price, cost=lot["cost"] * left / (lot["qty"] * price))
                lot["qty"] -= part["qty"]
                lot["cost"] -= part["cost"]
                st["lots"].append(part)
                lot = part
            self._sell(lot, price, "manual sell (you)")
            if left is not None:
                left -= lot["qty"] * price

    def _handle_command(self, price):
        path = self.cfg.command_path
        if not path.exists():
            return
        command = path.read_text().strip().lower()
        path.unlink()
        log.info("received command: %s", command)
        self._run_command(command, price)

    def _run_command(self, command, price):
        st = self.state
        if command.split()[0] in ("buy", "sell"):
            return self._manual(command, price)
        if command == "pause":
            st["status"], st["status_reason"] = "paused", "paused by you (holding, nothing sold)"
        elif command == "resume":
            if st["status"] == "halted":
                st["start_equity"] = self.equity(price)
            st["status"], st["status_reason"] = "running", ""
        elif command == "reset":
            for lot in list(st["lots"]):
                self._sell(lot, price, "reset by you")
            final = self.equity(price)
            self.state = new_bb_state(self.cfg)
            log.info("reset: previous run ended with $%.2f", final)
        else:
            log.warning("command %r isn't supported in bollinger mode (pause, resume, reset, buy, sell)", command)

    # --------------------------------------------------------------- reports

    def _record_history(self, price):
        now = utcnow()
        if self.last_history and (now - self.last_history).total_seconds() < HISTORY_INTERVAL:
            return
        self.last_history = now
        append_jsonl(self.cfg.history_path, {
            "t": now.isoformat(timespec="seconds"), "price": price,
            "equity": round(self.equity(price), 4), "status": self.state["status"],
        })

    def _maybe_daily_report(self, price):
        report = self.state["report"]
        now = utcnow()
        today = now.date().isoformat()
        if report["last_date"] == today or now.hour < self.cfg.alerts.daily_summary_hour_utc:
            return
        equity = self.equity(price)
        if report["last_date"] is not None:
            self.notifier.send(f"Daily summary: ${equity:,.2f}", self.summary(price, report))
        self.state["report"] = {"last_date": today, "equity": equity, "fills": 0}

    def summary(self, price=None, report=None):
        st = self.state
        price = price or st.get("last_price") or 0
        eq = self.equity(price)
        start = st["start_equity"]
        s = st["stats"]
        b = st.get("bands") or {}
        t = st.get("trend") or {}
        lines = [
            f"Mode: {st['mode'].upper()}   Strategy: Bollinger Bands ({self.b.period}, {self.b.std_dev:g}) "
            f"on {self.b.timeframe}",
            f"Status: {st['status']} {st['status_reason']}",
            f"{self.cfg.symbol} price: ${price:,.2f}",
            f"Equity: ${eq:,.2f} ({(eq / start - 1) * 100:+.2f}% since start at ${start:,.2f})",
            f"Holding: ${st['balances']['quote']:,.2f} + {st['balances']['base']:.4f} SOL "
            f"in {len(st['lots'])} open buy(s)",
        ]
        if report:
            lines.append(f"Last 24h: {(eq / report['equity'] - 1) * 100:+.2f}%, {report['fills']} trades")
        if b:
            lines.append(f"Bands: lower ${b['lower']:,.2f}  middle ${b['middle']:,.2f}  upper ${b['upper']:,.2f}  "
                         f"(width {b['width_pct']:.2f}%{', SQUEEZE' if b.get('squeeze') else ''})")
        if t:
            lines.append(f"Trend: {t.get('label')}")
        lines.append(f"Trades: {s['buys']} buys, {s['sells']} sells, profit ${s['grid_profit']:,.2f}, "
                     f"skipped signals {s.get('skipped_signals', 0)}")
        lines.append(f"Fees paid: ${s['fees']:.2f}   Kill switch at: "
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
