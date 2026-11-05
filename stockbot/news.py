"""News scanner: reads recent headlines and scores each stock by how likely
the news is to push it up (positive score) or down (negative score).

This is simple keyword matching, not a crystal ball. It is good at spotting
the catalysts that move small and mid-sized stocks (FDA decisions, earnings
beats, buyouts, big contracts, analyst upgrades) and at keeping the bot away
from stocks with poison news (share offerings, bankruptcy, investigations).
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# (pattern, points). Patterns are matched against the lower-cased headline and
# summary. Bigger numbers = bigger expected move.
POSITIVE = [
    (r"fda (approv|grants?|clears?|clearance|accept)", 4),
    (r"(breakthrough|fast track|orphan drug) designation", 3),
    (r"(positive|successful|met|meets) (top-?line|primary endpoint|phase)", 3),
    (r"(to be acquired|agrees? to be acquired|acquired by|buyout|takeover|tender offer)", 4),
    (r"(definitive|merger) agreement", 2),
    (r"\b(beats?|tops?|surpass\w*|exceeds?)\b.{0,40}\b(estimates?|expectations|consensus|eps|revenue)", 3),
    (r"(raises?|boosts?|lifts?|increases?) (its |full-year |fy\d* |annual )?(guidance|outlook|forecast)", 3),
    (r"record (revenue|quarter|sales|earnings|profit)", 2),
    (r"\bupgrade[sd]?\b", 2),
    (r"price target (raised|increased|boosted)|raises? price target", 1),
    (r"(awarded|wins?|won|secures?|lands?) .{0,30}(contract|order|deal)", 2),
    (r"\b(partnership|collaboration|strategic alliance)\b", 1),
    (r"(share|stock) (buyback|repurchase)", 1),
    (r"(added to|join\w*) (the )?s&p 500", 3),
    (r"short squeeze", 1),
    (r"\bsoar\w*|\bsurg\w*|\bjump\w*|\brall(y|ies)\b|\bskyrocket\w*", 1),
]
NEGATIVE = [
    (r"(public|registered direct|secondary|stock|share|equity) offering|offering of .{0,30}shares", -4),
    (r"\bdilut\w*", -3),
    (r"(at-the-market|atm) (program|offering)", -3),
    (r"reverse (stock )?split", -3),
    (r"bankrupt\w*|chapter 11|going concern", -5),
    (r"delist\w*|non-?compliance notice", -3),
    (r"\bdowngrade[sd]?\b", -2),
    (r"\b(miss(es|ed)?|falls? short of)\b.{0,40}\b(estimates?|expectations|consensus|eps|revenue)", -3),
    (r"(cuts?|lowers?|slashes|withdraws?|reduces?) (its |full-year |fy\d* |annual )?(guidance|outlook|forecast)", -3),
    (r"complete response letter|fda (rejects?|declines?)|clinical hold", -4),
    (r"(failed|fails|did not meet) (to meet )?(its )?(primary )?endpoint", -4),
    (r"\b(sec|doj|ftc) (probe|investigation|charges|subpoena)|investigation into|class action|lawsuit", -2),
    (r"\brecall\w*", -2),
    (r"(ceo|cfo) (resigns|steps down|departs|fired)", -2),
    (r"\bplunge\w*|\bplummet\w*|\btumbl\w*|\bsink\w*|\bslump\w*", -1),
]
_POSITIVE = [(re.compile(p), w) for p, w in POSITIVE]
_NEGATIVE = [(re.compile(p), w) for p, w in NEGATIVE]


def score_text(text):
    """Points for one piece of text, plus the phrases that matched."""
    text = text.lower()
    total, hits = 0, []
    for rx, points in _POSITIVE + _NEGATIVE:
        m = rx.search(text)
        if m:
            total += points
            hits.append(m.group(0))
    return total, hits


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class Headline:
    headline: str
    source: str
    url: str
    created_at: datetime
    score: int
    hits: list


@dataclass
class StockNews:
    symbol: str
    score: float = 0.0
    headlines: list = field(default_factory=list)

    @property
    def best(self):
        return max(self.headlines, key=lambda h: abs(h.score), default=None)


def score_articles(articles, now=None):
    """Turn Alpaca news articles into a {symbol: StockNews} map.

    Fresh news counts more than old news, and an article about one company
    counts more than a market round-up that mentions twenty."""
    now = now or datetime.now(timezone.utc)
    result = {}
    for a in articles:
        symbols = [s.upper() for s in a.get("symbols") or [] if s and "." not in s and "/" not in s]
        if not symbols or len(symbols) > 5:
            continue  # unrelated or a broad round-up
        points, hits = score_text(f"{a.get('headline', '')}. {a.get('summary', '')}")
        try:
            created = parse_time(a.get("created_at") or a.get("updated_at"))
        except (TypeError, ValueError):
            created = now
        age = now - created
        freshness = 1.0 if age <= timedelta(hours=3) else 0.6 if age <= timedelta(hours=12) else 0.3
        weight = freshness / len(symbols)
        h = Headline(a.get("headline", "").strip(), a.get("source", ""), a.get("url", ""),
                     created, points, hits)
        for sym in symbols:
            entry = result.setdefault(sym, StockNews(sym))
            entry.score += points * weight
            entry.headlines.append(h)
    for entry in result.values():
        entry.score = round(entry.score, 2)
        entry.headlines.sort(key=lambda h: h.created_at, reverse=True)
    return result


class NewsScanner:
    def __init__(self, client, cfg):
        self.client = client
        self.cfg = cfg

    def scan(self, symbols=None, now=None):
        now = now or datetime.now(timezone.utc)
        start = (now - timedelta(hours=self.cfg.lookback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        articles = self.client.news(start, symbols=symbols)
        return score_articles(articles, now)


def hot_list(news_map, min_score, limit=10):
    """Stocks with the strongest good news, best first."""
    hot = [n for n in news_map.values() if n.score >= min_score]
    return sorted(hot, key=lambda n: n.score, reverse=True)[:limit]
