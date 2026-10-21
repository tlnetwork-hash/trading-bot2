"""Manual buy/sell commands and the kill switch that sells to cash."""

from bot.remote import parse


def test_kill_switch_can_sell_to_cash(make_bot, cfg):
    cfg.risk.sell_on_breakout_down = False
    cfg.risk.kill_switch_sells = True
    bot, kraken, notifier = make_bot()
    bot.tick()
    kraken.px = 135
    bot.tick()
    kraken.px = 100
    bot.tick()
    assert bot.state["status"] == "halted"
    assert bot.state["balances"]["base"] < 0.02   # coins sold
    body = dict(notifier.sent)["KILL SWITCH - bot halted"]
    assert "coins were sold" in body


def test_manual_sell_all_pauses_and_sells(make_bot, cfg):
    bot, kraken, notifier = make_bot()
    bot.tick()
    assert bot.state["status"] == "running" and bot.state["balances"]["base"] > 0
    cfg.command_path.write_text("sell all")
    bot.tick()
    st = bot.state
    assert st["status"] == "paused" and st["orders"] == {} and st["grid"] is None
    assert st["balances"]["base"] < 0.02
    assert any(s.startswith("Manual SELL done") for s in notifier.subjects())
    # Stays paused until you resume, and resume rebuilds the grid.
    bot.tick()
    assert bot.state["status"] == "paused"
    cfg.command_path.write_text("resume")
    bot.tick()
    assert bot.state["status"] == "running" and bot.state["grid"] is not None


def test_manual_buy_dollar_amount(make_bot, cfg):
    bot, kraken, notifier = make_bot()
    bot.tick()
    cfg.command_path.write_text("sell all")
    bot.tick()
    cash = bot.state["balances"]["quote"]
    cfg.command_path.write_text("buy 30")
    bot.tick()
    b = bot.state["balances"]
    assert 29 < cash - b["quote"] < 30.1
    assert abs(b["base"] * 150 - 30) < 1
    assert bot.state["status"] == "paused"


def test_manual_buy_never_spends_more_than_the_bot_has(make_bot, cfg):
    bot, kraken, _ = make_bot()
    bot.tick()
    cfg.command_path.write_text("buy 100000")
    bot.tick()
    assert bot.state["balances"]["quote"] >= 0


def test_manual_trade_too_small_is_refused(make_bot, cfg):
    bot, kraken, notifier = make_bot()
    bot.tick()
    cfg.command_path.write_text("sell all")
    bot.tick()
    cfg.command_path.write_text("sell 5")   # nothing left to sell
    bot.tick()
    assert "Manual sell too small" in notifier.subjects()


def test_phone_parse_trade_commands():
    assert parse("1234 buy 10", "1234", "binanceus").command == "buy 10"
    assert parse("1234 sell all", "1234", "binanceus").command == "sell all"
    assert parse("1234 buy $12.50 live", "1234", "binanceus").command == "buy 12.50"
    assert parse("1234 sell all kraken", "1234", "binanceus").for_me is False
    assert parse("1234 buy", "1234", "binanceus").error
    assert parse("1234 buy lots", "1234", "binanceus").error
    assert parse("1234 pause live", "1234", "kraken").command == "pause"
