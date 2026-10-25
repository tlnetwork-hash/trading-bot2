"""Grid math: volatility measurement and laying out grid levels.

These are pure functions with no network access, so they are easy to test.
"""

import math
from dataclasses import dataclass


@dataclass
class GridPlan:
    levels: list          # grid prices, lowest first
    amount: float         # coins (base currency) per order
    center_index: int     # level closest to the current price; left empty
    spacing_pct: float
    buy_list: list = None   # set by plan_hold_grid: only these levels get orders now
    sell_list: list = None
    held: bool = False      # sells use coins the bot already held

    @property
    def lower(self):
        return self.levels[0]

    @property
    def upper(self):
        return self.levels[-1]

    @property
    def buy_indexes(self):
        if self.buy_list is not None:
            return list(self.buy_list)
        return list(range(0, self.center_index))

    @property
    def sell_indexes(self):
        if self.sell_list is not None:
            return list(self.sell_list)
        return list(range(self.center_index + 1, len(self.levels)))


class GridError(Exception):
    pass


def average_true_range(ohlcv, period):
    """Average True Range from ccxt candles [ts, open, high, low, close, volume]."""
    if len(ohlcv) < period + 1:
        raise GridError(f"need at least {period + 1} candles to measure volatility, got {len(ohlcv)}")
    true_ranges = []
    for prev, cur in zip(ohlcv[:-1], ohlcv[1:]):
        high, low, prev_close = cur[2], cur[3], prev[4]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(true_ranges[-period:]) / period


def range_pct_from_atr(price, atr, grid_cfg):
    """Half-width of the grid as a percent of price, clamped to the configured bounds."""
    pct = atr * grid_cfg.atr_multiplier / price * 100
    return min(max(pct, grid_cfg.min_range_pct), grid_cfg.max_range_pct)


def plan_grid(price, range_pct, equity, grid_cfg, min_amount, min_cost, round_amount,
              lower=None, upper=None):
    """Lay out a geometric grid centred on `price` that fits within `equity` (USD).

    The level nearest the price is left empty. Levels below it get buy orders
    and levels above it get sell orders, all for the same coin amount, so each
    filled buy can be answered by a sell one level up and vice versa.

    `round_amount` rounds a coin amount down to what the exchange accepts.
    """
    budget = equity * (1 - grid_cfg.fee_reserve_pct / 100)
    if lower is None:
        lower = price * (1 - range_pct / 100)
    if upper is None:
        upper = price * (1 + range_pct / 100)
    min_step = 1 + grid_cfg.min_spacing_pct / 100

    steps = int(math.log(upper / lower) / math.log(min_step))
    steps = min(steps, grid_cfg.max_levels - 1)

    while steps >= 2:
        ratio = (upper / lower) ** (1 / steps)
        levels = [lower * ratio ** i for i in range(steps + 1)]
        center = min(range(len(levels)), key=lambda i: abs(levels[i] - price))
        buys = levels[:center]
        sells = levels[center + 1:]
        if buys and sells:
            # Each buy order locks up level * amount USD; each sell order needs
            # `amount` coins bought now at roughly `price`.
            usd_per_coin = sum(buys) + len(sells) * price
            amount = round_amount(budget / usd_per_coin)
            if amount >= min_amount and amount * lower >= min_cost:
                return GridPlan(levels, amount, center, (ratio - 1) * 100)
        steps -= 1

    raise GridError(
        f"${equity:.2f} is too little for a grid: the exchange needs at least {min_amount} coins "
        f"(${min_cost}) per order. Increase capital_usd or widen min_range_pct."
    )


def plan_hold_grid(price, lower, upper, base, quote, grid_cfg, min_amount, min_cost, round_amount):
    """Grid that sells the coins already held through its sell orders (no market
    sale). Buy levels below the price get orders only as far as cash on hand
    covers; the rest fill in as sells complete. Returns None when this doesn't
    fit (mostly cash, or too few coins) so the caller uses plan_grid instead."""
    if base * price < quote:
        return None
    min_step = 1 + grid_cfg.min_spacing_pct / 100
    steps = min(int(math.log(upper / lower) / math.log(min_step)), grid_cfg.max_levels - 1)
    if steps < 2:
        return None
    ratio = (upper / lower) ** (1 / steps)
    levels = [lower * ratio ** i for i in range(steps + 1)]
    center = min(range(len(levels)), key=lambda i: abs(levels[i] - price))
    above = list(range(center + 1, len(levels)))
    n = len(above)
    while n >= 1:
        amount = round_amount(base / n)
        if amount >= min_amount and amount * levels[center] >= min_cost:
            break
        n -= 1
    else:
        return None
    budget = quote * (1 - grid_cfg.fee_reserve_pct / 100)
    buys = []
    for i in range(center - 1, -1, -1):
        cost = levels[i] * amount
        if cost > budget:
            break
        budget -= cost
        buys.append(i)
    return GridPlan(levels, amount, center, (ratio - 1) * 100,
                    buy_list=sorted(buys), sell_list=above[:n], held=True)
