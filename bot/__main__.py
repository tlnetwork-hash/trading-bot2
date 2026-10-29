"""Command line entry point.

    python -m bot            run the bot (this is what Docker runs)
    python -m bot check      test the exchange connection and send test alerts
    python -m bot status     show what the bot is doing
    python -m bot pause      cancel all orders and stop trading
    python -m bot resume     build a fresh grid and start trading again
    python -m bot reset      cancel orders, sell the bot's coins, start over
    python -m bot buy 10     pause the grid and market-buy $10 of coin ("all" = all cash)
    python -m bot sell all   pause the grid and market-sell the bot's coins (or a $ amount)
    python -m bot dashboard  serve the read-only web dashboard on port 8080
"""

import argparse
import logging
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

import ccxt

from .config import ConfigError, load_config
from .exchange import LiveExchange, MarketData, PaperExchange
from .notifier import Notifier
from .state import load_state, new_state, save_state
from .trader import GridBot

log = logging.getLogger("bot")


def setup_logging(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    file_handler = RotatingFileHandler(cfg.data_dir / "bot.log", maxBytes=5_000_000, backupCount=3)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def make_exchange(cfg, state):
    if cfg.mode == "live":
        return LiveExchange(cfg.symbol, cfg.api_key, cfg.api_secret, exchange=cfg.exchange)
    return PaperExchange(cfg.symbol, state["paper"], cfg.fees, cfg.paper_slippage_pct,
                         exchange=cfg.exchange, fill_through_pct=cfg.paper_fill_through_pct)


def make_notifier(cfg):
    if cfg.strategy == "trend":
        coins = "/".join(sym.split("/")[0] for sym in cfg.trend.symbols)
        prefix = f"{coins} trend {cfg.mode} ({cfg.exchange_name})"
    elif cfg.strategy == "bollinger":
        prefix = f"{cfg.symbol.split('/')[0]} bollinger {cfg.mode} ({cfg.exchange_name})"
    else:
        prefix = f"{cfg.symbol.split('/')[0]} grid {cfg.mode} ({cfg.exchange_name})"
    return Notifier(cfg.email, prefix, cfg.ntfy_url)


def run(cfg):
    setup_logging(cfg)
    log.info("starting in %s mode on %s %s with $%.2f",
             cfg.mode.upper(), cfg.exchange_name, cfg.symbol, cfg.capital_usd)
    if cfg.mode == "live":
        log.warning("LIVE MODE: real orders will be placed on %s", cfg.exchange_name)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    if cfg.strategy == "trend":
        return run_trend(cfg, stop)
    if cfg.strategy == "bollinger":
        return run_bollinger(cfg, stop)

    state = load_state(cfg.state_path)
    if state is None:
        state = new_state(cfg)
        save_state(cfg.state_path, state)
        log.info("no saved state found, starting fresh")
    notifier = make_notifier(cfg)

    # The exchange can be briefly unreachable when the server boots; keep trying.
    while not stop.is_set():
        try:
            exchange = make_exchange(cfg, state)
            break
        except ccxt.NetworkError as e:
            log.warning("cannot connect to %s yet (%s), retrying in 30s", cfg.exchange_name, e)
            stop.wait(30)
    else:
        return

    GridBot(cfg, exchange, notifier, state).run_forever(stop)


def take_over_live_grid(cfg, state):
    """First live start in bollinger mode: cancel the grid's resting orders on the
    exchange and carry over its money and coins (no sale). Each held coin lot keeps
    the price the grid planned to sell it at, through the +min_profit_pct rule."""
    from pathlib import Path
    grid_path = cfg.data_dir / f"state-{cfg.mode}.json"
    grid = load_state(grid_path)
    ex = LiveExchange(cfg.symbol, cfg.api_key, cfg.api_secret, exchange=cfg.exchange)
    for oid in ex.open_order_ids():
        ex.cancel_order(oid)
        log.info("cancelled grid order %s on %s", oid, cfg.exchange_name)
    if grid is None:
        return
    for o in grid.get("orders", {}).values():
        if o.get("id"):
            ex.cancel_order(o["id"])
    b = grid["balances"]
    state["balances"] = {"quote": b["quote"], "base": b["base"]}
    state["start_equity"] = grid["start_equity"]          # keeps the $45 kill switch
    state["report"]["equity"] = grid["report"].get("equity", grid["start_equity"])
    lots, left = [], b["base"]
    for o in sorted(grid.get("orders", {}).values(), key=lambda o: o["price"]):
        if o["side"] != "sell" or left <= 0:
            continue
        qty = min(o["amount"], left)
        buy = o.get("buy_price") or o["price"] / (1 + cfg.bollinger.min_profit_pct / 100)
        lots.append({"id": len(lots) + 1, "qty": qty, "price": buy, "cost": qty * buy,
                     "t": grid.get("created_at"), "size_mult": None, "from_grid": True})
        left -= qty
    if lots and left > 1e-9:
        lots[0]["qty"] += left                               # dust goes with the cheapest lot
        lots[0]["cost"] = lots[0]["qty"] * lots[0]["price"]
    state["lots"], state["seq"] = lots, len(lots)
    grid["status"], grid["status_reason"] = "paused", "replaced by bollinger mode on " + \
        state["created_at"][:10] + " (its orders were cancelled; coins handed over)"
    grid["orders"], grid["grid"] = {}, None
    save_state(grid_path, grid)
    log.info("took over the live grid: %d coin lot(s): %s", len(lots),
             ", ".join(f"{l['qty']:.4f} @ ${l['price']:.2f}" for l in lots))


def run_bollinger(cfg, stop):
    from .bollinger import BollingerBot, new_bb_state
    b = cfg.bollinger
    log.info("strategy: bollinger (%d, %g) on %s candles, trend filter %dx%s",
             b.period, b.std_dev, b.timeframe, b.trend_period, b.trend_timeframe)
    state = load_state(cfg.state_path)
    if state is None:
        state = new_bb_state(cfg)
        if cfg.mode == "live":
            take_over_live_grid(cfg, state)
        save_state(cfg.state_path, state)
        log.info("bollinger state started: $%.2f cash + %.6f coins",
                 state["balances"]["quote"], state["balances"]["base"])
    notifier = make_notifier(cfg)
    while not stop.is_set():
        try:
            bot = BollingerBot(cfg, notifier, state)
            break
        except ccxt.NetworkError as e:
            log.warning("cannot connect to %s yet (%s), retrying in 30s", cfg.exchange_name, e)
            stop.wait(30)
    else:
        return
    bot.run_forever(stop)


def run_trend(cfg, stop):
    from .trend import TrendBot, new_trend_state
    log.info("strategy: trend on %s, %d-day average", ", ".join(cfg.trend.symbols), cfg.trend.sma_days)
    state = load_state(cfg.state_path)
    if state is None:
        state = new_trend_state(cfg)
        save_state(cfg.state_path, state)
        log.info("no saved trend state found, starting fresh")
    notifier = make_notifier(cfg)
    while not stop.is_set():
        try:
            bot = TrendBot(cfg, notifier, state)
            break
        except ccxt.NetworkError as e:
            log.warning("cannot connect to %s yet (%s), retrying in 30s", cfg.exchange_name, e)
            stop.wait(30)
    else:
        return
    bot.run_forever(stop)


def check(cfg):
    name = cfg.exchange_name
    print(f"Config OK: {cfg.mode.upper()} mode, {name} {cfg.symbol}, ${cfg.capital_usd:,.2f}")
    market = MarketData(cfg.symbol, exchange=cfg.exchange)
    print(f"{name} reachable: {cfg.symbol} = ${market.price():,.2f} "
          f"(min order {market.min_amount} {market.base})")
    if cfg.api_key:
        live = LiveExchange(cfg.symbol, cfg.api_key, cfg.api_secret, exchange=cfg.exchange)
        base_free, quote_free = live.free_balance()
        print(f"{name} API key works: ${quote_free:,.2f} {live.quote} and {base_free} {live.base} available")
    else:
        print(f"No {name} API key set (fine for paper mode)")
    notifier = make_notifier(cfg)
    if cfg.ntfy_url:
        ok = notifier._attempt("ntfy", notifier._send_ntfy, "Test alert",
                               "If you can read this, phone alerts are working.", "default")
        print(f"Test phone alert sent to ntfy topic {cfg.ntfy_topic}" if ok else
              "Test phone alert via ntfy FAILED - see the error above")
    else:
        print("Phone alerts (ntfy) not configured: set NTFY_TOPIC in .env")
    if cfg.email.enabled:
        send = notifier._send_brevo if cfg.email.method == "brevo" else notifier._send_gmail
        ok = notifier._attempt(cfg.email.method, send, f"[{notifier.prefix}] Test email",
                               "If you can read this, email alerts are working.")
        print(f"Test email sent via {cfg.email.method} to {cfg.email.to_address} "
              "(check spam if you don't see it)" if ok else
              f"Test email via {cfg.email.method} FAILED - see the error above")
    else:
        print("Email not configured: set GMAIL_ADDRESS plus BREVO_API_KEY or GMAIL_APP_PASSWORD in .env")


def status(cfg):
    state = load_state(cfg.state_path)
    if state is None:
        print(f"No saved state for {cfg.mode} mode yet. Has the bot run?")
        return
    if cfg.strategy == "trend":
        from .trend import TrendBot
        bot = TrendBot.__new__(TrendBot)
        bot.cfg, bot.state = cfg, state
        print(bot.summary())
        return
    if cfg.strategy == "bollinger":
        from .bollinger import BollingerBot
        bot = BollingerBot.__new__(BollingerBot)
        bot.cfg, bot.b, bot.state = cfg, cfg.bollinger, state
        print(bot.summary())
        return
    price = state.get("last_price") or 0
    bot = GridBot.__new__(GridBot)  # summary() only needs cfg and state
    bot.cfg, bot.state = cfg, state
    print(bot.summary(price))


def send_command(cfg, command):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.command_path.write_text(command)
    print(f"Sent '{command}'. The bot will act on it within {cfg.poll_seconds} seconds.")
    print("Watch it happen with:  docker compose logs -f --tail 20 bot")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m bot", description="Grid trading bot")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "check", "status", "pause", "resume", "reset", "dashboard",
                                 "buy", "sell", "risk", "risk-now"])
    parser.add_argument("amount", nargs="?", default="",
                        help="for buy/sell: a dollar amount (e.g. 10) or 'all'")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    if args.command in ("buy", "sell"):
        from .remote import trade_amount_ok
        if not trade_amount_ok(args.amount.lower()):
            parser.error(f"{args.command} needs a dollar amount or 'all', e.g. {args.command} 10")
        args.command = f"{args.command} {args.amount.lower().lstrip('$')}"

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"Config problem: {e}", file=sys.stderr)
        if args.command == "run":
            time.sleep(60)  # avoid a tight restart loop under Docker
        sys.exit(2)

    if args.command == "run":
        run(cfg)
    elif args.command == "check":
        try:
            check(cfg)
        except ccxt.AuthenticationError as e:
            sys.exit(f"{cfg.exchange_name} rejected the API key/secret: {e}")
        except ccxt.PermissionDenied as e:
            sys.exit(f"{cfg.exchange_name} API key is missing a permission (see README step 5): {e}")
        except ccxt.NetworkError as e:
            sys.exit(f"Cannot reach {cfg.exchange_name} from this server: {e}")
    elif args.command == "status":
        status(cfg)
    elif args.command in ("risk", "risk-now"):
        from . import risk
        from .exchange import make_client
        client = make_client("binanceus")
        if args.command == "risk-now":
            pts, reasons, facts = risk.score(client)
            print(f"Score {pts} -> {risk.level_for(pts, 'normal').upper()}")
            print("\n".join(reasons) or "No warning signs.")
            print(facts)
            return
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        notifier = Notifier(cfg.email, "Risk monitor", cfg.ntfy_url)
        notifier.send("Risk monitor started", "Checking market risk every 10 minutes.", priority="low")
        risk.run_monitor(notifier, client, stop)
    elif args.command == "dashboard":
        if cfg.strategy == "bollinger":
            from .bbdash import serve
        else:
            from .dashboard import serve
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        serve(cfg, notifier=make_notifier(cfg))
    else:
        send_command(cfg, args.command)


if __name__ == "__main__":
    main()
