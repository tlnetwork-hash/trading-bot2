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
