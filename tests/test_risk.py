import json
from datetime import datetime, timedelta, timezone

import pytest

from bot import risk


def write_risk(path, level, minutes_ago=0):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    path.write_text(json.dumps({"t": t.isoformat(timespec="seconds"), "level": level, "score": 0}))


@pytest.fixture
def risk_file(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setattr(risk, "RISK_FILE", path)
    monkeypatch.setattr(risk.current_level, "__defaults__", (path,))
    return path


def buys(bot):
    return [k for k, o in bot.state["orders"].items() if o["side"] == "buy"]


def test_levels_and_hysteresis():
    assert risk.level_for(0, "normal") == "normal"
    assert risk.level_for(3, "normal") == "elevated"
    assert risk.level_for(5, "normal") == "high"
    assert risk.level_for(4, "high") == "high"      # stays high until 3 or less
    assert risk.level_for(3, "high") == "elevated"


def test_missing_or_stale_file_is_normal(risk_file):
    assert risk.current_level() == "normal"
    write_risk(risk_file, "high", minutes_ago=120)
    assert risk.current_level() == "normal"
    write_risk(risk_file, "high")
    assert risk.current_level() == "high"


def test_guard_off_by_default_ignores_risk(make_bot, risk_file):
    write_risk(risk_file, "high")
    bot, _, _ = make_bot()
    bot.tick()
    assert bot.state["grid"] is not None and buys(bot)


def test_guard_holds_buys_and_restores_them(make_bot, cfg, risk_file):
    cfg.risk.guard = True
    bot, kraken, notifier = make_bot()
    bot.tick()
    before = sorted(buys(bot))
    sells = sorted(k for k, o in bot.state["orders"].items() if o["side"] == "sell")
    assert before

    write_risk(risk_file, "high")
    bot.tick()
    assert buys(bot) == []
    assert sorted(k for k, o in bot.state["orders"].items() if o["side"] == "sell") == sells
    assert "Risk guard ON" in notifier.subjects()

    # A sell filling while HIGH: its counter-buy is held, not placed.
    grid = bot.state["grid"]
    c = grid["center_index"]
    kraken.px = grid["levels"][c + 1] + 0.01
    bot.tick()
    assert buys(bot) == []
    assert any(h["index"] == c for h in bot.state["risk_held"])

    write_risk(risk_file, "normal")
    bot.tick()
    assert str(c) in buys(bot)
    assert set(before) <= set(buys(bot))
    assert "Risk guard OFF" in notifier.subjects()


def test_guard_waits_to_build_grid(make_bot, cfg, risk_file):
    cfg.risk.guard = True
    write_risk(risk_file, "high")
    bot, _, _ = make_bot()
    bot.tick()
    assert bot.state["grid"] is None and bot.state["status"] == "running"
    write_risk(risk_file, "normal")
    bot.tick()
    assert bot.state["grid"] is not None and buys(bot)


class FakeClient:
    def __init__(self, hourly, daily):
        self.h, self.d = hourly, daily

    def fetch_ohlcv(self, symbol, tf, limit):
        return self.h if tf == "1h" else self.d


def test_score_flags_sharp_drop_and_event():
    now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    hourly = [[i, 100, 100.5, 99.5, 100, 1] for i in range(190)]
    hourly += [[i, 92, 92.5, 91.5, 92, 1] for i in range(4)]   # -8% in the last few hours
    daily = [[0, 100, 100, 100, 100, 1]] * 56
    events = [("FOMC", datetime(2026, 10, 28, 18, tzinfo=timezone.utc), 12, 6, 4)]
    pts, reasons, _ = risk.score(FakeClient(hourly, daily), now=now, fng=50, events=events)
    assert any("FOMC" in r for r in reasons)
    assert any("24h" in r for r in reasons) and any("4h" in r for r in reasons)
    assert risk.level_for(pts, "normal") == "high"


def test_slide_guard_holds_buys_while_price_slides(make_bot, cfg, monkeypatch):
    from bot import trader
    monkeypatch.setattr(trader, "SLIDE_CHECK_SECONDS", 0)
    cfg.risk.slide_guard_pct = 1.0
    cfg.risk.slide_guard_hours = 12
    bot, kraken, notifier = make_bot()
    bot.tick()
    assert bot.state["grid"] is not None and buys(bot)

    # Price 150 twelve hours ago, 147 now: down 2% -> buys held, sells kept.
    flat = kraken.fetch_ohlcv
    kraken.fetch_ohlcv = lambda s, tf, limit: (
        [[i, 150, 150.5, 149.5, 150, 1] for i in range(limit - 1)] + [[limit, 147, 147.5, 146.5, 147, 1]])
    kraken.px = 147.0
    bot.tick()
    assert buys(bot) == []
    assert any(o["side"] == "sell" for o in bot.state["orders"].values())
    assert "Risk guard ON" in notifier.subjects()
    assert "slide guard" in dict(notifier.sent)["Risk guard ON"]

    # Price steady at 147 for the last 12 hours: slide over -> buys come back.
    kraken.fetch_ohlcv = lambda s, tf, limit: [[i, 147, 147.5, 146.5, 147, 1] for i in range(limit)]
    bot.tick()
    assert buys(bot)
    assert "Risk guard OFF" in notifier.subjects()
    kraken.fetch_ohlcv = flat


def test_slide_guard_off_by_default(make_bot, monkeypatch):
    from bot import trader
    monkeypatch.setattr(trader, "SLIDE_CHECK_SECONDS", 0)
    bot, kraken, _ = make_bot()
    bot.tick()
    kraken.fetch_ohlcv = lambda s, tf, limit: (
        [[i, 150, 150.5, 149.5, 150, 1] for i in range(limit - 1)] + [[limit, 147, 147.5, 146.5, 147, 1]])
    kraken.px = 147.0
    bot.tick()
    assert buys(bot)
