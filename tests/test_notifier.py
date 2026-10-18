import io
import json
import urllib.error

import bot.notifier as notifier_mod
from bot.config import EmailSecrets, load_config
from bot.notifier import Notifier


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_method_selection():
    assert EmailSecrets().method is None
    assert EmailSecrets("a@gmail.com", "pw", "a@gmail.com").method == "gmail"
    assert EmailSecrets("a@gmail.com", "pw", "a@gmail.com", "xkeysib-1").method == "brevo"
    assert EmailSecrets("a@gmail.com", "", "a@gmail.com", "xkeysib-1").method == "brevo"
    assert EmailSecrets("", "", "", "xkeysib-1").method is None  # needs a sender address


def test_brevo_key_from_env(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("mode: paper\n")
    cfg = load_config(path, env={"GMAIL_ADDRESS": "a@gmail.com", "BREVO_API_KEY": " xkeysib-1 "})
    assert cfg.email.method == "brevo" and cfg.email.brevo_api_key == "xkeysib-1"
    assert cfg.email.to_address == "a@gmail.com"


def test_brevo_request(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["key"] = request.get_header("Api-key")
        seen["body"] = json.loads(request.data)
        return FakeResponse()

    monkeypatch.setattr(notifier_mod.urllib.request, "urlopen", fake_urlopen)
    n = Notifier(EmailSecrets("me@gmail.com", "", "you@gmail.com", "xkeysib-1"), "SOL grid PAPER")
    assert n.send("Grid built", "hello") is True
    assert seen["url"] == notifier_mod.BREVO_URL
    assert seen["key"] == "xkeysib-1"
    assert seen["body"]["sender"]["email"] == "me@gmail.com"
    assert seen["body"]["to"] == [{"email": "you@gmail.com"}]
    assert seen["body"]["subject"] == "[SOL grid PAPER] Grid built"
    assert seen["body"]["textContent"] == "hello"


def test_brevo_error_never_raises(monkeypatch, caplog):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {},
                                     io.BytesIO(b'{"message":"Key not found"}'))

    monkeypatch.setattr(notifier_mod.urllib.request, "urlopen", fake_urlopen)
    n = Notifier(EmailSecrets("me@gmail.com", "", "me@gmail.com", "bad"), "x")
    assert n.send("s", "b") is False
    assert "Key not found" in caplog.text


def test_unconfigured_returns_false():
    assert Notifier(EmailSecrets(), "x").send("s", "b") is False


def test_ntfy_request(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen.update(url=request.full_url, body=request.data.decode(),
                    title=request.get_header("Title"), priority=request.get_header("Priority"))
        return FakeResponse()

    monkeypatch.setattr(notifier_mod.urllib.request, "urlopen", fake_urlopen)
    n = Notifier(EmailSecrets(), "SOL grid paper", "https://ntfy.sh/gridbot-abc")
    assert n.channels == ["ntfy"]
    assert n.send("KILL SWITCH – halted", "details", priority="urgent") is True
    assert seen == {"url": "https://ntfy.sh/gridbot-abc", "body": "details",
                    "title": "SOL grid paper: KILL SWITCH ? halted", "priority": "urgent"}


def test_sends_to_both_channels_and_survives_one_failing(monkeypatch):
    urls = []

    def fake_urlopen(request, timeout):
        urls.append(request.full_url)
        if "brevo" in request.full_url:
            raise OSError("brevo down")
        return FakeResponse()

    monkeypatch.setattr(notifier_mod.urllib.request, "urlopen", fake_urlopen)
    n = Notifier(EmailSecrets("me@gmail.com", "", "me@gmail.com", "k"), "x", "https://ntfy.sh/t")
    assert n.channels == ["ntfy", "email"]
    assert n.send("s", "b") is True          # ntfy worked even though email failed
    assert urls == ["https://ntfy.sh/t", notifier_mod.BREVO_URL]


def test_ntfy_topic_from_env(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("mode: paper\n")
    assert load_config(path, env={}).ntfy_url == ""
    cfg = load_config(path, env={"NTFY_TOPIC": "gridbot-xyz"})
    assert cfg.ntfy_url == "https://ntfy.sh/gridbot-xyz"
    cfg = load_config(path, env={"NTFY_TOPIC": "t", "NTFY_SERVER": "https://ntfy.example.com/"})
    assert cfg.ntfy_url == "https://ntfy.example.com/t"


def test_exchange_choice_and_keys_from_env(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("mode: paper\n")
    cfg = load_config(path, env={"KRAKEN_API_KEY": "k1", "BINANCEUS_API_KEY": "b1"})
    assert cfg.exchange == "kraken" and cfg.exchange_name == "Kraken" and cfg.api_key == "k1"
    path.write_text("mode: paper\nexchange: binanceus\n")
    cfg = load_config(path, env={"KRAKEN_API_KEY": "k1", "BINANCEUS_API_KEY": "b1"})
    assert cfg.exchange_name == "Binance.US" and cfg.api_key == "b1"


def test_bad_exchange_and_live_without_keys(tmp_path):
    import pytest
    from bot.config import ConfigError
    path = tmp_path / "config.yaml"
    path.write_text("exchange: coinbase\n")
    with pytest.raises(ConfigError, match="exchange must be"):
        load_config(path, env={})
    path.write_text("mode: live\nexchange: binanceus\n")
    with pytest.raises(ConfigError, match="BINANCEUS_API_KEY"):
        load_config(path, env={"KRAKEN_API_KEY": "k", "KRAKEN_API_SECRET": "s"})


def test_dashboard_links(tmp_path):
    import pytest
    from bot.config import ConfigError
    path = tmp_path / "config.yaml"
    path.write_text("dashboard:\n  links:\n    - {label: Kraken, port: 8080}\n")
    assert load_config(path, env={}).dashboard_links == [{"label": "Kraken", "port": 8080}]
    path.write_text("dashboard:\n  links:\n    - {label: Kraken}\n")
    with pytest.raises(ConfigError, match="dashboard link"):
        load_config(path, env={})


def test_shipped_configs_load():
    from pathlib import Path
    root = Path(__file__).parent.parent
    kraken = load_config(root / "config.yaml", env={})
    binance = load_config(root / "config-binanceus.yaml", env={})
    assert (kraken.exchange, binance.exchange) == ("kraken", "binanceus")
    assert binance.fees.maker_pct == 0 and kraken.fees.maker_pct == 0.40
    assert kraken.dashboard_links == binance.dashboard_links
    assert (kraken.chart_source, binance.chart_source) == ("kraken", "kraken")


def test_chart_exchange(tmp_path):
    import pytest
    from bot.config import ConfigError
    path = tmp_path / "config.yaml"
    path.write_text("exchange: binanceus\n")
    assert load_config(path, env={}).chart_source == "binanceus"
    path.write_text("exchange: binanceus\ndashboard:\n  chart_exchange: Kraken\n")
    assert load_config(path, env={}).chart_source == "kraken"
    path.write_text("dashboard:\n  chart_exchange: nope\n")
    with pytest.raises(ConfigError, match="chart_exchange"):
        load_config(path, env={})
