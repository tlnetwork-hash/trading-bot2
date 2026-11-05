"""The stock bot's main loop: scan, buy, protect, sell, report."""

import logging
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.state import append_jsonl, load_state, save_state

from .alpaca import AlpacaError
from .news import NewsScanner, hot_list, parse_time
from .scanner import MarketScanner

log = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")
PDT_EQUITY = 25_000      # FINRA pattern-day-trader threshold
PDT_LIMIT = 3            # day trades allowed in 5 business days below it
NEWS_ALERT_MINUTES = 10


def new_state():
    return {
        "version": 1,
        "paused": False,
        "day": None,                # trading day (New York date) the counters below belong to
        "halted_today": "",         # reason new trades stopped for the rest of the day
        "traded_today": [],
        "positions": {},            # stocks the bot bought (it never touches anything else)
        "last_scan": None,
        "last_news": None,
        "seen_news": [],
        "summary_sent": None,
        "notices": {},
        "last_error_alert": None,
        "stats": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0},
        "today": {"trades": 0, "pnl": 0.0, "closed": []},
    }


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class StockBot:
    def __init__(self, cfg, client, notifier, state, now=None):
        self.cfg = cfg
        self.client = client
        self.notifier = notifier
        self.state = state
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.news = NewsScanner(client, cfg.news)
        self.scanner = MarketScanner(client, cfg.scanner, cfg.news)
        self._session = (None, None, None)  # (date, open_at, close_at)

    # --- loop -------------------------------------------------------------

    def run_forever(self, stop):
        log.info("stock bot running (%s mode, %s style, $%.0f capital)",
                 self.cfg.mode, self.cfg.trade.style, self.cfg.capital_usd)
        self.notifier.send("Stock bot started",
                           f"Mode: {self.cfg.mode.upper()}  Style: {self.cfg.trade.style}\n"
                           f"Capital: ${self.cfg.capital_usd:,.0f}  "
                           f"Max positions: {self.cfg.trade.max_positions}", "low")
        while not stop.is_set():
            try:
                self.step()
            except AlpacaError as e:
                log.error("%s", e)
                self._error_alert(str(e))
            except Exception as e:  # keep running; the next loop may well succeed
                log.exception("unexpected error")
                self._error_alert(f"{type(e).__name__}: {e}")
            save_state(self.cfg.state_path, self.state)
            stop.wait(self.cfg.poll_seconds)
        save_state(self.cfg.state_path, self.state)
        log.info("stopped")

    def step(self):
        now = self.now()
        self._handle_command()
        self._new_day(now)
        if self.cfg.news.enabled:
            self._news_alerts(now)

        clock = self.client.clock()
        positions = {p["symbol"]: p for p in self.client.positions()}
        self._record_closed(positions, now)
        if not clock.get("is_open"):
            self._daily_summary(now)
            return

        open_at, close_at = self.session(now)
        if close_at is None:
            return
        if self.cfg.trade.style == "day" and now >= close_at - timedelta(minutes=self.cfg.trade.flatten_minutes):
            self.flatten(positions, "end of day")
            return
        if self.state["paused"]:
            return
        account = self.client.account()
        if self._daily_loss_hit(account):
            self.flatten(positions, "daily loss limit")
            return
        entry_from = open_at + timedelta(minutes=self.cfg.trade.entry_delay_minutes)
        entry_until = close_at - timedelta(minutes=self.cfg.trade.entry_cutoff_minutes)
        if not entry_from <= now <= entry_until:
            return
        last = self.state["last_scan"]
        if last and now - parse_time(last) < timedelta(minutes=self.cfg.scanner.scan_minutes):
            return
        self.state["last_scan"] = iso(now)
        self.scan_and_trade(account, positions, now, open_at, close_at)

    # --- trading ------------------------------------------------------------

    def scan_and_trade(self, account, positions, now, open_at, close_at):
        st, t = self.state, self.cfg.trade
        mine = {s: p for s, p in positions.items() if s in st["positions"]}
        slots = t.max_positions - len(mine)
        trades_left = t.max_trades_per_day - st["today"]["trades"]
        pdt_left = self._pdt_room(account, now)
        if min(slots, trades_left, pdt_left) <= 0 or st["halted_today"]:
            log.info("no new trades: %d slot(s), %d trade(s) left today, %d day trade(s) allowed",
                     slots, trades_left, pdt_left)
            return

        news_map = self.news.scan(now=now) if self.cfg.news.enabled else {}
        candidates = self.scanner.scan(news_map, now, open_at, close_at)
        passed = [c for c in candidates if not c.rejected]
        log.info("scan: %d looked at, %d passed%s", len(candidates), len(passed),
                 (": " + ", ".join(c.symbol for c in passed[:8])) if passed else "")

        invested = sum(abs(float(p.get("market_value") or 0)) for p in mine.values())
        cash = min(self.cfg.capital_usd - invested, float(account.get("buying_power") or 0))
        budget = min(slots, trades_left, pdt_left)
        for c in passed:
            if budget <= 0:
                break
            if c.symbol in positions or c.symbol in st["traded_today"]:
                continue
            qty = self.position_size(c.price, cash)
            if qty < 1:
                log.info("%s: not enough cash for one share at $%.2f", c.symbol, c.price)
                continue
            if not self._tradable(c.symbol):
                continue
            if self.buy(c, qty, now):
                cash -= qty * c.price
                budget -= 1

    def position_size(self, price, cash):
        t = self.cfg.trade
        risk_dollars = self.cfg.capital_usd * t.risk_per_trade_pct / 100
        by_risk = risk_dollars / (price * t.stop_loss_pct / 100)
        by_size = self.cfg.capital_usd * t.max_position_pct / 100 / price
        by_cash = max(cash, 0) / price
        return math.floor(min(by_risk, by_size, by_cash))

    def _tradable(self, symbol):
        try:
            asset = self.client.asset(symbol)
        except AlpacaError as e:
            log.info("%s: cannot look up asset (%s)", symbol, e)
            return False
        if not asset.get("tradable") or asset.get("status") != "active":
            log.info("%s: not tradable on Alpaca", symbol)
            return False
        return True

    def buy(self, c, qty, now):
        t = self.cfg.trade
        stop = round(c.price * (1 - t.stop_loss_pct / 100), 2)
        target = round(c.price * (1 + t.take_profit_pct / 100), 2)
        tif = "day" if t.style == "day" else "gtc"
        try:
            order = self.client.bracket_buy(c.symbol, qty, stop, target, tif)
        except AlpacaError as e:
            log.warning("buy %s refused: %s", c.symbol, e)
            self.state["traded_today"].append(c.symbol)  # don't keep retrying it today
            return False
        st = self.state
        st["positions"][c.symbol] = {
            "qty": qty, "entry": c.price, "stop": stop, "target": target,
            "opened_at": iso(now), "day": st["day"], "order_id": order.get("id"),
            "reason": f"{c.gain_pct:+.1f}% today, volume {c.rel_volume:.1f}x",
            "headline": c.headline,
        }
        st["traded_today"].append(c.symbol)
        st["today"]["trades"] += 1
        append_jsonl(self.cfg.trades_path, {"time": iso(now), "side": "buy", "symbol": c.symbol,
                                            "qty": qty, "price": c.price, "stop": stop,
                                            "target": target, "headline": c.headline})
        body = (f"Bought {qty} {c.symbol} at about ${c.price:,.2f} (${qty * c.price:,.2f})\n"
                f"Why: {st['positions'][c.symbol]['reason']}, "
                f"{(c.price / c.vwap - 1) * 100 if c.vwap else 0:+.1f}% vs VWAP\n"
                f"Stop-loss ${stop:,.2f}  Take-profit ${target:,.2f}")
        if c.headline:
            body += f"\nNews ({c.news_score:+.1f}): {c.headline}"
        log.info("BUY %d %s @ ~%.2f stop %.2f target %.2f", qty, c.symbol, c.price, stop, target)
        self.notifier.send(f"Bought {c.symbol}", body)
        return True

    def flatten(self, positions, reason):
        """Sell everything the bot owns."""
        selling = self.state.setdefault("selling", {})
        mine = [s for s in self.state["positions"]
                if s in positions and selling.get(s) != self.state["day"]]
        if not mine:
            return
        for sym in mine:
            selling[sym] = self.state["day"]
            try:
                self.client.close_position(sym)
                log.info("selling %s (%s)", sym, reason)
            except AlpacaError as e:
                log.error("could not sell %s: %s", sym, e)
                self._error_alert(f"Could not sell {sym}: {e}")
        self.notifier.send(f"Selling {len(mine)} position(s): {reason}", ", ".join(mine), "high")

    # --- bookkeeping --------------------------------------------------------

    def _record_closed(self, positions, now):
        """Notice positions the bot owned that are gone (stop, target or our own sell)."""
        st = self.state
        for sym in [s for s in st["positions"] if s not in positions]:
            pos = st["positions"].pop(sym)
            st.setdefault("selling", {}).pop(sym, None)
            exit_price = self._exit_price(sym, pos)
            pnl = round((exit_price - pos["entry"]) * pos["qty"], 2) if exit_price else 0.0
            how = ("take-profit" if exit_price and exit_price >= pos["target"] * 0.995 else
                   "stop-loss" if exit_price and exit_price <= pos["stop"] * 1.005 else "sold")
            st["stats"]["trades"] += 1
            st["stats"]["wins" if pnl > 0 else "losses"] += 1
            st["stats"]["pnl"] = round(st["stats"]["pnl"] + pnl, 2)
            st["today"]["pnl"] = round(st["today"]["pnl"] + pnl, 2)
            st["today"]["closed"].append({"symbol": sym, "pnl": pnl, "how": how})
            append_jsonl(self.cfg.trades_path, {"time": iso(now), "side": "sell", "symbol": sym,
                                                "qty": pos["qty"], "price": exit_price,
                                                "pnl": pnl, "how": how})
            price = f"${exit_price:,.2f}" if exit_price else "unknown price"
            log.info("SOLD %s (%s) at %s, P&L %+.2f", sym, how, price, pnl)
            self.notifier.send(f"Sold {sym} ({how}): {'+' if pnl >= 0 else '-'}${abs(pnl):,.2f}",
                               f"Sold {pos['qty']} {sym} at {price} (bought at ${pos['entry']:,.2f})\n"
                               f"Profit/loss: ${pnl:+,.2f}\n"
                               f"Today: ${st['today']['pnl']:+,.2f}   All time: ${st['stats']['pnl']:+,.2f}")

    def _exit_price(self, sym, pos):
        try:
            orders = self.client.closed_orders(sym, after=pos.get("opened_at"))
        except AlpacaError as e:
            log.warning("could not look up %s sale: %s", sym, e)
            return None
        fills = []
        for o in orders:
            for leg in [o] + (o.get("legs") or []):
                if leg.get("side") == "sell" and leg.get("filled_avg_price") and leg.get("filled_at"):
                    fills.append(leg)
        if not fills:
            return None
        fills.sort(key=lambda o: o["filled_at"], reverse=True)
        return float(fills[0]["filled_avg_price"])

    def session(self, now):
        today = now.astimezone(NY).date().isoformat()
        if self._session[0] != today:
            day = self.client.calendar_day(today)
            if day:
                o = datetime.fromisoformat(f"{today}T{day['open']}").replace(tzinfo=NY)
                c = datetime.fromisoformat(f"{today}T{day['close']}").replace(tzinfo=NY)
                self._session = (today, o, c)
            else:
                self._session = (today, None, None)
        return self._session[1], self._session[2]

    def _new_day(self, now):
        today = now.astimezone(NY).date().isoformat()
        st = self.state
        if st["day"] != today:
            st["day"] = today
            st["halted_today"] = ""
            st["traded_today"] = []
            st["today"] = {"trades": 0, "pnl": 0.0, "closed": []}

    def _daily_loss_hit(self, account):
        st = self.state
        if st["halted_today"]:
            return False
        equity, last = float(account.get("equity") or 0), float(account.get("last_equity") or 0)
        loss = last - equity if last else -st["today"]["pnl"]
        limit = self.cfg.capital_usd * self.cfg.trade.daily_loss_limit_pct / 100
        if loss >= limit:
            st["halted_today"] = f"down ${loss:,.2f} today (limit ${limit:,.2f})"
            self.notifier.send("Daily loss limit hit, done for today",
                               f"The account is {st['halted_today']}. Selling the bot's stocks; "
                               "it will start again tomorrow.", "high")
            return True
        return False

    def _pdt_room(self, account, now):
        """How many more new trades are safe under the pattern-day-trader rule."""
        if not self.cfg.trade.respect_pdt:
            return 99
        equity = float(account.get("equity") or 0)
        if account.get("pattern_day_trader") and equity >= PDT_EQUITY:
            return 99
        if equity >= PDT_EQUITY:
            return 99
        used = int(account.get("daytrade_count") or 0)
        # Each position opened today could become a day trade when it's sold today.
        open_today = sum(1 for p in self.state["positions"].values() if p.get("day") == self.state["day"])
        room = PDT_LIMIT - used - open_today
        if room <= 0:
            self._notice_once("pdt", "Day-trade limit reached",
                              f"Accounts under ${PDT_EQUITY:,} may make only {PDT_LIMIT} day trades "
                              f"in 5 business days. Used: {used}. No new trades until one drops off.")
        return max(room, 0)

    def _notice_once(self, key, subject, body):
        if self.state["notices"].get(key) != self.state["day"]:
            self.state["notices"][key] = self.state["day"]
            log.info("%s: %s", subject, body)
            self.notifier.send(subject, body, "low")

    def _error_alert(self, text):
        last = self.state.get("last_error_alert")
        now = self.now()
        if last and now - parse_time(last) < timedelta(minutes=30):
            return
        self.state["last_error_alert"] = iso(now)
        self.notifier.send("Stock bot error", text, "high")

    # --- news alerts -------------------------------------------------------

    def _news_alerts(self, now):
        """Every 10 minutes, alert about strong new headlines (even before the open)."""
        st = self.state
        if st["last_news"] and now - parse_time(st["last_news"]) < timedelta(minutes=NEWS_ALERT_MINUTES):
            return
        st["last_news"] = iso(now)
        news_map = self.news.scan(now=now)
        seen = set(st["seen_news"])
        fresh = []
        for n in hot_list(news_map, self.cfg.news.alert_min_score, limit=20):
            best = n.best
            if best and best.score >= self.cfg.news.alert_min_score and best.url not in seen \
                    and now - best.created_at <= timedelta(hours=2):
                fresh.append((n, best))
                seen.add(best.url)
        st["seen_news"] = list(seen)[-500:]
        if fresh:
            lines = [f"{n.symbol} ({n.score:+.1f}): {h.headline}" for n, h in fresh[:8]]
            self.notifier.send(f"News watch: {', '.join(n.symbol for n, _ in fresh[:5])}",
                               "Stocks with fresh catalyst news:\n" + "\n".join(lines), "low")

    def _daily_summary(self, now):
        st = self.state
        local = now.astimezone(NY)
        if st["summary_sent"] == st["day"] or local.hour < 16 or local.weekday() >= 5:
            return
        st["summary_sent"] = st["day"]
        if not st["today"]["trades"] and not st["today"]["closed"]:
            return
        self.notifier.send(f"Stock day summary: ${st['today']['pnl']:+,.2f}", self.summary())

    def summary(self):
        st = self.state
        s, today = st["stats"], st["today"]
        lines = [f"Mode: {self.cfg.mode.upper()}  Style: {self.cfg.trade.style}  "
                 f"{'PAUSED' if st['paused'] else 'running'}",
                 f"Today ({st['day']}): {today['trades']} buy(s), P&L ${today['pnl']:+,.2f}"]
        if st["halted_today"]:
            lines.append(f"Stopped for today: {st['halted_today']}")
        for c in today["closed"]:
            lines.append(f"  {c['symbol']}: ${c['pnl']:+,.2f} ({c['how']})")
        if st["positions"]:
            lines.append("Holding:")
            for sym, p in st["positions"].items():
                lines.append(f"  {sym}: {p['qty']} @ ${p['entry']:,.2f}  "
                             f"stop ${p['stop']:,.2f}  target ${p['target']:,.2f}")
        win_rate = f"{s['wins'] / s['trades'] * 100:.0f}%" if s["trades"] else "-"
        lines.append(f"All time: {s['trades']} trades, {win_rate} winners, P&L ${s['pnl']:+,.2f}")
        return "\n".join(lines)

    # --- commands -----------------------------------------------------------

    def _handle_command(self):
        path = self.cfg.command_path
        if not path.exists():
            return
        command = path.read_text().strip().lower()
        path.unlink()
        if command == "pause":
            self.state["paused"] = True
            self.notifier.send("Paused", "No new trades. Open positions keep their stop-loss "
                               "and take-profit.")
        elif command == "resume":
            self.state["paused"] = False
            self.notifier.send("Resumed", "Looking for trades again.")
        elif command == "sell-all":
            self.state["paused"] = True
            self.flatten({p["symbol"]: p for p in self.client.positions()}, "you asked (sell-all)")
        else:
            log.warning("unknown command %r", command)


def load_or_new(path):
    state = load_state(path)
    if state is None:
        return new_state()
    for key, value in new_state().items():
        state.setdefault(key, value)
    return state
