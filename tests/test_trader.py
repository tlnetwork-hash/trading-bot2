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


def test_breakout_pauses_and_cancels(make_bot, cfg):
    cfg.risk.sell_on_breakout_down = False
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


def test_kill_switch_halts(make_bot, cfg):
    cfg.risk.sell_on_breakout_down = False
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
    cfg.risk.sell_on_breakout_down = False
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


def test_breakout_below_sells_and_pauses(make_bot):
    bot, kraken, notifier = make_bot()
    bot.tick()
    kraken.px = 137   # just below the grid, after every buy filled
    bot.tick()
    st = bot.state
    assert st["status"] == "paused" and st["orders"] == {} and st["grid"] is None
    assert st["status_reason"].startswith("stop loss")
    assert st["balances"]["base"] == pytest.approx(0, abs=1e-6)
    assert "Stop loss: sold on breakout" in notifier.subjects()
    assert notifier.priorities["Stop loss: sold on breakout"] == "high"
    kraken.px = 100   # the crash no longer hurts: the bot holds cash
    bot.tick()
    assert st["status"] == "paused"
    assert bot.equity(100) == pytest.approx(bot.equity(137))


def test_breakout_above_still_just_pauses(make_bot):
    bot, kraken, notifier = make_bot()
    bot.tick()
    kraken.px = 162
    bot.tick()
    assert bot.state["status"] == "paused"
    assert "Bot paused" in notifier.subjects()


def test_profit_lock_trails_and_sells_before_going_negative(make_bot, cfg):
    from bot.trader import profit_lock_floor
    # A grid sells as the price rises, so in this short test equity only gains
    # about 1%; arm the lock at 0.5% to exercise it.
    cfg.risk.profit_lock_start_pct = 0.5
    bot, kraken, notifier = make_bot()
    bot.tick()
    lock = bot.state["lock"]
    assert lock == {"base": 400, "peak": 400}
    assert "arms when equity reaches $402.00" in bot.summary(150)
    # Rising price: the bot gains, the lock arms and its stop follows the high.
    stops = []
    for px in (154, 156, 158):
        kraken.px = px
        bot.tick()
        stops.append(profit_lock_floor(cfg.risk, lock))
    assert bot.state["status"] == "running"
    assert stops == sorted(stops) and stops[0] is not None   # only moves up
    high = lock["peak"]
    assert high >= 400 * 1.005
    assert stops[-1] == pytest.approx(400 + (high - 400) / 2)
    assert "ARMED" in bot.summary(158)
    # Falling back: once equity reaches the stop, it sells everything.
    for px in (157, 156, 155, 154, 153, 152, 151, 150):
        kraken.px = px
        bot.tick()
        if bot.state["status"] != "running":
            break
    st = bot.state
    assert st["status"] == "paused" and st["status_reason"].startswith("profit lock")
    assert st["balances"]["base"] == pytest.approx(0, abs=1e-6)
    assert "Profit locked" in notifier.subjects()
    # It sold while still ahead of where it started.
    assert st["balances"]["quote"] > 400


def test_profit_lock_keeps_half_the_best_gain(cfg):
    from bot.trader import profit_lock_floor
    lock = {"base": 400, "peak": 407}
    assert profit_lock_floor(cfg.risk, lock) is None          # +1.75%: not armed yet
    lock["peak"] = 408                                         # +2% arms it
    assert profit_lock_floor(cfg.risk, lock) == pytest.approx(404)   # keeps +1%
    lock["peak"] = 424                                         # best gain +6%
    assert profit_lock_floor(cfg.risk, lock) == pytest.approx(412)   # keeps +3%
    cfg.risk.profit_lock_start_pct = 0
    assert profit_lock_floor(cfg.risk, lock) is None          # turned off


def test_resume_after_profit_lock_starts_a_fresh_lock(make_bot, cfg):
    cfg.risk.profit_lock_start_pct = 0.5
    bot, kraken, _ = make_bot()
    bot.tick()
    for px in (154, 156, 158, 156, 154, 152, 150):
        kraken.px = px
        bot.tick()
    assert bot.state["status"] == "paused"
    cfg.command_path.write_text("resume")
    bot.tick()
    bot.tick()
    st = bot.state
    assert st["status"] == "running" and st["grid"]["created_price"] == 150
    assert st["lock"]["base"] == pytest.approx(bot.equity(150), rel=0.01)
    assert st["start_equity"] == 400   # the kill switch still measures from the start


def test_old_state_without_lock_still_loads(make_bot, cfg):
    from bot.state import new_state
    state = new_state(cfg)
    del state["lock"]
    bot, _, _ = make_bot(state=state)
    assert bot.state["lock"] == {"base": 400, "peak": 400}


@pytest.fixture
def clock(monkeypatch):
    """Controls the time the trader sees."""
    import bot.trader as trader
    from datetime import datetime, timedelta, timezone
    now = [datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(trader, "utcnow", lambda: now[0])

    def advance(minutes):
        now[0] += timedelta(minutes=minutes)
    return advance


def sell_on_breakdown(bot, kraken):
    bot.tick()
    kraken.px = 137   # falls out of the bottom of the grid -> sells
    bot.tick()
    assert bot.state["status"] == "paused" and bot.state["rebuy"]["sold_price"] < 137


def test_auto_restart_waits_for_a_bounce_below_the_sale_price(make_bot, clock):
    bot, kraken, notifier = make_bot()
    sell_on_breakdown(bot, kraken)
    sold = bot.state["rebuy"]["sold_price"]
    # Still falling: never restarts while making new lows.
    for px in (134, 130, 127):
        kraken.px = px
        clock(30)
        bot.tick()
        assert bot.state["status"] == "paused"
    assert bot.state["rebuy"]["low"] == 127
    # A bounce under 2% isn't enough.
    kraken.px = 129
    clock(30)
    bot.tick()
    assert bot.state["status"] == "paused"
    assert "Auto-restart: waiting" in bot.summary(129)
    # 2% off the low and below where it sold: restarts with a new grid there.
    kraken.px = 130
    bot.tick()
    st = bot.state
    assert 130 < sold
    assert st["status"] == "running" and "rebuy" not in st
    assert st["grid"]["created_price"] == 130
    assert "Bot restarted: good spot to buy" in notifier.subjects()
    assert st["lock"]["base"] == pytest.approx(bot.equity(130), rel=0.01)


def test_auto_restart_respects_the_wait(make_bot, clock):
    bot, kraken, _ = make_bot()
    sell_on_breakdown(bot, kraken)
    kraken.px = 130
    clock(10)
    bot.tick()
    kraken.px = 133   # bounced, but only 20 minutes after the sale
    clock(10)
    bot.tick()
    assert bot.state["status"] == "paused"
    clock(45)         # an hour has passed and the price is still a good spot
    bot.tick()
    assert bot.state["status"] == "running"


def test_auto_restart_never_buys_back_higher_than_it_sold(make_bot, clock):
    bot, kraken, _ = make_bot()
    sell_on_breakdown(bot, kraken)
    sold = bot.state["rebuy"]["sold_price"]
    kraken.px = 145   # jumped straight back above the sale price
    clock(120)
    bot.tick()
    assert bot.state["status"] == "paused"
    kraken.px = 140   # 2% above the low of 137, but still above where it sold
    bot.tick()
    assert bot.state["status"] == "paused"
    kraken.px = 130   # a lower low...
    bot.tick()
    kraken.px = 133   # ...and a 2% bounce that is cheaper than the sale: buys again
    bot.tick()
    assert 133 < sold and bot.state["status"] == "running"


def test_pause_command_cancels_auto_restart(make_bot, cfg, clock):
    bot, kraken, notifier = make_bot()
    sell_on_breakdown(bot, kraken)
    cfg.command_path.write_text("pause")
    bot.tick()
    assert "rebuy" not in bot.state and "Automatic restart cancelled" in notifier.subjects()
    kraken.px = 120
    clock(120)
    bot.tick()
    kraken.px = 125
    bot.tick()
    assert bot.state["status"] == "paused"


def test_auto_restart_can_be_turned_off(make_bot, cfg, clock):
    cfg.risk.auto_restart = False
    bot, kraken, _ = make_bot()
    bot.tick()
    kraken.px = 137
    bot.tick()
    assert bot.state["status"] == "paused" and "rebuy" not in bot.state


def test_kill_switch_does_not_auto_restart(make_bot, cfg, clock):
    cfg.risk.sell_on_breakout_down = False
    bot, kraken, _ = make_bot()
    bot.tick()
    kraken.px = 135
    bot.tick()
    kraken.px = 100
    bot.tick()
    assert bot.state["status"] == "halted" and "rebuy" not in bot.state


def test_breakout_holds_then_resumes_at_break_even(make_bot, cfg):
    cfg.risk.sell_on_breakout_down = False
    cfg.risk.resume_at_break_even = True
    bot, kraken, notifier = make_bot()
    bot.tick()
    kraken.px = 137   # just below the grid -> pause, keep coins
    bot.tick()
    st = bot.state
    assert st["status"] == "paused" and st["resume_at_break_even"]
    assert "Will restart at break-even" in notifier.subjects()
    be = bot.break_even_price()
    kraken.px = round(be * 0.99, 2)   # still under water: stays paused
    bot.tick()
    assert bot.state["status"] == "paused"
    kraken.px = round(be * 1.001, 2)  # back to break-even: restarts
    bot.tick()
    st = bot.state
    assert st["status"] == "running" and "resume_at_break_even" not in st
    assert st["grid"] is not None
    assert "Back to break-even, restarting" in notifier.subjects()


def test_pause_command_cancels_break_even_resume(make_bot, cfg):
    cfg.risk.sell_on_breakout_down = False
    cfg.risk.resume_at_break_even = True
    bot, kraken, notifier = make_bot()
    bot.tick()
    kraken.px = 137
    bot.tick()
    bot._run_command("pause", 137)
    assert "resume_at_break_even" not in bot.state
    kraken.px = round(bot.break_even_price() * 1.01, 2)
    bot.tick()
    assert bot.state["status"] == "paused"


def test_resume_keeping_coins_skips_market_sale(make_bot, cfg):
    """Live-SOL setup: paused below the grid holding coins, then resumed with
    keep_coins_on_build + a fixed range. No market sale; held coins go into
    sells above; buys fill in as sells complete."""
    cfg.capital_usd = 50
    cfg.risk.sell_on_breakout_down = False
    cfg.risk.resume_at_break_even = True
    bot, kraken, _ = make_bot(price=105.0)
    bot.tick()
    kraken.px = 95.0
    bot.tick()  # below the grid -> paused, holding
    assert bot.state["status"] == "paused"
    base_before = bot.state["balances"]["base"]
    assert base_before * 95 > bot.state["balances"]["quote"]

    cfg.grid.keep_coins_on_build = True
    cfg.grid.range_below_pct = 4.5
    cfg.grid.range_above_pct = 4.5
    trades_before = cfg.trades_path.read_text() if cfg.trades_path.exists() else ""
    cfg.command_path.write_text("resume")
    bot.tick()
    st = bot.state
    assert st["status"] == "running"
    trades_after = cfg.trades_path.read_text()
    assert "rebalance" not in trades_after[len(trades_before):]   # no market sale
    assert st["balances"]["base"] == pytest.approx(base_before)
    sells = levels_with(bot, "sell")
    assert sells and all(st["orders"][str(i)].get("held") for i in sells)
    assert st["grid"]["lower"] == pytest.approx(95 * 0.955, rel=0.001)

    # A held sell filling doesn't count as a round trip, and queues a buy below.
    first = min(sells)
    rt = st["stats"]["round_trips"]
    kraken.px = st["grid"]["levels"][first] + 0.01
    bot.tick()
    assert st["stats"]["round_trips"] == rt
    assert first - 1 in levels_with(bot, "buy")
    # That buy then sells back up as a normal round trip.
    kraken.px = st["grid"]["levels"][first - 1] - 0.01
    bot.tick()
    kraken.px = st["grid"]["levels"][first] + 0.01
    bot.tick()
    assert st["stats"]["round_trips"] == rt + 1
