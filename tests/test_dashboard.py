import pytest
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from bot.dashboard import make_handler, snapshot
from bot.state import read_jsonl_tail


def run_some_trades(make_bot):
    bot, kraken, _ = make_bot()
    bot.tick()
    grid = bot.state["grid"]
    c = grid["center_index"]
    kraken.px = grid["levels"][c - 1] - 0.01
    bot.tick()
    kraken.px = grid["levels"][c] + 0.01
    bot.tick()
    return bot


def test_trade_and_history_logs(make_bot, cfg):
    bot = run_some_trades(make_bot)
    trades = read_jsonl_tail(cfg.trades_path, 50)
    assert [t["kind"] for t in trades] == ["rebalance", "grid", "grid"]
    assert trades[1]["side"] == "buy" and trades[1]["profit"] is None
    assert trades[2]["side"] == "sell" and trades[2]["profit"] > 0
    history = read_jsonl_tail(cfg.history_path, 50)
    assert len(history) == 1  # one point per 5 minutes
    assert history[0]["equity"] > 390


def test_jsonl_skips_truncated_line(tmp_path):
    path = tmp_path / "x.jsonl"
    path.write_text('{"a": 1}\n{"a": 2\n')
    assert read_jsonl_tail(path, 10) == [{"a": 1}]


def test_snapshot(make_bot, cfg):
    bot = run_some_trades(make_bot)
    snap = snapshot(cfg)
    assert snap["status"] == "running" and snap["mode"] == "paper"
    assert snap["exchange"] == "kraken" and snap["exchange_name"] == "Kraken"
    assert snap["service"] == "bot" and snap["links"] == []
    assert snap["chart_exchange"] == "kraken" and snap["chart_exchange_name"] == "Kraken"
    lock = snap["profit_lock"]
    assert lock["base"] == 400 and lock["arm_at"] == pytest.approx(408) and lock["keep_pct"] == 50
    assert snap["sell_on_breakout_down"] is True
    assert snap["kill_floor"] == 360
    assert snap["equity"] == bot.equity(bot.state["last_price"])
    assert len(snap["orders"]) == len(snap["grid"]["levels"]) - 1
    assert [o["index"] for o in snap["orders"]] == sorted(o["index"] for o in snap["orders"])
    assert len(snap["trades"]) == 3
    assert snap["updated_seconds_ago"] < 60


def test_snapshot_without_state(cfg):
    assert "error" in snapshot(cfg)


def test_http_server(make_bot, cfg):
    run_some_trades(make_bot)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "<title>Grid Bot</title>" in page
        api = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert api["symbol"] == "SOL/USD"
        try:
            urllib.request.urlopen(base + "/../config.yaml")
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        server.shutdown()


def test_candle_source_caches_and_survives_errors(monkeypatch):
    from bot import dashboard
    calls = []

    def fetch(tf, n):
        calls.append((tf, n))
        if len(calls) == 2:
            raise RuntimeError("kraken down")
        return [[i * 60000, 1, 2, 0.5, 1.5, 10] for i in range(n + 5)]

    clock = [1000.0]
    monkeypatch.setattr(dashboard.time, "time", lambda: clock[0])
    src = dashboard.CandleSource("SOL/USD", fetch)
    first = src.get("1h")
    assert first["timeframe"] == "1m" and len(first["candles"]) == 60
    assert src.get("1h") is first and len(calls) == 1      # cached
    clock[0] += 11
    assert src.get("1h") is first and len(calls) == 2      # fetch failed -> stale copy
    assert "error" in src.get("bogus")
    assert "error" in dashboard.CandleSource("SOL/USD", lambda tf, n: 1 / 0).get("24h")


def test_candles_endpoint(make_bot, cfg):
    from bot.dashboard import CandleSource
    run_some_trades(make_bot)
    src = CandleSource("SOL/USD", lambda tf, n: [[i, 1, 2, 0.5, 1.5, 1] for i in range(n)])
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, src))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        body = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/api/candles?range=7d").read())
        assert body["timeframe"] == "1h" and len(body["candles"]) == 168
    finally:
        server.shutdown()


def test_watchdog_alerts_once_and_on_recovery(make_bot, cfg):
    import os
    from bot.dashboard import Watchdog
    from tests.conftest import FakeNotifier
    bot, _, _ = make_bot()
    bot.tick()
    mtime = os.path.getmtime(cfg.state_path)
    clock = [mtime + 60]
    notifier = FakeNotifier()
    dog = Watchdog(cfg, notifier, clock=lambda: clock[0])
    dog.check()
    assert notifier.sent == []
    clock[0] = mtime + 400
    dog.check(); dog.check()
    assert notifier.subjects() == ["Bot stopped updating"]
    assert notifier.priorities["Bot stopped updating"] == "urgent"
    bot.tick()  # bot saves again
    clock[0] = os.path.getmtime(cfg.state_path) + 5
    dog.check()
    assert notifier.subjects() == ["Bot stopped updating", "Bot is running again"]


def test_trade_alerts_are_quiet_and_kill_switch_is_urgent(make_bot, cfg):
    cfg.risk.sell_on_breakout_down = False
    bot, kraken, notifier = make_bot()
    bot.tick()
    kraken.px = 135
    bot.tick()
    kraken.px = 100
    bot.tick()
    p = notifier.priorities
    assert any(v == "low" for k, v in p.items() if k.startswith("BUY filled"))
    assert p["Bot paused"] == "high"
    assert p["KILL SWITCH - bot halted"] == "urgent"
