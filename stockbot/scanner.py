"""Market scanner: finds stocks that are moving today, with heavy volume,
and are still pushing higher.

1. Build a list to look at: Alpaca's top % gainers, the most-traded stocks,
   and any stock with strong news from the news scanner.
2. Filter: price range, % up today, relative volume, dollar volume, spread,
   above VWAP (the average price paid today), close to today's high, no bad news.
3. Confirm: the price is breaking above the last few 5-minute bars (it is
   moving up right now, not fading).
4. Rank what's left by strength of move, volume and news.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    symbol: str
    price: float = 0.0
    gain_pct: float = 0.0
    rel_volume: float = 0.0
    dollar_volume: float = 0.0
    spread_pct: float = 0.0
    vwap: float = 0.0
    day_high: float = 0.0
    news_score: float = 0.0
    headline: str = ""
    breakout: bool = False
    score: float = 0.0
    rejected: str = ""            # why it was skipped ("" = it passed)
    sources: list = field(default_factory=list)

    def line(self):
        news = f"  news {self.news_score:+.1f}: {self.headline[:70]}" if self.headline else ""
        return (f"{self.symbol:<6} ${self.price:>8.2f}  {self.gain_pct:+6.1f}%  "
                f"rvol {self.rel_volume:4.1f}x  ${self.dollar_volume / 1e6:6.1f}M{news}")


def session_fraction(now, open_at, close_at):
    """How much of today's trading session has passed (0.05 - 1.0)."""
    if not open_at or not close_at:
        return 1.0
    total = (close_at - open_at).total_seconds()
    done = (now - open_at).total_seconds()
    return min(1.0, max(0.05, done / total))


class MarketScanner:
    def __init__(self, client, cfg, news_cfg):
        self.client = client
        self.cfg = cfg
        self.news_cfg = news_cfg

    def universe(self, news_map):
        """Symbols worth a look, with where each came from."""
        found = {}
        movers = self.client.movers(self.cfg.universe_size)
        for m in movers.get("gainers") or []:
            found.setdefault(m["symbol"], []).append("top gainer")
        for m in self.client.most_active(self.cfg.universe_size):
            found.setdefault(m["symbol"], []).append("most active")
        for sym, n in (news_map or {}).items():
            if n.score >= self.news_cfg.alert_min_score:
                found.setdefault(sym, []).append("news")
        exclude = set(self.cfg.exclude)
        # Plain common-stock tickers only (no warrants/units like ABCDW, BRK.B is fine to skip).
        return {s: src for s, src in found.items()
                if s not in exclude and s.isalpha() and len(s) <= 5}

    def scan(self, news_map=None, now=None, open_at=None, close_at=None):
        """Every candidate looked at, best first. Passed ones have rejected == ""."""
        now = now or datetime.now(timezone.utc)
        news_map = news_map or {}
        universe = self.universe(news_map)
        if not universe:
            return []
        snaps = self.client.snapshots(universe)
        avg_volume = self._average_volumes(list(snaps), now)
        fraction = session_fraction(now, open_at, close_at)

        candidates = []
        for sym, snap in snaps.items():
            c = self._evaluate(sym, snap, avg_volume.get(sym), fraction, news_map.get(sym))
            c.sources = universe.get(sym, [])
            candidates.append(c)

        passed = [c for c in candidates if not c.rejected]
        self._confirm_breakouts(passed, now)
        for c in candidates:
            if not c.rejected:
                c.score = round(c.gain_pct / 5 + min(c.rel_volume, 10)
                                + self.news_cfg.news_weight * max(c.news_score, 0), 2)
        candidates.sort(key=lambda c: (c.rejected != "", -c.score, -c.gain_pct))
        return candidates

    def _average_volumes(self, symbols, now):
        """Average daily volume over about the last 10 sessions (not counting today)."""
        if not symbols:
            return {}
        start = (now - timedelta(days=20)).strftime("%Y-%m-%d")
        today = now.strftime("%Y-%m-%d")
        try:
            bars = self.client.bars(symbols, "1Day", start)
        except Exception as e:
            log.warning("could not load daily volumes: %s", e)
            return {}
        out = {}
        for sym, rows in bars.items():
            past = [r["v"] for r in rows if not r["t"].startswith(today)][-10:]
            if past:
                out[sym] = sum(past) / len(past)
        return out

    def _evaluate(self, sym, snap, avg_volume, fraction, news):
        cfg = self.cfg
        c = Candidate(sym)
        day, prev = snap.get("dailyBar") or {}, snap.get("prevDailyBar") or {}
        trade, quote = snap.get("latestTrade") or {}, snap.get("latestQuote") or {}
        c.price = float(trade.get("p") or day.get("c") or 0)
        prev_close = float(prev.get("c") or 0)
        volume = float(day.get("v") or 0)
        c.vwap = float(day.get("vw") or 0)
        c.day_high = max(float(day.get("h") or 0), c.price)
        c.dollar_volume = volume * (c.vwap or c.price)
        if news:
            c.news_score = news.score
            c.headline = news.best.headline if news.best else ""
        if c.price <= 0 or prev_close <= 0:
            c.rejected = "no price data"
            return c
        c.gain_pct = round((c.price / prev_close - 1) * 100, 2)
        expected = (avg_volume or float(prev.get("v") or 0)) * fraction
        c.rel_volume = round(volume / expected, 2) if expected > 0 else 0.0
        bid, ask = float(quote.get("bp") or 0), float(quote.get("ap") or 0)
        c.spread_pct = round((ask - bid) / ask * 100, 2) if bid > 0 and ask >= bid else 99.0

        if not cfg.min_price <= c.price <= cfg.max_price:
            c.rejected = f"price ${c.price:.2f} outside ${cfg.min_price:g}-${cfg.max_price:g}"
        elif c.gain_pct < cfg.min_gain_pct:
            c.rejected = f"only {c.gain_pct:+.1f}% today"
        elif c.gain_pct > cfg.max_gain_pct:
            c.rejected = f"up {c.gain_pct:.0f}%, too extended"
        elif c.rel_volume < cfg.min_relative_volume:
            c.rejected = f"volume only {c.rel_volume:.1f}x normal"
        elif c.dollar_volume < cfg.min_dollar_volume:
            c.rejected = f"only ${c.dollar_volume / 1e6:.2f}M traded"
        elif c.spread_pct > cfg.max_spread_pct:
            c.rejected = f"spread {c.spread_pct:.2f}% too wide"
        elif c.vwap and c.price < c.vwap:
            c.rejected = "below VWAP (sellers in control)"
        elif c.price < c.day_high * (1 - cfg.near_high_pct / 100):
            c.rejected = f"fading: {(1 - c.price / c.day_high) * 100:.1f}% off the high"
        elif news and news.score < self.news_cfg.min_score_to_buy:
            c.rejected = f"bad news ({news.score:+.1f})"
        elif self.news_cfg.require_news and not (news and news.score > 0):
            c.rejected = "no fresh good news"
        return c

    def _confirm_breakouts(self, candidates, now):
        """Keep stocks whose price is above the highs of the last few 5-minute bars."""
        if not candidates:
            return
        start = (now - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            bars = self.client.bars([c.symbol for c in candidates], "5Min", start)
        except Exception as e:
            log.warning("could not load 5-minute bars: %s", e)
            for c in candidates:
                c.rejected = "no intraday bars"
            return
        n = self.cfg.breakout_bars
        for c in candidates:
            rows = bars.get(c.symbol) or []
            # The last bar is still forming; compare against the n before it.
            done = rows[:-1][-n:]
            if len(done) < n:
                c.rejected = "not enough intraday bars yet"
                continue
            level = max(float(r["h"]) for r in done)
            c.breakout = c.price > level
            if not c.breakout:
                c.rejected = f"not breaking out (needs > ${level:.2f})"
