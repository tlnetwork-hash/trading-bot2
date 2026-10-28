"""The grid trading loop: places orders, reacts to fills, and enforces safety rules."""

import logging
import threading
import time
from datetime import datetime, timedelta

import ccxt

from .grid import GridError, average_true_range, plan_grid, range_pct_from_atr, plan_hold_grid
from .remote import HELP, RemoteCommands
from .state import append_jsonl, new_state, save_state, utcnow

log = logging.getLogger(__name__)

NETWORK_ALERT_AFTER = 20  # consecutive failed ticks (~10 min at 30s) before emailing
HISTORY_INTERVAL = 300   # seconds between equity points saved for the dashboard chart
SLIDE_CHECK_SECONDS = 300  # how often the slide guard re-reads hourly candles


class BotError(Exception):
    pass


def profit_lock_floor(risk, lock):
    """Equity at which the profit lock sells, or None while it is not armed yet.

    It arms once equity has been profit_lock_start_pct above the run's base,
    then keeps profit_lock_keep_pct of the best gain seen. The stop only moves
    up as the high rises, and always stays above the base.
    """
    if risk.profit_lock_start_pct <= 0:
        return None
    base, peak = lock["base"], lock["peak"]
    if peak < base * (1 + risk.profit_lock_start_pct / 100):
        return None
    return base + (peak - base) * risk.profit_lock_keep_pct / 100


def rebuy_target(risk, rebuy):
    """Price SOL has to bounce back to before the bot buys in again."""
    return rebuy["low"] * (1 + risk.restart_bounce_pct / 100)


def rebuy_after(risk, rebuy):
    return datetime.fromisoformat(rebuy["since"]) + timedelta(minutes=risk.restart_wait_minutes)


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
        # State files saved before the profit lock existed.
        state.setdefault("lock", {"base": state["start_equity"], "peak": state["start_equity"]})
        self.remote = None
        if cfg.ntfy_command_url:
            self.remote = RemoteCommands(cfg.ntfy_command_url, cfg.ntfy_command_pin,
                                         cfg.exchange, state)
        self.last_rejected_alert = None

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
        self._handle_phone_commands(price)
        st = self.state  # "reset" replaces the state object
        if st["status"] == "paused" and st.get("rebuy"):
            self._check_rebuy(price)
        if st["status"] == "paused" and st.get("resume_at_break_even"):
            self._check_break_even_resume(price)
        if st["status"] == "paused" and st.get("recenter_above"):
            self._check_recenter_above(price)

        self._apply_buy_floor()
        self._apply_risk_guard()
        if st["status"] == "running" and st["grid"] is None:
            self._build_grid(price)
        if st["status"] != "halted" and self._check_kill_switch(price):
            return
        if st["status"] == "running" and st["grid"] and not self._check_profit_lock(price):
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

    # ------------------------------------------------------------ risk guard

    def _risk_high(self):
        if self._sliding():
            return True
        if not self.cfg.risk.guard:
            return False
        from .risk import current_level
        return current_level() == "high"

    def _sliding(self):
        """Slide guard: True while the price is more than slide_guard_pct below its
        level slide_guard_hours ago. Re-checked at most every SLIDE_CHECK_SECONDS."""
        r = self.cfg.risk
        if r.slide_guard_pct <= 0:
            return False
        now = time.monotonic()
        cache = getattr(self, "_slide_cache", None)
        if cache and now - cache[0] < SLIDE_CHECK_SECONDS:
            return cache[1]
        try:
            candles = self.ex.ohlcv("1h", r.slide_guard_hours + 2)
            ref = float(candles[-(r.slide_guard_hours + 1)][4])
            price = float(self.state.get("last_price") or candles[-1][4])
            self._slide_change = (price / ref - 1) * 100
            sliding = price < ref * (1 - r.slide_guard_pct / 100)
        except Exception as e:  # keep the last answer if candles can't be read
            log.warning("slide guard: could not read candles (%s)", e)
            sliding = cache[1] if cache else False
        self._slide_cache = (now, sliding)
        return sliding

    def _guard_reason(self):
        if self._sliding():
            return (f"{self.ex.base} is down {abs(getattr(self, '_slide_change', 0)):.1f}% over the last "
                    f"{self.cfg.risk.slide_guard_hours} hours (slide guard)")
        return "Market risk is HIGH"

    def _apply_buy_floor(self):
        """Pull open buy orders priced under no_buy_below_usd; put held ones
        back once they're no longer under it (floor lowered or removed)."""
        st = self.state
        floor = self.cfg.risk.no_buy_below_usd
        grid = st.get("grid")
        held = st.setdefault("floor_held", [])
        pulled = 0
        if floor > 0:
            grid_id = (grid or {}).get("created_at")
            for key, order in list(st["orders"].items()):
                if order["side"] != "buy" or order["price"] >= floor:
                    continue
                remaining = order["amount"]
                if order["id"] is not None:
                    self.ex.cancel_order(order["id"])
                    info = self.ex.fetch_order(order["id"])
                    if info.filled > 0:
                        amount, fill_price, fee = self._apply_fill("buy", info)
                        self._log_trade("before-cancel", "buy", amount, fill_price, fee)
                        st["stats"]["buys"] += 1
                        remaining = self.ex.round_amount(order["amount"] - info.filled)
                    self.ex.forget_order(order["id"])
                del st["orders"][key]
                if remaining >= self.ex.min_amount:
                    held.append({"index": int(key), "amount": remaining, "grid": grid_id})
                pulled += 1
            if pulled:
                self.save()
                log.warning("buy floor $%.2f: pulled %d buy orders", floor, pulled)
                self.notifier.send("No buys below floor",
                                   f"Pulled {pulled} buy order(s) under ${floor:,.2f}. The bot won't buy "
                                   f"below that. Sell orders keep working.")
        if held and grid:
            keep, back = [], []
            for h in held:
                ok = h.get("grid") == grid.get("created_at") and 0 <= h["index"] < len(grid["levels"])
                if not ok:
                    continue  # belonged to an old grid
                (back if floor <= 0 or grid["levels"][h["index"]] >= floor else keep).append(h)
            st["floor_held"] = keep
            for h in back:
                self._place(h["index"], "buy", h["amount"])
            if back:
                self.save()
                log.info("buy floor: restored %d buy orders", len(back))

    def _apply_risk_guard(self):
        """While market risk is HIGH, cancel open buy orders and hold new ones;
        put them back when risk drops. Sell orders are never touched."""
        st = self.state
        high = self._risk_high()
        if high and not st.get("risk_on"):
            st["risk_on"] = True
            held = st.setdefault("risk_held", [])
            grid_id = (st.get("grid") or {}).get("created_at")
            for key, order in list(st["orders"].items()):
                if order["side"] != "buy":
                    continue
                remaining = order["amount"]
                if order["id"] is not None:
                    self.ex.cancel_order(order["id"])
                    info = self.ex.fetch_order(order["id"])
                    if info.filled > 0:
                        amount, fill_price, fee = self._apply_fill("buy", info)
                        self._log_trade("before-cancel", "buy", amount, fill_price, fee)
                        st["stats"]["buys"] += 1
                        remaining = self.ex.round_amount(order["amount"] - info.filled)
                    self.ex.forget_order(order["id"])
                del st["orders"][key]
                if remaining >= self.ex.min_amount:
                    held.append({"index": int(key), "amount": remaining, "grid": grid_id})
            self.save()
            log.warning("risk HIGH: holding %d buy orders", len(held))
            self.notifier.send("Risk guard ON",
                               f"{self._guard_reason()}, so new buy orders are on hold "
                               f"({len(held)} paused). Sell orders keep working.")
        elif not high and st.get("risk_on"):
            st["risk_on"] = False
            held, st["risk_held"] = st.get("risk_held", []), []
            grid = st.get("grid")
            placed = 0
            for h in held:
                if grid and h.get("grid") == grid.get("created_at") and 0 <= h["index"] < len(grid["levels"]):
                    self._place(h["index"], "buy", h["amount"])
                    placed += 1
            self.save()
            log.info("risk back to normal: restored %d buy orders", placed)
            self.notifier.send("Risk guard OFF",
                               f"Market risk is normal and the price isn't sliding, so buying is back on "
                               f"({placed} buy orders restored).",
                               priority="low")

    def _build_grid(self, price):
        st, g = self.state, self.cfg.grid
        if st.get("risk_on"):
            return  # wait for risk to drop before building/rebuying into a grid
        try:
            if g.range_below_pct > 0 and g.range_above_pct > 0:
                lower = price * (1 - g.range_below_pct / 100)
                upper = price * (1 + g.range_above_pct / 100)
                range_pct = 0
            else:
                candles = self.ex.ohlcv(g.timeframe, g.atr_period + 5)
                atr = average_true_range(candles, g.atr_period)
                range_pct = range_pct_from_atr(price, atr, g)
                lower, upper = price * (1 - range_pct / 100), price * (1 + range_pct / 100)
            self._check_exchange_balance()
            plan = None
            if g.keep_coins_on_build:
                b = st["balances"]
                plan = plan_hold_grid(price, lower, upper, b["base"], b["quote"], g,
                                      self.ex.min_amount, self.ex.min_cost, self.ex.round_amount)
            if plan is None:
                plan = plan_grid(price, range_pct, self.equity(price), g,
                                 self.ex.min_amount, self.ex.min_cost, self.ex.round_amount,
                                 lower=lower, upper=upper)
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
            if plan.held and str(i) in st["orders"]:
                st["orders"][str(i)]["held"] = True
        self.save()

        msg = (
            f"Price: ${price:,.2f}\n"
            f"Range: ${plan.lower:,.2f} - ${plan.upper:,.2f} "
            f"(-{(1 - plan.lower / price) * 100:.1f}% / +{(plan.upper / price - 1) * 100:.1f}%)\n"
            + ("Kept the coins it holds (no market sale); buys below fill in as sells complete.\n"
               if plan.held else "") +
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
        if side == "buy" and self.state.get("risk_on"):
            grid_id = (self.state.get("grid") or {}).get("created_at")
            self.state.setdefault("risk_held", []).append(
                {"index": index, "amount": amount, "grid": grid_id})
            log.info("risk HIGH: holding buy at level %d", index)
            return
        floor = self.cfg.risk.no_buy_below_usd
        if side == "buy" and floor > 0 and self.state["grid"]["levels"][index] < floor:
            grid_id = (self.state.get("grid") or {}).get("created_at")
            self.state.setdefault("floor_held", []).append(
                {"index": index, "amount": amount, "grid": grid_id})
            log.info("buy floor $%.2f: holding buy at level %d", floor, index)
            return
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
            if order.get("held"):
                profit_line = "Sold coins the bot already held (not a grid round trip).\n"
            elif index > 0:
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
        self.state["risk_held"] = []  # held buys belonged to the grid being torn down
        self.state["floor_held"] = []

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
        st.pop("rebuy", None)
        self.save()
        log.error(st["status_reason"])
        sold_line = "All orders were cancelled. Your coins were NOT sold.\n"
        if self.cfg.risk.kill_switch_sells:
            try:
                sold = self._sell_all(price, "kill")
            except Exception as e:  # still halted; say so loudly
                log.exception("kill switch could not sell")
                sold = f"Tried to sell but it FAILED: {e}\nSell by hand: send 'sell all'.\n"
            self.save()
            sold_line = "All orders were cancelled and the coins were sold.\n" + sold
        b = st["balances"]
        self.notifier.send(
            "KILL SWITCH - bot halted",
            f"Equity fell {self.cfg.risk.max_drawdown_pct}% below the starting ${st['start_equity']:,.2f}.\n"
            f"Price: ${price:,.2f}\nEquity: ${equity:,.2f}\n"
            f"Holding: ${b['quote']:,.2f} + {b['base']:.6f} {self.ex.base}\n\n"
            + sold_line +
            "To restart with a fresh grid (this resets the loss limit to today's value):\n"
            f"  docker compose exec {self.cfg.service_name} python -m bot resume\n"
            "To sell everything and start over:\n"
            f"  docker compose exec {self.cfg.service_name} python -m bot reset",
            priority="urgent",
        )
        return True

    def _check_profit_lock(self, price):
        """Trailing stop on equity. Returns True if it sold and paused."""
        lock = self.state["lock"]
        equity = self.equity(price)
        lock["peak"] = max(lock["peak"], equity)
        floor = profit_lock_floor(self.cfg.risk, lock)
        if floor is None or equity > floor:
            return False
        gain = (equity / lock["base"] - 1) * 100
        high = (lock["peak"] / lock["base"] - 1) * 100
        self._sell_all_and_pause(
            "Profit locked",
            f"profit lock: equity ${equity:,.2f} fell to the stop at ${floor:,.2f} "
            f"(high was ${lock['peak']:,.2f}, {high:+.2f}%)",
            f"The bot was up {high:+.2f}% at its high and dropped back to {gain:+.2f}%, "
            f"so it sold to keep the gain.",
            price,
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
        if direction == "below" and self.cfg.risk.sell_on_breakout_down:
            self._sell_all_and_pause(
                "Stop loss: sold on breakout",
                f"stop loss: {reason}",
                "The price fell out of the bottom of the grid, so the bot sold its "
                f"{self.ex.base} instead of holding it while it keeps falling.",
                price,
            )
        elif self.cfg.risk.breakout_action == "recenter":
            log.warning("%s, recentering", reason)
            self._cancel_all()
            self.state["grid"] = None
            self.notifier.send("Grid recentering", reason)
            self._build_grid(price)
        else:
            self._pause(reason)
            wait = self.cfg.risk.recenter_above_after_minutes
            if direction == "above" and wait > 0:
                st = self.state
                st["recenter_above"] = {"since": utcnow().isoformat(), "top": grid["upper"]}
                st["status_reason"] = reason + f" (rebuilds by itself if it holds above for {wait} min)"
                self.save()
                self.notifier.send(
                    "Will re-center by itself",
                    f"If {self.ex.base} stays above ${grid['upper']:,.2f} for {wait} minutes, the bot "
                    f"builds a new grid around the price. If it drops back below that first, it "
                    f"restarts right away. Send pause to cancel that.")
            if direction == "below" and self.cfg.risk.resume_at_break_even:
                st = self.state
                st["resume_at_break_even"] = True
                st["status_reason"] = reason + " (holding; restarts by itself at break-even)"
                self.save()
                self.notifier.send(
                    "Will restart at break-even",
                    f"Holding the coins. The bot restarts by itself once they're worth "
                    f"${st['start_equity']:,.2f} again (about ${self.break_even_price():,.2f} per "
                    f"{self.ex.base}). Send pause to cancel that.")

    def break_even_price(self):
        st, b = self.state, self.state["balances"]
        if b["base"] <= 0:
            return 0.0
        return (st["start_equity"] - b["quote"]) / b["base"]

    def _check_recenter_above(self, price):
        st = self.state
        r = st["recenter_above"]
        if price <= r["top"]:
            why = f"{self.ex.base} dropped back to ${price:,.2f}, inside the old grid."
        elif utcnow() >= datetime.fromisoformat(r["since"]) + timedelta(
                minutes=self.cfg.risk.recenter_above_after_minutes):
            why = (f"{self.ex.base} held above ${r['top']:,.2f} for "
                   f"{self.cfg.risk.recenter_above_after_minutes} minutes (now ${price:,.2f}).")
        else:
            return
        log.info("re-centering after upside breakout at %.4f", price)
        self._resume(price)
        self.notifier.send("Re-centering grid", why + " Building a new grid around this price.",
                           priority="high")

    def _check_break_even_resume(self, price):
        st = self.state
        if self.equity(price) < st["start_equity"]:
            return
        log.info("back to break-even at %.4f, resuming", price)
        self._resume(price)
        self.notifier.send(
            "Back to break-even, restarting",
            f"{self.ex.base} is at ${price:,.2f}, so the bot's money is back to "
            f"${self.equity(price):,.2f}. Building a new grid around this price.",
            priority="high")

    def _sell_all(self, price, kind):
        """Market-sell every coin the bot holds. Returns a line describing the sale."""
        amount = self.ex.round_amount(self.state["balances"]["base"])
        if amount < self.ex.min_amount:
            return ""
        info = self.ex.market_order("sell", amount, price)
        amount, fill_price, fee = self._apply_fill("sell", info)
        self.state["stats"]["sells"] += 1
        self._log_trade(kind, "sell", amount, fill_price, fee)
        self.state["last_sale_price"] = fill_price
        return f"Sold {amount} {self.ex.base} @ ${fill_price:,.2f} (fee ${fee:.4f}).\n"

    def _sell_all_and_pause(self, title, reason, explanation, price):
        st = self.state
        self._cancel_all()
        st["grid"] = None
        st["last_sale_price"] = price
        sold = self._sell_all(price, "stop")
        st["status"] = "paused"
        st["status_reason"] = reason
        risk = self.cfg.risk
        restart = ""
        if risk.auto_restart:
            st["rebuy"] = {"sold_price": st["last_sale_price"], "low": price,
                           "since": utcnow().isoformat(timespec="seconds")}
            restart = (f"It will start again by itself once {self.ex.base} has bounced "
                       f"{risk.restart_bounce_pct:g}% off its lowest price, is no higher than "
                       f"${st['last_sale_price']:,.2f} (where it sold), and at least "
                       f"{risk.restart_wait_minutes} minutes have passed.\n"
                       f"To stop that, run: docker compose exec {self.cfg.service_name} python -m bot pause\n")
        self.save()
        log.warning("paused after selling: %s", reason)
        b = st["balances"]
        equity = self.equity(price)
        self.notifier.send(
            title,
            f"{explanation}\n\n{sold}"
            f"Now holding: ${b['quote']:,.2f} + {b['base']:.6f} {self.ex.base}\n"
            f"Equity: ${equity:,.2f} ({(equity / st['start_equity'] - 1) * 100:+.2f}% since start)\n\n"
            f"The bot is paused and holding cash. {restart}"
            "To build a new grid around the current price now:\n"
            f"  docker compose exec {self.cfg.service_name} python -m bot resume",
            priority="high",
        )

    def _check_rebuy(self, price):
        """Auto-restart after a stop-loss or profit-lock sale, once the price has
        stopped falling and is no higher than where the bot sold."""
        st, risk = self.state, self.cfg.risk
        rebuy = st["rebuy"]
        rebuy["low"] = min(rebuy["low"], price)
        if not (price >= rebuy_target(risk, rebuy) and price <= rebuy["sold_price"]
                and utcnow() >= rebuy_after(risk, rebuy)):
            return
        log.info("good spot to buy again at %.4f (low %.4f, sold %.4f)",
                 price, rebuy["low"], rebuy["sold_price"])
        self._resume(price)
        self.notifier.send(
            "Bot restarted: good spot to buy",
            f"{self.ex.base} bounced from a low of ${rebuy['low']:,.2f} to ${price:,.2f}, "
            f"below the ${rebuy['sold_price']:,.2f} it sold at.\n"
            f"Building a new grid around ${price:,.2f}.",
            priority="high",
        )

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
            f"  docker compose exec {self.cfg.service_name} python -m bot resume",
            priority="high",
        )

    # -------------------------------------------------------------- commands

    def _handle_command(self, price):
        path = self.cfg.command_path
        if not path.exists():
            return
        command = path.read_text().strip().lower()
        path.unlink()
        log.info("received command: %s", command)
        self._run_command(command, price)

    def _handle_phone_commands(self, price):
        if self.remote is None:
            return
        for parsed in self.remote.poll():
            if parsed.error:
                self._phone_command_rejected(parsed.error)
            elif parsed.for_me:
                log.info("phone command: %s", parsed.command)
                self._phone_command(parsed.command, price)
        self.state["remote"] = self.remote.state

    def _phone_command(self, command, price):
        if command == "help":
            if self.cfg.remote_primary:   # one reply, not one per bot
                self.notifier.send("Phone commands", HELP)
            return
        if command == "status":
            self.notifier.send(f"Status: ${self.equity(price):,.2f}", self.summary(price))
            return
        before, waiting = self.state["status"], bool(self.state.get("rebuy"))
        self._run_command(command, price)
        if command == "resume" and before == "running":
            self.notifier.send("Already running", self.summary(price))
        elif command == "pause" and before != "running" and not waiting:
            self.notifier.send("Already paused", self.summary(price))

    def _phone_command_rejected(self, error):
        # Only the main bot answers, at most every 10 minutes, so a stranger
        # spamming the topic can't flood your phone.
        if not self.cfg.remote_primary:
            return
        if self.last_rejected_alert is not None and time.monotonic() - self.last_rejected_alert < 600:
            return
        self.last_rejected_alert = time.monotonic()
        self.notifier.send("Phone command ignored",
                           f"A message on the command topic was ignored: {error}.\n\n{HELP}",
                           priority="low")

    def _run_command(self, command, price):
        st = self.state
        if command == "pause":
            if st["status"] == "running":
                self._pause("paused by you")
            elif (st.pop("rebuy", None) or st.pop("resume_at_break_even", None)
                  or st.pop("recenter_above", None)):
                st["status_reason"] = "paused by you (automatic restart cancelled)"
                self.save()
                self.notifier.send("Automatic restart cancelled",
                                   "The bot stays paused until you run resume.")
        elif command == "resume":
            if st["status"] == "running":
                log.info("already running")
                return
            if st["status"] == "halted":
                st["start_equity"] = self.equity(price)
            self._resume(price)
            self.notifier.send("Bot resuming", f"Building a new grid around ${price:,.2f}.\n"
                                               f"Loss limit measured from ${st['start_equity']:,.2f}.")
        elif command == "reset":
            self._reset(price)
        elif command.split()[0] in ("buy", "sell"):
            self._manual_trade(command, price)
        else:
            log.warning("unknown command %r (expected pause, resume, reset, buy or sell)", command)

    def _manual_trade(self, command, price):
        """A market buy or sell you asked for. It pauses the grid first (cancelling
        its orders) so the bot's books stay right, and leaves it paused; send
        resume to rebuild a grid around whatever the bot then holds."""
        st, b, base = self.state, self.state["balances"], self.ex.base
        words = command.split()
        side = words[0]
        arg = words[1].lstrip("$") if len(words) > 1 else ""
        try:
            usd = None if arg == "all" else float(arg)
        except ValueError:
            usd = -1
        if usd is not None and usd <= 0:
            self.notifier.send(f"Manual {side} ignored",
                               f"'{command}' needs a dollar amount or 'all', e.g. {side} 10 or {side} all.")
            return

        if st["status"] == "running":
            self._cancel_all()
            st["grid"] = None
            st["status"] = "paused"
        st.pop("rebuy", None)  # you're in control now; no automatic restart
        st.pop("resume_at_break_even", None)
        st.pop("recenter_above", None)

        if side == "buy":
            spend = b["quote"] if usd is None else min(usd, b["quote"])
            # Room for the taker fee plus 0.5% of price movement / slippage.
            spend /= 1 + (self.cfg.fees.taker_pct + 0.5) / 100
            amount = self.ex.round_amount(spend / price)
        else:
            want = b["base"] if usd is None else usd / price
            amount = self.ex.round_amount(min(want, b["base"]))
        if amount <= 0 or amount < self.ex.min_amount or amount * price < max(self.ex.min_cost, 1.0):
            have = f"${b['quote']:,.2f} cash" if side == "buy" else f"{b['base']:.6f} {base}"
            self.save()
            self.notifier.send(f"Manual {side} too small",
                               f"Not enough to {side}: the bot has {have} and the exchange's minimum "
                               f"order is {self.ex.min_amount} {base} / ${max(self.ex.min_cost, 1.0):,.2f}.\n\n"
                               + self.summary(price))
            return

        info = self.ex.market_order(side, amount, price)
        amount, fill_price, fee = self._apply_fill(side, info)
        st["stats"]["buys" if side == "buy" else "sells"] += 1
        self._log_trade("manual", side, amount, fill_price, fee)
        if st["status"] == "paused":
            st["status_reason"] = "paused after your manual trade (send resume to restart the grid)"
        self.save()
        log.info("manual %s %s %s @ %.4f (fee $%.4f)", side, amount, base, fill_price, fee)
        self.notifier.send(
            f"Manual {side.upper()} done @ ${fill_price:,.2f}",
            f"{side.upper()} {amount} {base} @ ${fill_price:,.4f} (~${amount * fill_price:,.2f}, fee ${fee:.4f})\n"
            f"The grid is paused. Send resume to rebuild it around what the bot now holds.\n\n"
            + self.summary(fill_price),
            priority="high",
        )

    def _resume(self, price):
        st = self.state
        # The profit lock starts over from today's value.
        eq = self.equity(price)
        st["lock"] = {"base": max(eq, self.cfg.risk.profit_lock_min_base_usd), "peak": eq}
        st.pop("rebuy", None)
        st.pop("resume_at_break_even", None)
        st.pop("recenter_above", None)
        self._cancel_all()
        st["grid"] = None
        st["status"] = "running"
        st["status_reason"] = ""
        self.save()

    def _reset(self, price):
        self._cancel_all()
        sold = self._sell_all(price, "reset")
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
            self._lock_line(),
        ]
        if st.get("rebuy"):
            r = st["rebuy"]
            lines.append(
                f"Auto-restart: waiting for {self.cfg.symbol.split('/')[0]} to bounce to "
                f"${rebuy_target(self.cfg.risk, r):,.2f} (low ${r['low']:,.2f}) while at or below "
                f"${r['sold_price']:,.2f}, not before {rebuy_after(self.cfg.risk, r):%H:%M} UTC")
        if g:
            lines.append(f"Grid: ${g['lower']:,.2f} - ${g['upper']:,.2f}, {len(g['levels'])} levels, "
                         f"{g['spacing_pct']:.2f}% apart, {len(st['orders'])} open orders")
        return "\n".join(lines)

    def _lock_line(self):
        start = self.state["start_equity"]
        risk, lock = self.cfg.risk, self.state.get("lock") or {"base": start, "peak": start}
        if risk.profit_lock_start_pct <= 0:
            return "Profit lock: off"
        floor = profit_lock_floor(risk, lock)
        if floor is None:
            arm = lock["base"] * (1 + risk.profit_lock_start_pct / 100)
            return f"Profit lock: arms when equity reaches ${arm:,.2f} (high so far ${lock['peak']:,.2f})"
        return f"Profit lock: ARMED, sells if equity falls to ${floor:,.2f} (high ${lock['peak']:,.2f})"

    def _error_email(self, error):
        now = utcnow()
        cooldown = self.cfg.alerts.error_email_cooldown_minutes * 60
        if self.last_error_email and (now - self.last_error_email).total_seconds() < cooldown:
            return
        self.last_error_email = now
        self.notifier.send("Error", f"{type(error).__name__}: {error}\n\nThe bot keeps running and will retry. "
                                    "Check the logs: docker compose logs --tail 100 bot", priority="high")
