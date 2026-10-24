"""Market risk monitor ("risk dial").

A separate container runs `python -m bot risk`. Every few minutes it scores how
risky the market looks right now and writes the result to risk.json. Bots with
risk.guard: true read that file and get more careful while the level is HIGH:

  grid bots   hold off on NEW buy orders (open buys are cancelled and put back
              once risk drops); sell orders keep working, and no new grid is
              built while HIGH.
  trend bot   skips new buys while HIGH and re-checks once risk drops.

Nothing is ever sold because of this. If risk.json is missing or older than
STALE_MINUTES, bots treat risk as normal (they never freeze on a broken monitor).

Points (calendar dates live in risk-calendar.yaml next to config.yaml):
  Fed rate decision: 12h before to 6h after          +4
  CPI release:        6h before to 3h after           +3
  BTC 24h change  <= -5%  +2,  <= -8%  +3
  BTC 4h change   <= -3%  +2
  Volatility (1h ATR) >= 1.5x its 7-day norm  +1,  >= 2x  +2
  BTC daily close below its 50-day average    +1
  Fear & Greed index <= 30  +1,  <= 20  +2

  0-2 NORMAL, 3-4 ELEVATED (alert only), 5+ HIGH (guard on).
  Once HIGH, it stays HIGH until the score is 3 or less (stops flip-flopping).
"""

import json
import logging
import os
import statistics
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

RISK_FILE = Path(os.environ.get("RISK_FILE", "/app/risk/risk.json"))
CALENDAR_FILE = Path(os.environ.get("RISK_CALENDAR", "/app/risk-calendar.yaml"))
CHECK_SECONDS = 600
STALE_MINUTES = 45
HIGH_AT, ELEVATED_AT, HIGH_EXIT_AT = 5, 3, 3
FNG_URL = "https://api.alternative.me/fng/?limit=1"


def _now():
    return datetime.now(timezone.utc)


# ------------------------------------------------------------- reading (bots)

def current_level(path=RISK_FILE):
    """Level the bots should act on: 'high', 'elevated' or 'normal'."""
    try:
        data = json.loads(Path(path).read_text())
        at = datetime.fromisoformat(data["t"])
        if _now() - at > timedelta(minutes=STALE_MINUTES):
            return "normal"
        return data.get("level", "normal")
    except Exception:
        return "normal"


def read(path=RISK_FILE):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


# ------------------------------------------------------------ scoring (monitor)

def _events(path=CALENDAR_FILE):
    try:
        raw = yaml.safe_load(Path(path).read_text()) or {}
    except FileNotFoundError:
        return []
    out = []
    for kind, before_h, after_h, pts in (("fomc", 12, 6, 4), ("cpi", 6, 3, 3)):
        for ts in raw.get(kind) or []:
            at = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            out.append((kind.upper(), at, before_h, after_h, pts))
    return out


def _atr(rows):
    trs = []
    for prev, cur in zip(rows, rows[1:]):
        h, l, pc = cur[2], cur[3], prev[4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return trs


def score(client, now=None, fng=None, events=None):
    """Returns (points, reasons, facts). client is a ccxt client."""
    now = now or _now()
    pts, reasons, facts = 0, [], {}

    for name, at, before_h, after_h, p in (events if events is not None else _events()):
        if at - timedelta(hours=before_h) <= now <= at + timedelta(hours=after_h):
            pts += p
            reasons.append(f"{name} release {at:%b %d %H:%M} UTC (+{p})")
            break

    hourly = client.fetch_ohlcv("BTC/USD", "1h", limit=24 * 8 + 2)
    price = float(hourly[-1][4])
    facts["btc"] = price
    if len(hourly) >= 25:
        ch24 = (price / hourly[-25][4] - 1) * 100
        facts["btc_24h_pct"] = round(ch24, 2)
        if ch24 <= -8:
            pts += 3; reasons.append(f"BTC {ch24:+.1f}% in 24h (+3)")
        elif ch24 <= -5:
            pts += 2; reasons.append(f"BTC {ch24:+.1f}% in 24h (+2)")
    if len(hourly) >= 5:
        ch4 = (price / hourly[-5][4] - 1) * 100
        facts["btc_4h_pct"] = round(ch4, 2)
        if ch4 <= -3:
            pts += 2; reasons.append(f"BTC {ch4:+.1f}% in 4h (+2)")

    trs = _atr(hourly)
    if len(trs) >= 60:
        recent = sum(trs[-14:]) / 14
        norm = statistics.median(trs[:-14])
        ratio = recent / norm if norm else 0
        facts["vol_ratio"] = round(ratio, 2)
        if ratio >= 2:
            pts += 2; reasons.append(f"volatility {ratio:.1f}x normal (+2)")
        elif ratio >= 1.5:
            pts += 1; reasons.append(f"volatility {ratio:.1f}x normal (+1)")

    daily = client.fetch_ohlcv("BTC/USD", "1d", limit=56)
    today_ms = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    closes = [float(r[4]) for r in daily if r[0] < today_ms]
    if len(closes) >= 50:
        sma = sum(closes[-50:]) / 50
        facts["sma50"] = round(sma, 2)
        if closes[-1] < sma:
            pts += 1; reasons.append(f"BTC below 50-day average ${sma:,.0f} (+1)")

    if fng is None:
        try:
            with urllib.request.urlopen(FNG_URL, timeout=10) as r:
                fng = int(json.load(r)["data"][0]["value"])
        except Exception as e:
            log.warning("fear & greed index unavailable: %s", e)
    if fng is not None:
        facts["fear_greed"] = fng
        if fng <= 20:
            pts += 2; reasons.append(f"Fear & Greed {fng} (+2)")
        elif fng <= 30:
            pts += 1; reasons.append(f"Fear & Greed {fng} (+1)")
    return pts, reasons, facts


def level_for(points, previous):
    if points >= HIGH_AT or (previous == "high" and points > HIGH_EXIT_AT):
        return "high"
    if points >= ELEVATED_AT:
        return "elevated"
    return "normal"


def run_monitor(notifier, client, stop):
    RISK_FILE.parent.mkdir(parents=True, exist_ok=True)
    prev = (read() or {}).get("level", "normal")
    while not stop.is_set():
        try:
            pts, reasons, facts = score(client)
            level = level_for(pts, prev)
            data = {"t": _now().isoformat(timespec="seconds"), "level": level,
                    "score": pts, "reasons": reasons, "facts": facts}
            tmp = RISK_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(RISK_FILE)
            log.info("risk %s (%d): %s", level.upper(), pts, "; ".join(reasons) or "nothing notable")
            if level != prev:
                what = {
                    "high": "Paper bots are holding off on new buys until this passes.",
                    "elevated": "Watching closely; bots trade normally for now.",
                    "normal": "Back to normal; paper bots trade as usual.",
                }[level]
                notifier.send(f"Market risk {level.upper()} (score {pts})",
                              f"{what}\n\n" + ("\n".join(reasons) or "No warning signs."),
                              priority="high" if level == "high" else "default")
                prev = level
        except Exception as e:
            log.exception("risk check failed: %s", e)
        stop.wait(CHECK_SECONDS)
