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

COMMANDS = ("status", "pause", "resume", "help")
# Words that name each bot (its ccxt exchange id). "all" or nothing means every bot.
BOT_NAMES = {
    "kraken": "kraken", "k": "kraken",
    "binance": "binanceus", "binanceus": "binanceus", "binance.us": "binanceus", "b": "binanceus",
}
HELP = (
    "Send: <PIN> <command> [bot]\n"
    "Commands: status, pause, resume, help\n"
    "Bot: kraken or binance (leave out for both)\n"
    "Example: 1234 pause kraken"
)


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
    command, target = words[1], (words[2] if len(words) > 2 else "all")
    if command not in COMMANDS:
        return Parsed(error=f"unknown command {command!r}")
    if target != "all" and target not in BOT_NAMES:
        return Parsed(error=f"unknown bot {target!r}")
    if target != "all" and BOT_NAMES[target] != exchange:
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
