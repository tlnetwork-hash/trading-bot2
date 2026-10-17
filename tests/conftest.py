import math

import pytest

from bot.config import Config
from bot.exchange import PaperExchange
from bot.state import new_state
from bot.trader import GridBot


class FakeKraken:
    """Stands in for ccxt.kraken with a price the test controls."""

    def __init__(self, price=150.0):
        self.px = price

    def load_markets(self):
        pass

    def market(self, symbol):
        base, quote = symbol.split("/")
        return {"base": base, "quote": quote,
                "limits": {"amount": {"min": 0.02}, "cost": {"min": 0.5}}}

    def fetch_ticker(self, symbol):
        return {"last": self.px}

    def fetch_ohlcv(self, symbol, timeframe, limit):
        # Candles whose high-low range is 1% of the price -> ATR = 1.5 at $150.
        return [[i, self.px, self.px * 1.005, self.px * 0.995, self.px, 1] for i in range(limit)]

    def amount_to_precision(self, symbol, amount):
        value = math.floor(amount * 1e8) / 1e8
        if value <= 0:
            raise __import__("ccxt").InvalidOrder("too small")
        return str(value)

    def price_to_precision(self, symbol, price):
        return str(round(price, 2))


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))

    def subjects(self):
        return [s for s, _ in self.sent]


@pytest.fixture
def cfg(tmp_path):
    return Config(data_dir=tmp_path)


@pytest.fixture
def make_bot(cfg):
    def _make(price=150.0, state=None):
        state = state or new_state(cfg)
        kraken = FakeKraken(price)
        ex = PaperExchange(cfg.symbol, state["paper"], cfg.fees, cfg.paper_slippage_pct, client=kraken)
        notifier = FakeNotifier()
        return GridBot(cfg, ex, notifier, state), kraken, notifier
    return _make
