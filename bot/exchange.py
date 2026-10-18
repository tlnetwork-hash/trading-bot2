"""Talking to the exchange (live) or simulating it (paper).

Both classes expose the same methods, so the trading logic never needs to
know which mode it is running in. Paper mode still reads real prices and
market rules from the exchange's public API; it just never sends orders.
The exchange is any ccxt id listed in config.EXCHANGES (kraken, binanceus).
"""

import time
from dataclasses import dataclass

import ccxt


@dataclass
class OrderInfo:
    status: str            # "open", "closed" (fully filled) or "canceled"
    filled: float          # coins filled so far
    average: float         # average fill price
    fee_cost: float = 0.0
    fee_currency: str = ""


def make_client(exchange, **options):
    return getattr(ccxt, exchange)({"enableRateLimit": True, "timeout": 30000, **options})


def _order_info(order, fallback_price):
    fee = order.get("fee") or {}
    status = order.get("status") or "open"
    if status == "expired":
        status = "canceled"
    return OrderInfo(
        status=status,
        filled=float(order.get("filled") or 0),
        average=float(order.get("average") or order.get("price") or fallback_price),
        fee_cost=float(fee.get("cost") or 0),
        fee_currency=fee.get("currency") or "",
    )


class MarketData:
    """Public, read-only exchange data: prices, candles and order size rules."""

    def __init__(self, symbol, client=None, exchange="kraken"):
        self.client = client or make_client(exchange)
        self.symbol = symbol
        self.client.load_markets()
        self.market = self.client.market(symbol)
        self.base = self.market["base"]
        self.quote = self.market["quote"]
        limits = self.market.get("limits") or {}
        self.min_amount = float((limits.get("amount") or {}).get("min") or 0)
        self.min_cost = float((limits.get("cost") or {}).get("min") or 0)

    def price(self):
        return float(self.client.fetch_ticker(self.symbol)["last"])

    def ohlcv(self, timeframe, limit):
        return self.client.fetch_ohlcv(self.symbol, timeframe, limit=limit)

    def round_amount(self, amount):
        try:
            return float(self.client.amount_to_precision(self.symbol, amount))
        except ccxt.BaseError:
            return 0.0  # smaller than the exchange's smallest unit

    def round_price(self, price):
        return float(self.client.price_to_precision(self.symbol, price))


class LiveExchange(MarketData):
    def __init__(self, symbol, api_key, api_secret, client=None, exchange="kraken"):
        client = client or make_client(exchange, apiKey=api_key, secret=api_secret)
        super().__init__(symbol, client)

    def simulate(self, price):
        pass  # real orders are filled by the exchange

    def create_limit_order(self, side, price, amount):
        return self.client.create_order(self.symbol, "limit", side, amount, price)["id"]

    def cancel_order(self, order_id):
        try:
            self.client.cancel_order(order_id, self.symbol)
        except ccxt.InvalidOrder:  # includes OrderNotFound
            pass  # already filled or cancelled

    def open_order_ids(self):
        return {o["id"] for o in self.client.fetch_open_orders(self.symbol)}

    def fetch_order(self, order_id):
        return _order_info(self.client.fetch_order(order_id, self.symbol), 0.0)

    def forget_order(self, order_id):
        pass

    def market_order(self, side, amount, ref_price):
        order = self.client.create_order(self.symbol, "market", side, amount)
        # Some exchanges (Kraken) confirm market orders asynchronously; wait for the fill.
        for _ in range(10):
            info = self.fetch_order(order["id"])
            if info.status != "open":
                break
            time.sleep(1)
        if not info.average:
            info.average = ref_price
        return info

    def free_balance(self):
        balance = self.client.fetch_balance()
        free = balance.get("free") or {}
        return float(free.get(self.base) or 0), float(free.get(self.quote) or 0)


class PaperExchange(MarketData):
    """Simulates orders against live prices. `book` is saved with the bot state
    so simulated orders survive restarts."""

    def __init__(self, symbol, book, fees, slippage_pct, client=None, exchange="kraken"):
        super().__init__(symbol, client, exchange)
        self.book = book
        self.fees = fees
        self.slippage_pct = slippage_pct

    def simulate(self, price):
        for order in self.book["orders"].values():
            if order["status"] != "open":
                continue
            if (order["side"] == "buy" and price <= order["price"]) or (
                order["side"] == "sell" and price >= order["price"]
            ):
                order["status"] = "closed"

    def create_limit_order(self, side, price, amount):
        self.book["seq"] += 1
        order_id = f"paper-{self.book['seq']}"
        self.book["orders"][order_id] = {"side": side, "price": price, "amount": amount, "status": "open"}
        return order_id

    def cancel_order(self, order_id):
        order = self.book["orders"].get(order_id)
        if order and order["status"] == "open":
            order["status"] = "canceled"

    def open_order_ids(self):
        return {oid for oid, o in self.book["orders"].items() if o["status"] == "open"}

    def fetch_order(self, order_id):
        order = self.book["orders"].get(order_id)
        if order is None:
            return OrderInfo("canceled", 0.0, 0.0)
        if order["status"] != "closed":
            return OrderInfo(order["status"], 0.0, order["price"])
        fee = order["price"] * order["amount"] * self.fees.maker_pct / 100
        return OrderInfo("closed", order["amount"], order["price"], fee, self.quote)

    def forget_order(self, order_id):
        self.book["orders"].pop(order_id, None)

    def market_order(self, side, amount, ref_price):
        slip = self.slippage_pct / 100
        price = ref_price * (1 + slip if side == "buy" else 1 - slip)
        fee = price * amount * self.fees.taker_pct / 100
        return OrderInfo("closed", amount, price, fee, self.quote)

    def free_balance(self):
        return None  # paper money is tracked by the bot itself
