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
