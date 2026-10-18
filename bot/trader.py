"""The grid trading loop: places orders, reacts to fills, and enforces safety rules."""

import logging
import threading

import ccxt

from .grid import GridError, average_true_range, plan_grid, range_pct_from_atr
from .state import append_jsonl, new_state, save_state, utcnow

log = logging.getLogger(__name__)

NETWORK_ALERT_AFTER = 20  # consecutive failed ticks (~10 min at 30s) before emailing
HISTORY_INTERVAL = 300   # seconds between equity points saved for the dashboard chart


class BotError(Exception):
    pass


class GridBot:
    def __init__(self, cfg, exchange, notifier, state):
        self.cfg = cfg
        self.ex = exchange
        self.notifier = notifier
        self.state = state
        self.network_failures = 0
        self.last_error_email = None
        self.last_history = None
        if state["symbol"] != cfg.symbol:
            raise BotError(
                f"saved state is for {state['symbol']} but config.yaml says {cfg.symbol}. "
                f"Run 'reset' with the old symbol first, or delete {cfg.state_path}."
            )

    # ------------------------------------------------------------------ loop

    def run_forever(self, stop_event=None):
        stop_event = stop_event or threading.Event()
        st = self.state
        self.notifier.send(
            "Bot started",
            f"Mode: {self.cfg.mode.upper()}\nPair: {self.cfg.symbol}\n"
            f"Status: {st['status']} {st['status_reason']}\n"
            f"Tracked balances: ${st['balances']['quote']:.2f} + "
            f"{st['balances']['base']:.6f} {self.ex.base}",
        )
        while not stop_event.is_set():
            try:
                self.tick()
                self.network_failures = 0
            except ccxt.NetworkError as e:
                self.network_failures += 1
                log.warning("network problem talking to %s (%d in a row): %s",
                            self.cfg.exchange_name, self.network_failures, e)
                if self.network_failures == NETWORK_ALERT_AFTER:
                    self.notifier.send(f"Cannot reach {self.cfg.exchange_name}", f"{NETWORK_ALERT_AFTER} failed attempts in a row.\nLast error: {e}",
                                         priority="high")
            except Exception as e:
                log.exception("error during tick")
                self._error_email(e)
            stop_event.wait(self.cfg.poll_seconds)
        log.info("bot stopped")

    def tick(self):
        st = self.state
        price = self.ex.price()
        st["last_price"] = price
        self.ex.simulate(price)

        self._check_orders()
        self._handle_command(price)
        st = self.state  # "reset" replaces the state object

        if st["status"] == "running" and st["grid"] is None:
            self._build_grid(price)
        if st["status"] != "halted" and self._check_kill_switch(price):
            return
        if st["status"] == "running" and st["grid"]:
            self._check_breakout(price)
        self._maybe_daily_report(price)
        self._record_history(price)
        self.save()

    def save(self):
        save_state(self.cfg.state_path, self.state)

    # -------------------------------------------------------------- accounting

    def equity(self, price):
        b = self.state["balances"]
        return b["quote"] + b["base"] * price

    def _apply_fill(self, side, info):
        """Update tracked balances for a fill. Returns (amount, price, fee in USD)."""
        b = self.state["balances"]
        amount, price = info.filled, info.average
        if side == "buy":
            b["base"] += amount
            b["quote"] -= amount * price
        else:
            b["base"] -= amount
            b["quote"] += amount * price
        if info.fee_currency == self.ex.base:
            b["base"] -= info.fee_cost
            fee_usd = info.fee_cost * price
        else:
            b["quote"] -= info.fee_cost
            fee_usd = info.fee_cost
        self.state["stats"]["fees"] += fee_usd
        return amount, price, fee_usd

    # ------------------------------------------------------------------ grid

    def _build_grid(self, price):
        st, g = self.state, self.cfg.grid
        try:
            candles = self.ex.ohlcv(g.timeframe, g.atr_period + 5)
            atr = average_true_range(candles, g.atr_period)
            range_pct = range_pct_from_atr(price, atr, g)
            plan = plan_grid(price, range_pct, self.equity(price), g,
                             self.ex.min_amount, self.ex.min_cost, self.ex.round_amount)
            self._check_exchange_balance()
            self._rebalance(plan, price)
        except (GridError, BotError, ccxt.InsufficientFunds, ccxt.InvalidOrder) as e:
            self._pause(f"could not build grid: {e}")
            return

        st["grid"] = {
            "levels": plan.levels,
            "amount": plan.amount,
            "center_index": plan.center_index,
            "spacing_pct": plan.spacing_pct,
            "lower": plan.lower,
            "upper": plan.upper,
            "created_at": utcnow().isoformat(timespec="seconds"),
            "created_price": price,
        }
        st["orders"] = {}
        for i in plan.buy_indexes:
            self._place(i, "buy", plan.amount)
        for i in plan.sell_indexes:
            self._place(i, "sell", plan.amount)
        self.save()

        msg = (
            f"Price: ${price:,.2f}\n"
            f"Range: ${plan.lower:,.2f} - ${plan.upper:,.2f} (+/-{range_pct:.1f}%)\n"
            f"Levels: {len(plan.levels)}, spacing {plan.spacing_pct:.2f}%\n"
            f"Order size: {plan.amount} {self.ex.base} (~${plan.amount * price:,.2f})\n"
            f"Buy orders: {len(plan.buy_indexes)}, sell orders: {len(plan.sell_indexes)}\n"
            f"Equity: ${self.equity(price):,.2f}"
        )
        log.info("grid built\n%s", msg)
        self.notifier.send("Grid built", msg)

    def _check_exchange_balance(self):
        free = self.ex.free_balance()
        if free is None:
            return
        base_free, quote_free = free
        b = self.state["balances"]
        if quote_free < b["quote"] * 0.99:
            raise BotError(f"{self.cfg.exchange_name} has ${quote_free:,.2f} {self.ex.quote} free "
                           f"but the bot expects ${b['quote']:,.2f}")
        if base_free < b["base"] * 0.99:
            raise BotError(f"{self.cfg.exchange_name} has {base_free} {self.ex.base} free "
                           f"but the bot expects {b['base']}")

    def _rebalance(self, plan, price):
        """Buy or sell at market so we hold exactly the coins the sell orders need."""
        b = self.state["balances"]
        diff = len(plan.sell_indexes) * plan.amount - b["base"]
        if diff > 0:
            amount = max(self.ex.round_amount(diff * 1.001), self.ex.min_amount)
            side = "buy"
        elif -diff >= self.ex.min_amount:
            amount = self.ex.round_amount(-diff)
            side = "sell"
        else:
            return
        info = self.ex.market_order(side, amount, price)
        amount, fill_price, fee = self._apply_fill(side, info)
        log.info("rebalance: market %s %s %s @ %.4f (fee $%.4f)", side, amount, self.ex.base, fill_price, fee)
        self._log_trade("rebalance", side, amount, fill_price, fee)

    def _place(self, index, side, amount):
        """Record the order first, then send it. If sending fails it stays
        pending (id None) and is retried on the next tick."""
        orders = self.state["orders"]
        key = str(index)
        if key in orders:
            # Happens when price swings across several levels within one poll:
            # the order in this slot will fill next, then this one goes in.
            log.info("level %d busy, queueing %s for later", index, side)
            self.state.setdefault("deferred", []).append({"index": index, "side": side, "amount": amount})
            return
        price = self.ex.round_price(self.state["grid"]["levels"][index])
        orders[key] = {"id": None, "side": side, "price": price, "amount": amount}
        self.save()
        self._submit(key)

    def _submit(self, key):
        order = self.state["orders"][key]
        order["id"] = self.ex.create_limit_order(order["side"], order["price"], order["amount"])
        self.save()
        log.info("placed %s %s %s @ %s (level %s)", order["side"], order["amount"], self.ex.base, order["price"], key)

    def _check_orders(self):
        orders = self.state["orders"]
        if not orders:
            return
        open_ids = self.ex.open_order_ids()
        done = []
        for key, order in list(orders.items()):
            if order["id"] is None:
                self._submit(key)
            elif order["id"] not in open_ids:
                info = self.ex.fetch_order(order["id"])
                if info.status != "open":
                    done.append((int(key), order, info))

        # Handle fills nearest the empty level first (buys top-down, sells
        # bottom-up) so each counter order lands on a level that is free.
        done.sort(key=lambda d: -d[0] if d[1]["side"] == "buy" else d[0])
        for index, order, info in done:
            key = str(index)
            del orders[key]
            self.ex.forget_order(order["id"])
            if info.filled > 0:
                self._on_fill(int(key), order, info)
            if info.status != "closed":
                remaining = self.ex.round_amount(order["amount"] - info.filled)
                log.warning("order at level %s was cancelled outside the bot (%s left)", key, remaining)
                if remaining >= self.ex.min_amount and remaining * order["price"] >= self.ex.min_cost:
                    self._place(index, order["side"], remaining)
            self.save()

        waiting = self.state.get("deferred", [])
        self.state["deferred"] = []
        for d in waiting:
            self._place(d["index"], d["side"], d["amount"])  # re-queues itself if still busy

    def _on_fill(self, index, order, info):
        st = self.state
        levels = st["grid"]["levels"]
        side = order["side"]
        amount, price, fee = self._apply_fill(side, info)
        stats = st["stats"]
        st["report"]["fills"] += 1

        if side == "buy":
            stats["buys"] += 1
            counter_index, counter_side = index + 1, "sell"
            profit_line = ""
            profit = None
        else:
            stats["sells"] += 1
            profit = None
            counter_index, counter_side = index - 1, "buy"
            if index > 0:
                # Profit of this completed buy-low/sell-high step, after both fees.
                buy_price = levels[index - 1]
                profit = amount * (price - buy_price) - fee - amount * buy_price * self.cfg.fees.maker_pct / 100
                stats["round_trips"] += 1
                stats["grid_profit"] += profit
                profit_line = f"Profit on this step: ${profit:.4f}\n"
            else:
                profit_line = ""

        self._log_trade("grid", side, amount, price, fee, profit)
        if 0 <= counter_index < len(levels):
            self._place(counter_index, counter_side, amount)
            next_line = f"Next: {counter_side} at ${levels[counter_index]:,.2f}\n"
        else:
            next_line = "Edge of grid reached, no counter order.\n"

        msg = (
            f"{side.upper()} {amount} {self.ex.base} @ ${price:,.4f} (fee ${fee:.4f})\n"
            f"{profit_line}{next_line}"
            f"Equity: ${self.equity(price):,.2f} (started ${st['start_equity']:,.2f})\n"
            f"Total grid profit: ${stats['grid_profit']:.4f} over {stats['round_trips']} round trips"
        )
        log.info("fill at level %d: %s", index, msg.replace("\n", " | "))
        if self.cfg.alerts.email_on_every_fill:
            self.notifier.send(f"{side.upper()} filled @ ${price:,.2f}", msg, priority="low")

    def _cancel_all(self):
        """Cancel every grid order, counting anything that filled before the cancel."""
        for key, order in list(self.state["orders"].items()):
            if order["id"] is not None:
                self.ex.cancel_order(order["id"])
                info = self.ex.fetch_order(order["id"])
                if info.filled > 0:
                    amount, price, fee = self._apply_fill(order["side"], info)
                    self._log_trade("before-cancel", order["side"], amount, price, fee)
                    self.state["stats"]["buys" if order["side"] == "buy" else "sells"] += 1
                self.ex.forget_order(order["id"])
            del self.state["orders"][key]
            self.save()
        self.state["deferred"] = []

    # ---------------------------------------------------------------- safety

    def _check_kill_switch(self, price):
        st = self.state
        equity = self.equity(price)
        floor = st["start_equity"] * (1 - self.cfg.risk.max_drawdown_pct / 100)
        if equity > floor:
            return False
        self._cancel_all()
        st["grid"] = None
        st["status"] = "halted"
        st["status_reason"] = f"kill switch: equity ${equity:,.2f} fell below ${floor:,.2f}"
        self.save()
        log.error(st["status_reason"])
        b = st["balances"]
        self.notifier.send(
            "KILL SWITCH - bot halted",
            f"Equity fell {self.cfg.risk.max_drawdown_pct}% below the starting ${st['start_equity']:,.2f}.\n"
            f"Price: ${price:,.2f}\nEquity: ${equity:,.2f}\n"
            f"Holding: ${b['quote']:,.2f} + {b['base']:.6f} {self.ex.base}\n\n"
            "All orders were cancelled. Your coins were NOT sold.\n"
            "To restart with a fresh grid (this resets the loss limit to today's value):\n"
            "  docker compose exec bot python -m bot resume\n"
            "To sell everything and start over:\n"
            "  docker compose exec bot python -m bot reset",
            priority="urgent",
        )
        return True

    def _check_breakout(self, price):
        grid = self.state["grid"]
        margin = grid["spacing_pct"] / 100
        if grid["lower"] * (1 - margin) <= price <= grid["upper"] * (1 + margin):
            return
        direction = "above" if price > grid["upper"] else "below"
        reason = (f"price ${price:,.2f} broke {direction} the grid "
                  f"(${grid['lower']:,.2f} - ${grid['upper']:,.2f})")
        if self.cfg.risk.breakout_action == "recenter":
            log.warning("%s, recentering", reason)
            self._cancel_all()
            self.state["grid"] = None
            self.notifier.send("Grid recentering", reason)
            self._build_grid(price)
        else:
            self._pause(reason)

    def _pause(self, reason):
        st = self.state
        self._cancel_all()
        st["grid"] = None
        st["status"] = "paused"
        st["status_reason"] = reason
        self.save()
        log.warning("paused: %s", reason)
        price = st["last_price"] or 0
        b = st["balances"]
        self.notifier.send(
            "Bot paused",
            f"Reason: {reason}\n\nAll orders were cancelled. Nothing was sold.\n"
            f"Holding: ${b['quote']:,.2f} + {b['base']:.6f} {self.ex.base}\n"
            f"Equity: ${self.equity(price):,.2f}\n\n"
            "To build a new grid around the current price:\n"
            "  docker compose exec bot python -m bot resume",
            priority="high",
        )

    # -------------------------------------------------------------- commands

    def _handle_command(self, price):
        path = self.cfg.command_path
        if not path.exists():
            return
        command = path.read_text().strip().lower()
        path.unlink()
        st = self.state
        log.info("received command: %s", command)
        if command == "pause":
            if st["status"] == "running":
                self._pause("paused by you")
        elif command == "resume":
            if st["status"] == "running":
                log.info("already running")
                return
            if st["status"] == "halted":
                st["start_equity"] = self.equity(price)
            self._cancel_all()
            st["grid"] = None
            st["status"] = "running"
            st["status_reason"] = ""
            self.save()
            self.notifier.send("Bot resuming", f"Building a new grid around ${price:,.2f}.\n"
                                               f"Loss limit measured from ${st['start_equity']:,.2f}.")
        elif command == "reset":
            self._reset(price)
        else:
            log.warning("unknown command %r (expected pause, resume or reset)", command)

    def _reset(self, price):
        self._cancel_all()
        base = self.state["balances"]["base"]
        amount = self.ex.round_amount(base)
        sold = ""
        if amount >= self.ex.min_amount:
            info = self.ex.market_order("sell", amount, price)
            amount, fill_price, fee = self._apply_fill("sell", info)
            self._log_trade("reset", "sell", amount, fill_price, fee)
            sold = f"Sold {amount} {self.ex.base} @ ${fill_price:,.2f}.\n"
        final = self.state["balances"]["quote"]
        self.state = new_state(self.cfg)
        if hasattr(self.ex, "book"):
            self.ex.book = self.state["paper"]
        self.save()
        self.notifier.send("Bot reset", f"{sold}Previous run ended with ${final:,.2f}.\n"
                                        f"Starting fresh with ${self.cfg.capital_usd:,.2f}.")

    # --------------------------------------------------------------- reports

    def _log_trade(self, kind, side, amount, price, fee, profit=None):
        append_jsonl(self.cfg.trades_path, {
            "t": utcnow().isoformat(timespec="seconds"), "kind": kind, "side": side,
            "amount": amount, "price": price, "fee": fee, "profit": profit,
        })

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

    def summary(self, price, report=None):
        st = self.state
        b, s, g = st["balances"], st["stats"], st["grid"]
        equity = self.equity(price)
        start = st["start_equity"]
        lines = [
            f"Mode: {st['mode'].upper()}   Status: {st['status']} {st['status_reason']}",
            f"{self.cfg.symbol} price: ${price:,.2f}",
            f"Equity: ${equity:,.2f} ({(equity / start - 1) * 100:+.2f}% since start at ${start:,.2f})",
        ]
        if report:
            lines.append(f"Last 24h: {(equity / report['equity'] - 1) * 100:+.2f}%, {report['fills']} fills")
        lines += [
            f"Holding: ${b['quote']:,.2f} + {b['base']:.6f} {self.cfg.symbol.split('/')[0]}",
            f"Trades: {s['buys']} buys, {s['sells']} sells, {s['round_trips']} completed round trips",
            f"Grid profit: ${s['grid_profit']:.4f}   Fees paid: ${s['fees']:.4f}",
            f"Kill switch at: ${start * (1 - self.cfg.risk.max_drawdown_pct / 100):,.2f}",
        ]
        if g:
            lines.append(f"Grid: ${g['lower']:,.2f} - ${g['upper']:,.2f}, {len(g['levels'])} levels, "
                         f"{g['spacing_pct']:.2f}% apart, {len(st['orders'])} open orders")
        return "\n".join(lines)

    def _error_email(self, error):
        now = utcnow()
        cooldown = self.cfg.alerts.error_email_cooldown_minutes * 60
        if self.last_error_email and (now - self.last_error_email).total_seconds() < cooldown:
            return
        self.last_error_email = now
        self.notifier.send("Error", f"{type(error).__name__}: {error}\n\nThe bot keeps running and will retry. "
                                    "Check the logs: docker compose logs --tail 100 bot", priority="high")
