import pytest

from bot.state import load_state


def levels_with(bot, side):
    return sorted(int(k) for k, o in bot.state["orders"].items() if o["side"] == side)


def test_first_tick_builds_grid_and_buys_inventory(make_bot):
    bot, kraken, notifier = make_bot()
    bot.tick()
    st = bot.state
    grid = st["grid"]
    assert grid is not None and st["status"] == "running"
    sells = levels_with(bot, "sell")
    buys = levels_with(bot, "buy")
    assert buys == list(range(grid["center_index"]))
    assert sells == list(range(grid["center_index"] + 1, len(grid["levels"])))
    # Holds exactly enough coins for the sell orders (plus a hair of rounding).
    assert st["balances"]["base"] == pytest.approx(len(sells) * grid["amount"], rel=0.002)
    assert st["balances"]["quote"] > 0
    assert "Grid built" in notifier.subjects()
    assert bot.equity(150) == pytest.approx(400, rel=0.01)  # only fees lost


def test_buy_fill_places_sell_above_and_round_trip_profits(make_bot):
    bot, kraken, notifier = make_bot()
    bot.tick()
    grid = bot.state["grid"]
    c = grid["center_index"]
    start_equity = bot.equity(150)

    kraken.px = grid["levels"][c - 1] - 0.01   # dip fills the buy just below centre
    bot.tick()
    assert bot.state["stats"]["buys"] == 1
    assert bot.state["orders"][str(c)]["side"] == "sell"
    assert str(c - 1) not in bot.state["orders"]

    kraken.px = grid["levels"][c] + 0.01        # bounce fills the new sell
    bot.tick()
    stats = bot.state["stats"]
    assert stats["sells"] == 1 and stats["round_trips"] == 1
    assert stats["grid_profit"] > 0
    assert bot.state["orders"][str(c - 1)]["side"] == "buy"
    assert str(c) not in bot.state["orders"]

    kraken.px = 150
    assert bot.equity(150) > start_equity
    assert any(s.startswith("BUY filled") for s in notifier.subjects())
    assert any(s.startswith("SELL filled") for s in notifier.subjects())


def test_one_level_always_stays_empty(make_bot):
    bot, kraken, _ = make_bot()
    bot.tick()
    n = len(bot.state["grid"]["levels"])
    for px in [146, 144, 147, 152, 155, 149, 143, 150]:
        kraken.px = px
        bot.tick()
        assert len(bot.state["orders"]) == n - 1


def test_breakout_pauses_and_cancels(make_bot):
    bot, kraken, notifier = make_bot()
    bot.tick()
    kraken.px = 137   # just below the grid
    bot.tick()
    st = bot.state
    assert st["status"] == "paused"
    assert st["orders"] == {} and st["grid"] is None
    assert "below" in st["status_reason"]
    assert "Bot paused" in notifier.subjects()
    # Every buy filled on the way down, so all the money is now in coins.
    assert st["balances"]["base"] > 2


def test_kill_switch_halts(make_bot):
    bot, kraken, notifier = make_bot()
    bot.tick()
    kraken.px = 135   # breaks out first -> paused while holding coins
    bot.tick()
    assert bot.state["status"] == "paused"
    kraken.px = 100   # keeps falling -> equity drops more than 10%
    bot.tick()
    assert bot.state["status"] == "halted"
    assert "KILL SWITCH - bot halted" in notifier.subjects()
    kraken.px = 90
    bot.tick()  # stays halted, does nothing
    assert bot.state["status"] == "halted" and bot.state["orders"] == {}


def test_resume_after_kill_switch_resets_loss_limit(make_bot, cfg):
    bot, kraken, _ = make_bot()
    bot.tick()
    kraken.px = 135
    bot.tick()
    kraken.px = 100
    bot.tick()
    cfg.command_path.write_text("resume")
    bot.tick()
    assert bot.state["status"] == "running"
    assert bot.state["grid"]["created_price"] == 100
    assert bot.state["start_equity"] == pytest.approx(bot.equity(100), rel=0.01)
    assert not cfg.command_path.exists()


def test_recenter_mode_rebuilds_instead_of_pausing(make_bot, cfg):
    cfg.risk.breakout_action = "recenter"
    bot, kraken, notifier = make_bot()
    bot.tick()
    kraken.px = 175
    bot.tick()
    assert bot.state["status"] == "running"
    assert bot.state["grid"]["created_price"] == 175
    assert "Grid recentering" in notifier.subjects()


def test_pause_and_reset_commands(make_bot, cfg):
    bot, kraken, notifier = make_bot()
    bot.tick()
    cfg.command_path.write_text("pause")
    bot.tick()
    assert bot.state["status"] == "paused" and bot.state["orders"] == {}

    cfg.command_path.write_text("reset")
    bot.tick()
    assert "Bot reset" in notifier.subjects()
    # Reset sells the coins and the next tick starts a fresh grid with full capital.
    assert bot.state["status"] == "running"
    assert bot.state["start_equity"] == cfg.capital_usd
    assert bot.ex.book is bot.state["paper"]


def test_state_survives_restart(make_bot, cfg):
    bot, kraken, _ = make_bot()
    bot.tick()
    saved = load_state(cfg.state_path)
    assert saved["orders"] == bot.state["orders"]

    bot2, kraken2, _ = make_bot(state=saved)
    c = saved["grid"]["center_index"]
    kraken2.px = saved["grid"]["levels"][c - 1] - 0.01
    bot2.tick()
    assert bot2.state["stats"]["buys"] == 1
    assert bot2.state["grid"]["created_price"] == 150  # did not rebuild


def test_grid_failure_pauses_with_reason(make_bot, cfg):
    cfg.capital_usd = 3
    bot, kraken, notifier = make_bot()
    bot.tick()
    assert bot.state["status"] == "paused"
    assert "too little" in bot.state["status_reason"]


def test_summary_mentions_key_numbers(make_bot):
    bot, kraken, _ = make_bot()
    bot.tick()
    text = bot.summary(150)
    assert "PAPER" in text and "Kill switch at: $360.00" in text


def test_swing_across_levels_within_one_poll_keeps_grid_whole(make_bot):
    bot, kraken, _ = make_bot()
    bot.tick()
    grid = bot.state["grid"]
    c, n = grid["center_index"], len(grid["levels"])
    # Price dips through the buy below centre and rallies through the sell
    # above it between two polls.
    bot.ex.simulate(grid["levels"][c - 1] - 0.01)
    kraken.px = grid["levels"][c + 1] + 0.01
    bot.tick()
    bot.tick()
    assert levels_with(bot, "buy") == list(range(c + 1))
    assert levels_with(bot, "sell") == list(range(c + 2, n))
    assert bot.state["deferred"] == []
    assert bot.state["stats"]["round_trips"] == 2


def test_gap_down_through_many_levels_places_every_counter_sell(make_bot):
    bot, kraken, _ = make_bot()
    bot.tick()
    grid = bot.state["grid"]
    c = grid["center_index"]
    kraken.px = grid["levels"][1] - 0.01   # fills buys c-1 .. 1 in one poll
    bot.tick()
    assert levels_with(bot, "buy") == [0]
    assert levels_with(bot, "sell") == list(range(2, len(grid["levels"])))
    assert bot.state["stats"]["buys"] == c - 1
