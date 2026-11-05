"""Stock bot command line.

    python -m stockbot            run the bot (this is what Docker runs)
    python -m stockbot check      test the Alpaca keys and send a test alert
    python -m stockbot scan       show what the market scanner finds right now
    python -m stockbot news       show today's strongest news catalysts
    python -m stockbot status     show positions and profit/loss
    python -m stockbot pause      stop opening new trades
    python -m stockbot resume     start opening trades again
    python -m stockbot sell-all   sell everything the bot owns and pause
"""

import argparse
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from bot.__main__ import setup_logging
from bot.config import ConfigError
from bot.notifier import Notifier

from .alpaca import Alpaca, AlpacaError
from .config import load_stock_config
from .news import NewsScanner, hot_list
from .trader import StockBot, load_or_new


def make_client(cfg):
    return Alpaca(cfg.api_key, cfg.api_secret, cfg.trading_url, cfg.data_feed)


def make_notifier(cfg):
    return Notifier(cfg.email, f"Stock bot {cfg.mode}", cfg.ntfy_url)


def run(cfg):
    setup_logging(cfg)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    state = load_or_new(cfg.state_path)
    StockBot(cfg, make_client(cfg), make_notifier(cfg), state).run_forever(stop)


def check(cfg):
    client = make_client(cfg)
    acct = client.account()
    clock = client.clock()
    print(f"Alpaca {cfg.mode.upper()} account OK: equity ${float(acct['equity']):,.2f}, "
          f"buying power ${float(acct['buying_power']):,.2f}, "
          f"day trades used (5 days): {acct.get('daytrade_count', 0)}")
    print(f"Market is {'OPEN' if clock['is_open'] else 'closed'} "
          f"(next open {clock['next_open']}, next close {clock['next_close']})")
    print(f"Market data ({cfg.data_feed}) OK: {len(client.movers(10).get('gainers', []))} top gainers loaded")
    notifier = make_notifier(cfg)
    ok = notifier.send("Test alert", "If you can read this, stock bot alerts are working.")
    print("Test alert sent" if ok else "No alert channel worked (set NTFY_TOPIC or email in .env)")


def scan(cfg):
    client = make_client(cfg)
    bot = StockBot(cfg, client, make_notifier(cfg), load_or_new(cfg.state_path))
    now = datetime.now(timezone.utc)
    news_map = bot.news.scan(now=now) if cfg.news.enabled else {}
    open_at, close_at = bot.session(now)
    candidates = bot.scanner.scan(news_map, now, open_at, close_at)
    passed = [c for c in candidates if not c.rejected]
    print(f"Looked at {len(candidates)} stocks; {len(passed)} pass every rule:\n")
    for c in passed:
        print(f"  BUY-READY  {c.line()}")
    print("\nClosest misses:")
    for c in [c for c in candidates if c.rejected and c.gain_pct > 0][:15]:
        print(f"  {c.symbol:<6} ${c.price:>8.2f}  {c.gain_pct:+6.1f}%  rvol {c.rel_volume:4.1f}x  -> {c.rejected}")


def news(cfg):
    news_map = NewsScanner(make_client(cfg), cfg.news).scan()
    good = hot_list(news_map, 1, limit=15)
    bad = sorted((n for n in news_map.values() if n.score <= -2), key=lambda n: n.score)[:10]
    print(f"Strongest good news (last {cfg.news.lookback_hours}h):")
    for n in good:
        print(f"  {n.symbol:<6} {n.score:+5.1f}  {n.best.headline[:90]}")
    print("\nBad news (the bot won't buy these):")
    for n in bad:
        print(f"  {n.symbol:<6} {n.score:+5.1f}  {n.best.headline[:90]}")


def status(cfg):
    bot = StockBot(cfg, None, None, load_or_new(cfg.state_path))
    print(bot.summary())


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m stockbot", description="Stock momentum + news bot")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "check", "scan", "news", "status", "pause", "resume", "sell-all"])
    parser.add_argument("--config", default="config-stocks.yaml")
    args = parser.parse_args(argv)
    try:
        cfg = load_stock_config(args.config)
    except ConfigError as e:
        print(f"Config problem: {e}", file=sys.stderr)
        if args.command == "run":
            time.sleep(60)  # avoid a tight restart loop under Docker
        sys.exit(2)

    try:
        if args.command == "run":
            run(cfg)
        elif args.command in ("pause", "resume", "sell-all"):
            cfg.data_dir.mkdir(parents=True, exist_ok=True)
            cfg.command_path.write_text(args.command)
            print(f"Sent '{args.command}'. The bot acts on it within {cfg.poll_seconds} seconds.")
        else:
            {"check": check, "scan": scan, "news": news, "status": status}[args.command](cfg)
    except AlpacaError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
