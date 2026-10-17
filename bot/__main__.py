"""Command line entry point.

    python -m bot            run the bot (this is what Docker runs)
    python -m bot check      test the Kraken connection and send a test email
    python -m bot status     show what the bot is doing
    python -m bot pause      cancel all orders and stop trading
    python -m bot resume     build a fresh grid and start trading again
    python -m bot reset      cancel orders, sell the bot's coins, start over
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
        return LiveExchange(cfg.symbol, cfg.kraken_api_key, cfg.kraken_api_secret)
    return PaperExchange(cfg.symbol, state["paper"], cfg.fees, cfg.paper_slippage_pct)


def run(cfg):
    setup_logging(cfg)
    log.info("starting in %s mode on %s with $%.2f", cfg.mode.upper(), cfg.symbol, cfg.capital_usd)
    if cfg.mode == "live":
        log.warning("LIVE MODE: real orders will be placed on Kraken")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    state = load_state(cfg.state_path)
    if state is None:
        state = new_state(cfg)
        save_state(cfg.state_path, state)
        log.info("no saved state found, starting fresh")
    notifier = Notifier(cfg.email, f"{cfg.symbol} grid {cfg.mode.upper()}")

    # Kraken can be briefly unreachable when the server boots; keep trying.
    while not stop.is_set():
        try:
            exchange = make_exchange(cfg, state)
            break
        except ccxt.NetworkError as e:
            log.warning("cannot connect to Kraken yet (%s), retrying in 30s", e)
            stop.wait(30)
    else:
        return

    GridBot(cfg, exchange, notifier, state).run_forever(stop)


def check(cfg):
    print(f"Config OK: {cfg.mode.upper()} mode, {cfg.symbol}, ${cfg.capital_usd:,.2f}")
    market = MarketData(cfg.symbol)
    print(f"Kraken reachable: {cfg.symbol} = ${market.price():,.2f} "
          f"(min order {market.min_amount} {market.base})")
    if cfg.kraken_api_key:
        live = LiveExchange(cfg.symbol, cfg.kraken_api_key, cfg.kraken_api_secret)
        base_free, quote_free = live.free_balance()
        print(f"Kraken API key works: ${quote_free:,.2f} {live.quote} and {base_free} {live.base} available")
    else:
        print("No Kraken API key set (fine for paper mode)")
    if cfg.email.enabled:
        Notifier(cfg.email, f"{cfg.symbol} grid {cfg.mode.upper()}").send(
            "Test email", "If you can read this, email alerts are working.")
        print(f"Test email sent to {cfg.email.to_address} (check spam if you don't see it)")
    else:
        print("Email NOT configured: set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env")


def status(cfg):
    state = load_state(cfg.state_path)
    if state is None:
        print(f"No saved state for {cfg.mode} mode yet. Has the bot run?")
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
    parser = argparse.ArgumentParser(prog="python -m bot", description="Kraken grid trading bot")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "check", "status", "pause", "resume", "reset", "dashboard"])
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)

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
            sys.exit(f"Kraken rejected the API key/secret: {e}")
        except ccxt.PermissionDenied as e:
            sys.exit(f"Kraken API key is missing a permission (see README step 5): {e}")
        except ccxt.NetworkError as e:
            sys.exit(f"Cannot reach Kraken from this server: {e}")
    elif args.command == "status":
        status(cfg)
    elif args.command == "dashboard":
        from .dashboard import serve
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        serve(cfg)
    else:
        send_command(cfg, args.command)


if __name__ == "__main__":
    main()
