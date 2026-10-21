from bot.config import FeeConfig
from bot.exchange import PaperExchange


class Client:
    def load_markets(self):
        pass

    def market(self, symbol):
        return {"base": "SOL", "quote": "USD", "limits": {}}


def make(through):
    book = {"seq": 0, "orders": {}}
    return PaperExchange("SOL/USD", book, FeeConfig(), 0.05, client=Client(), fill_through_pct=through)


def test_touch_does_not_fill():
    ex = make(0.05)
    buy = ex.create_limit_order("buy", 100.0, 1)
    sell = ex.create_limit_order("sell", 110.0, 1)
    ex.simulate(100.0)
    ex.simulate(110.0)
    assert ex.open_order_ids() == {buy, sell}


def test_trade_through_fills_at_order_price():
    ex = make(0.05)
    buy = ex.create_limit_order("buy", 100.0, 1)
    sell = ex.create_limit_order("sell", 110.0, 1)
    ex.simulate(99.96)      # not quite 0.05% through
    assert buy in ex.open_order_ids()
    ex.simulate(99.95)      # 0.05% through
    ex.simulate(110.055)
    assert ex.open_order_ids() == set()
    assert ex.fetch_order(buy).average == 100.0
    assert ex.fetch_order(sell).average == 110.0


def test_zero_keeps_old_touch_behaviour():
    ex = make(0.0)
    buy = ex.create_limit_order("buy", 100.0, 1)
    ex.simulate(100.0)
    assert ex.open_order_ids() == set()
