import pytest

from bot.config import load_config, ConfigError
from bot.remote import RemoteCommands, parse
from bot.trader import GridBot


def test_parse():
    assert parse("0627 pause kraken", "0627", "kraken").command == "pause"
    assert parse("  0627 STATUS ", "0627", "binanceus").command == "status"   # both bots
    assert parse("0627 resume binance", "0627", "binanceus").command == "resume"
    assert parse("0627 resume binance", "0627", "kraken").for_me is False
    assert parse("0627 pause all", "0627", "kraken").command == "pause"
    assert parse("1111 pause", "0627", "kraken").error == "wrong or missing PIN"
    assert parse("pause", "0627", "kraken").error == "wrong or missing PIN"
    assert parse("0627", "0627", "kraken").error == "no command given"
    assert "unknown command" in parse("0627 reset", "0627", "kraken").error   # never from the phone
    assert "unknown bot" in parse("0627 pause coinbase", "0627", "kraken").error


def test_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("mode: paper\n")
    env = {"NTFY_TOPIC": "alerts-x", "NTFY_COMMAND_TOPIC": "cmd-y", "NTFY_COMMAND_PIN": "0627"}
    assert load_config(path, env=env).ntfy_command_url == "https://ntfy.sh/cmd-y"
    assert load_config(path, env={}).ntfy_command_url == ""
    for bad in ({"NTFY_COMMAND_TOPIC": "cmd-y"},
                {"NTFY_COMMAND_TOPIC": "cmd-y", "NTFY_COMMAND_PIN": "12"},
                {"NTFY_COMMAND_TOPIC": "cmd-y", "NTFY_COMMAND_PIN": "abcd"},
                {"NTFY_TOPIC": "same", "NTFY_COMMAND_TOPIC": "same", "NTFY_COMMAND_PIN": "0627"}):
        with pytest.raises(ConfigError):
            load_config(path, env=bad)


class FakeTopic:
    def __init__(self):
        self.messages, self.calls = [], []

    def send(self, text):
        self.messages.append({"id": f"m{len(self.messages)}", "event": "message", "message": text})

    def fetch(self, since):
        self.calls.append(since)
        ids = [m["id"] for m in self.messages]
        start = ids.index(since) + 1 if since in ids else 0
        return self.messages[start:]


def test_poll_remembers_where_it_got_to():
    topic, state = FakeTopic(), {}
    remote = RemoteCommands("https://ntfy.sh/c", "0627", "kraken", state, fetch=topic.fetch)
    assert remote.poll() == []
    topic.send("0627 status")
    assert [p.command for p in remote.poll()] == ["status"]
    assert remote.poll() == []           # not repeated
    assert state["remote"]["since"] == "m0"


def test_poll_survives_network_errors():
    def broken(since):
        raise OSError("ntfy down")
    remote = RemoteCommands("https://ntfy.sh/c", "0627", "kraken", {}, fetch=broken)
    assert remote.poll() == []


@pytest.fixture
def phone(make_bot, cfg):
    cfg.ntfy_command_topic, cfg.ntfy_command_pin = "cmd", "0627"
    topic = FakeTopic()

    def make(**kw):
        bot, kraken, notifier = make_bot(**kw)
        bot.remote._fetch = topic.fetch
        return bot, kraken, notifier
    return make, topic


def test_pause_and_resume_from_phone(phone):
    make, topic = phone
    bot, kraken, notifier = make()
    bot.tick()
    topic.send("0627 pause kraken")
    bot.tick()
    assert bot.state["status"] == "paused" and bot.state["status_reason"] == "paused by you"
    topic.send("0627 resume")
    bot.tick()
    assert bot.state["status"] == "running" and bot.state["grid"] is not None
    assert "Bot resuming" in notifier.subjects()


def test_status_help_and_other_bot(phone):
    make, topic = phone
    bot, _, notifier = make()
    bot.tick()
    topic.send("0627 status")
    topic.send("0627 help")
    topic.send("0627 pause binance")     # meant for the other bot
    bot.tick()
    assert bot.state["status"] == "running"
    assert any(s.startswith("Status: $") for s in notifier.subjects())
    assert "Phone commands" in notifier.subjects()


def test_wrong_pin_is_ignored_and_reported_once(phone):
    make, topic = phone
    bot, _, notifier = make()
    bot.tick()
    for _ in range(3):
        topic.send("1111 pause")
    bot.tick()
    assert bot.state["status"] == "running"
    assert notifier.subjects().count("Phone command ignored") == 1


def test_pause_from_phone_cancels_auto_restart(phone):
    make, topic = phone
    bot, kraken, notifier = make()
    bot.tick()
    kraken.px = 137                      # stop-loss sale, auto-restart pending
    bot.tick()
    assert bot.state.get("rebuy")
    topic.send("0627 pause")
    bot.tick()
    assert "rebuy" not in bot.state
    assert "Automatic restart cancelled" in notifier.subjects()
    assert "Already paused" not in notifier.subjects()
