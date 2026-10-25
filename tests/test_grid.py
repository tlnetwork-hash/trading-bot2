import math

import pytest

from bot.config import Config, ConfigError, GridConfig, load_config
from bot.grid import GridError, average_true_range, plan_grid, range_pct_from_atr


def round8(x):
    return math.floor(x * 1e8) / 1e8


def test_atr_uses_true_range_including_gaps():
    candles = [[0, 10, 11, 9, 10, 0], [1, 12, 13, 12, 12, 0], [2, 12, 12.5, 11.5, 12, 0]]
    # Second candle gaps up: true range = 13 - prev close 10 = 3. Third: 1.
    assert average_true_range(candles, 2) == pytest.approx(2.0)


def test_atr_needs_enough_candles():
    with pytest.raises(GridError):
        average_true_range([[0, 1, 1, 1, 1, 0]] * 5, 14)


def test_range_is_clamped():
    g = GridConfig()
    assert range_pct_from_atr(100, 0.1, g) == g.min_range_pct
    assert range_pct_from_atr(100, 50, g) == g.max_range_pct
    assert range_pct_from_atr(100, 1, g) == pytest.approx(6.0)


def test_plan_fits_budget_and_spacing():
    g = GridConfig()
    plan = plan_grid(150, 6, 400, g, 0.02, 0.5, round8)
    assert 3 <= len(plan.levels) <= g.max_levels
    assert plan.spacing_pct >= g.min_spacing_pct
    assert plan.lower == pytest.approx(141) and plan.upper == pytest.approx(159)
    assert plan.levels[plan.center_index] == pytest.approx(150, rel=0.01)
    cost = sum(plan.levels[i] for i in plan.buy_indexes) * plan.amount + len(plan.sell_indexes) * plan.amount * 150
    assert cost <= 400 * (1 - g.fee_reserve_pct / 100)
    assert cost > 300  # uses most of the money


def test_plan_shrinks_grid_for_small_capital():
    plan = plan_grid(150, 6, 20, GridConfig(), 0.02, 0.5, round8)
    assert plan.amount >= 0.02
    assert len(plan.levels) < 13


def test_plan_rejects_tiny_capital():
    with pytest.raises(GridError):
        plan_grid(150, 6, 5, GridConfig(), 0.02, 0.5, round8)


def test_spacing_must_beat_fees():
    cfg = Config()
    cfg.grid.min_spacing_pct = 0.4
    with pytest.raises(ConfigError):
        cfg.validate()


def test_live_needs_keys():
    with pytest.raises(ConfigError):
        Config(mode="live").validate()


def test_load_config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("mode: paper\ncapital_usd: 250\nrisk:\n  max_drawdown_pct: 10\n")
    cfg = load_config(path, env={"GMAIL_ADDRESS": "a@gmail.com", "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"})
    assert cfg.capital_usd == 250
    assert cfg.email.enabled and cfg.email.to_address == "a@gmail.com"
    assert cfg.email.gmail_app_password == "abcdefghijklmnop"


def test_load_config_rejects_typos(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("risk:\n  max_drawdown: 10\n")
    with pytest.raises(ConfigError):
        load_config(path, env={})


def test_shipped_config_is_valid():
    cfg = load_config("config.yaml", env={})
    assert cfg.mode == "paper" and cfg.symbol == "BTC/USD"


def test_hold_grid_uses_held_coins_and_no_market_sale():
    from bot.config import GridConfig
    from bot.grid import plan_hold_grid
    g = GridConfig(min_spacing_pct=1.0, max_levels=20, fee_reserve_pct=2)
    rnd = lambda a: math.floor(a * 1000) / 1000
    plan = plan_hold_grid(101.58, 101.58 * 0.955, 101.58 * 1.045, 0.46, 1.68, g, 0.001, 1.0, rnd)
    assert plan is not None and plan.held
    assert all(plan.levels[i] > 101.58 for i in plan.sell_indexes)
    # sells never need more coins than the bot holds
    assert len(plan.sell_indexes) * plan.amount <= 0.46 + 1e-9
    # $1.68 cash can't fund a buy of ~$11, so no buys yet
    assert plan.buy_indexes == []
    assert plan.lower < 98 and plan.upper > 105


def test_hold_grid_falls_back_when_mostly_cash():
    from bot.config import GridConfig
    from bot.grid import plan_hold_grid
    g = GridConfig()
    rnd = lambda a: math.floor(a * 1000) / 1000
    assert plan_hold_grid(100, 95, 105, 0.01, 50, g, 0.001, 1.0, rnd) is None
