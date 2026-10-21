"""Commands from your phone through a private ntfy topic.

Send a message to the command topic (NTFY_COMMAND_TOPIC in .env) from the ntfy
app, in the form

    <PIN> <command> [bot]          e.g.  0627 pause kraken

command: status, pause, resume or help. bot: kraken, binance, or leave it out
for both. Messages without the right PIN are ignored. Selling everything
("reset") is deliberately not available from the phone.

Each bot polls the topic on every loop (about every 30 seconds), so a command
takes effect within about half a minute. Replies arrive as normal alerts.
"""

import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

COMMANDS = ("status", "pause", "resume", "help", "buy", "sell")
# Manual trades take a dollar amount or "all": "1234 buy 10", "1234 sell all".
TRADE_COMMANDS = ("buy", "sell")
# Words that name each bot (its ccxt exchange id). "all" or nothing means every bot.
# "live" is accepted by any bot; it is meant for a bot with its own command topic.
BOT_NAMES = {
    "kraken": "kraken", "k": "kraken",
    "binance": "binanceus", "binanceus": "binanceus", "binance.us": "binanceus", "b": "binanceus",
    "live": None,
}
HELP = (
    "Send: <PIN> <command> [bot]\n"
    "Commands: status, pause, resume, help,\n"
    "  buy <$ amount|all>, sell <$ amount|all>\n"
    "  (buy/sell pause the grid first; send resume to restart it)\n"
    "Bot: kraken or binance (leave out for both)\n"
    "Examples: 1234 pause kraken   1234 buy 10   1234 sell all"
)


def trade_amount_ok(text):
    """True for "all" or a positive dollar amount like 10 or 12.50."""
    if text == "all":
        return True
    try:
        return float(text.lstrip("$")) > 0
    except ValueError:
        return False


@dataclass
class Parsed:
    command: str = ""   # one of COMMANDS when the message is valid and for this bot
    error: str = ""     # why the message was rejected, if it was
    for_me: bool = True


def parse(text, pin, exchange):
    words = text.strip().lower().split()
    if not words or words[0] != pin:
        return Parsed(error="wrong or missing PIN")
    if len(words) < 2:
        return Parsed(error="no command given")
    command, rest = words[1], words[2:]
    if command not in COMMANDS:
        return Parsed(error=f"unknown command {command!r}")
    if command in TRADE_COMMANDS:
        if not rest or not trade_amount_ok(rest[0]):
            return Parsed(error=f"{command} needs a dollar amount or 'all', e.g. {command} 10")
        command = f"{command} {rest[0].lstrip('$')}"
        rest = rest[1:]
    target = rest[0] if rest else "all"
    if target != "all" and target not in BOT_NAMES:
        return Parsed(error=f"unknown bot {target!r}")
    if target != "all" and BOT_NAMES[target] not in (None, exchange):
        return Parsed(for_me=False)
    return Parsed(command=command)


class RemoteCommands:
    """Reads new messages from the ntfy command topic."""

    def __init__(self, url, pin, exchange, state, fetch=None):
        self.url = url
        self.pin = pin
        self.exchange = exchange
        # Where we got to, saved with the bot's state so a restart neither
        # replays old commands nor misses new ones. Starts at "now".
        self.state = state.setdefault("remote", {"since": str(int(time.time()))})
        self._fetch = fetch or self._fetch_ntfy

    def _fetch_ntfy(self, since):
        query = urllib.parse.urlencode({"poll": "1", "since": since})
        with urllib.request.urlopen(f"{self.url}/json?{query}", timeout=10) as response:
            lines = response.read().decode().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def poll(self):
        """Returns the new messages as Parsed commands (empty on any network trouble)."""
        try:
            messages = self._fetch(self.state["since"])
        except Exception as e:
            log.warning("could not read phone commands from ntfy: %s", e)
            return []
        results = []
        for m in messages:
            if m.get("event", "message") != "message":
                continue
            self.state["since"] = m["id"]
            parsed = parse(m.get("message", ""), self.pin, self.exchange)
            if parsed.error:
                log.warning("ignored phone command (%s)", parsed.error)
            results.append(parsed)
        return results
