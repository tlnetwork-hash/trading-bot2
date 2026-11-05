from datetime import datetime, timedelta, timezone

import pytest

from bot.config import ConfigError
from stockbot.alpaca import AlpacaError
from stockbot.config import StockConfig, load_stock_config
from stockbot.news import score_articles, score_text
from stockbot.trader import NY, StockBot, new_state
from tests.conftest import FakeNotifier

# 11:00 New York time on a Thursday
NOW = datetime(2026, 11, 5, 11, 0, tzinfo=NY).astimezone(timezone.utc)


def snap(price, prev_close, volume, vwap=None, high=None, spread=0.01):
    return {
        "latestTrade": {"p": price},
        "latestQuote": {"bp": price - spread / 2, "ap": price + spread / 2},
        "dailyBar": {"o": prev_close, "h": high or price, "l": prev_close, "c": price,
                     "v": volume, "vw": vwap or price * 0.98},
        "prevDailyBar": {"c": prev_close, "v": 1_000_000},
    }


class FakeAlpaca:
    def __init__(self):
        self.is_open = True
        self.gainers = ["MOVE"]
        self.snaps = {"MOVE": snap(10.5, 10.0, 3_000_000)}   # +5%, heavy volume
        self.articles = []
        self.positions_list = []
        self.closed = {}
        self.orders = []
        self.closed_symbols = []
        self.acct = {"equity": "1000", "last_equity": "1000", "buying_power": "1000",
                     "daytrade_count": 0, "pattern_day_trader": False}

    def clock(self):
        return {"is_open": self.is_open}

    def calendar_day(self, date):
        return {"date": date, "open": "09:30", "close": "16:00"}

    def account(self):
        return self.acct

    def positions(self):
        return self.positions_list

    def movers(self, top):
        return {"gainers": [{"symbol": s} for s in self.gainers]}

    def most_active(self, top):
        return []

    def snapshots(self, symbols):
        return {s: self.snaps[s] for s in symbols if s in self.snaps}

    def bars(self, symbols, timeframe, start):
        if timeframe == "1Day":
            return {s: [{"t": "2026-11-0%dT05:00:00Z" % d, "v": 1_000_000} for d in range(1, 5)]
                    for s in symbols}
        # five 5-minute bars below the current price, then the forming bar
        return {s: [{"h": self.snaps[s]["latestTrade"]["p"] - 0.1}] * 5 + [{"h": 99}] for s in symbols}

    def news(self, start, symbols=None):
        return self.articles

    def asset(self, symbol):
        return {"tradable": True, "status": "active"}

    def bracket_buy(self, symbol, qty, stop, target, tif):
        self.orders.append((symbol, qty, stop, target, tif))
        return {"id": "o1"}

    def close_position(self, symbol):
        self.closed_symbols.append(symbol)

    def closed_orders(self, symbol, after=None):
        return self.closed.get(symbol, [])


@pytest.fixture
def cfg(tmp_path):
    c = StockConfig(api_key="k", api_secret="s", data_dir=tmp_path)
    c.news.lookback_hours = 16
    return c


def make(cfg, now=NOW):
    client, notifier = FakeAlpaca(), FakeNotifier()
    clock = {"now": now}
    bot = StockBot(cfg, client, notifier, new_state(), now=lambda: clock["now"])
    return bot, client, notifier, clock


# --- news -------------------------------------------------------------------

def test_score_text_good_and_bad():
    assert score_text("FDA approves Acme's cancer drug")[0] >= 4
    assert score_text("Acme beats Q3 revenue estimates, raises guidance")[0] >= 6
    assert score_text("Acme announces $50 million public offering of common stock")[0] <= -4
    assert score_text("Acme files for Chapter 11 bankruptcy")[0] <= -5
    assert score_text("Acme to present at investor conference")[0] == 0


def test_score_articles_weights_and_skips_roundups():
    articles = [
        {"headline": "Acme to be acquired by Globex", "symbols": ["ACME"],
         "created_at": iso_ago(1)},
        {"headline": "Stocks jump", "symbols": list("ABCDEFG"), "created_at": iso_ago(1)},
        {"headline": "Old upgrade for Acme", "symbols": ["ACME"], "created_at": iso_ago(20)},
    ]
    result = score_articles(articles, NOW)
    assert set(result) == {"ACME"}
    assert result["ACME"].score == pytest.approx(4 + 2 * 0.3)
    assert result["ACME"].best.headline == "Acme to be acquired by Globex"


def iso_ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


# --- scanner + trading ------------------------------------------------------

def test_buys_a_breakout_with_bracket(cfg):
    bot, client, notifier, _ = make(cfg)
    bot.step()
    assert len(client.orders) == 1
    sym, qty, stop, target, tif = client.orders[0]
    # risk $10 / ($10.50 * 3%) = 31 shares, capped at 30% of $1000 = 28 shares
    assert (sym, qty, tif) == ("MOVE", 28, "day")
    assert stop == pytest.approx(10.19) and target == pytest.approx(11.13)
    assert "Bought MOVE" in notifier.subjects()
    assert "MOVE" in bot.state["positions"]


def test_skips_weak_or_bad_news_stocks(cfg):
    bot, client, _, _ = make(cfg)
    client.gainers = ["FLAT", "FADE", "THIN", "DILU"]
    client.snaps = {
        "FLAT": snap(10.1, 10.0, 3_000_000),                 # only +1%
        "FADE": snap(10.5, 10.0, 3_000_000, high=11.5),      # far off its high
        "THIN": snap(10.5, 10.0, 200_000),                   # no volume
        "DILU": snap(10.5, 10.0, 3_000_000),
    }
    client.articles = [{"headline": "DILU prices $20M registered direct offering",
                        "symbols": ["DILU"], "created_at": iso_ago(1)}]
    candidates = bot.scanner.scan(bot.news.scan(now=NOW), NOW, *bot.session(NOW))
    reasons = {c.symbol: c.rejected for c in candidates}
    assert "only" in reasons["FLAT"]
    assert "fading" in reasons["FADE"]
    assert "volume" in reasons["THIN"]
    assert "bad news" in reasons["DILU"]
    bot.step()
    assert client.orders == []


def test_no_trades_in_first_minutes_or_when_closed(cfg):
    bot, client, _, clock = make(cfg, now=datetime(2026, 11, 5, 9, 35, tzinfo=NY))
    bot.step()
    assert client.orders == []
    client.is_open = False
    clock["now"] = NOW
    bot.step()
    assert client.orders == []


def test_flattens_before_close_in_day_style(cfg):
    bot, client, notifier, clock = make(cfg)
    bot.step()
    client.positions_list = [{"symbol": "MOVE", "market_value": "294"},
                             {"symbol": "MINE", "market_value": "500"}]  # user's own stock
    clock["now"] = datetime(2026, 11, 5, 15, 52, tzinfo=NY)
    bot.step()
    bot.step()
    assert client.closed_symbols == ["MOVE"]  # once, and never the user's own stock


def test_records_take_profit_sale(cfg):
    bot, client, notifier, clock = make(cfg)
    bot.step()
    client.closed["MOVE"] = [{"side": "buy", "filled_avg_price": "10.50", "filled_at": "x1",
                              "legs": [{"side": "sell", "filled_avg_price": "11.13",
                                        "filled_at": "2026-11-05T17:00:00Z"}]}]
    clock["now"] = NOW + timedelta(minutes=30)
    bot.step()
    assert "MOVE" not in bot.state["positions"]
    assert bot.state["stats"]["pnl"] == pytest.approx((11.13 - 10.5) * 28)
    assert any(s.startswith("Sold MOVE (take-profit)") for s in notifier.subjects())


def test_daily_loss_limit_stops_trading(cfg):
    bot, client, notifier, _ = make(cfg)
    client.acct["equity"] = "960"   # down $40 = 4% of $1000 capital
    bot.step()
    assert client.orders == []
    assert bot.state["halted_today"]
    assert "Daily loss limit hit, done for today" in notifier.subjects()


def test_pdt_rule_blocks_fourth_day_trade(cfg):
    bot, client, notifier, _ = make(cfg)
    client.acct["daytrade_count"] = 3
    bot.step()
    assert client.orders == []
    assert "Day-trade limit reached" in notifier.subjects()


def test_does_not_rebuy_same_stock_same_day(cfg):
    bot, client, _, clock = make(cfg)
    bot.step()
    bot.state["positions"].clear()            # sold already
    clock["now"] = NOW + timedelta(minutes=10)
    bot.step()
    assert len(client.orders) == 1


def test_refused_order_is_not_retried(cfg):
    bot, client, _, clock = make(cfg)

    def refuse(*a):
        raise AlpacaError("insufficient buying power", 403)
    client.bracket_buy = refuse
    bot.step()
    assert bot.state["positions"] == {} and "MOVE" in bot.state["traded_today"]


def test_news_alert_once_per_headline(cfg):
    bot, client, notifier, clock = make(cfg)
    client.is_open = False
    client.articles = [{"headline": "FDA approves Zeta's drug", "symbols": ["ZETA"],
                        "url": "u1", "created_at": iso_ago(0.5)}]
    bot.step()
    clock["now"] = NOW + timedelta(minutes=15)
    bot.step()
    assert [s for s in notifier.subjects() if s.startswith("News watch")] == ["News watch: ZETA"]


def test_pause_command(cfg):
    bot, client, _, _ = make(cfg)
    cfg.command_path.write_text("pause")
    bot.step()
    assert bot.state["paused"] and client.orders == []


# --- config -------------------------------------------------------------------

def test_config_needs_keys(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("mode: paper\n")
    with pytest.raises(ConfigError, match="ALPACA_API_KEY"):
        load_stock_config(path, env={})
    cfg = load_stock_config(path, env={"ALPACA_API_KEY": "a", "ALPACA_API_SECRET": "b"})
    assert cfg.trading_url == "https://paper-api.alpaca.markets"
    path.write_text("mode: live\n")
    cfg = load_stock_config(path, env={"ALPACA_LIVE_API_KEY": "a", "ALPACA_LIVE_API_SECRET": "b"})
    assert cfg.trading_url == "https://api.alpaca.markets"


def test_shipped_config_loads():
    cfg = load_stock_config("config-stocks.yaml", env={"ALPACA_API_KEY": "a", "ALPACA_API_SECRET": "b"})
    assert cfg.mode == "paper"
