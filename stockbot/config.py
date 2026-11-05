"""Settings for the stock bot: config-stocks.yaml plus secrets from .env."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from bot.config import ConfigError, EmailSecrets, _section


@dataclass
class ScannerConfig:
    scan_minutes: int = 5          # how often to look for new trades
    min_price: float = 3.0         # skip penny stocks
    max_price: float = 300.0
    min_gain_pct: float = 3.0      # must be up at least this much today...
    max_gain_pct: float = 40.0     # ...but not so much that it's likely to crash back
    min_relative_volume: float = 2.0  # today's volume vs. normal for this time of day
    min_dollar_volume: float = 1_000_000  # traded today, so we can get in and out
    max_spread_pct: float = 0.5    # gap between bid and ask
    near_high_pct: float = 3.0     # price must be within this % of today's high
    breakout_bars: int = 3         # 5-minute bars whose high the price must clear
    universe_size: int = 50        # how many top movers/most-active stocks to look at
    exclude: list = field(default_factory=list)  # symbols never to trade


@dataclass
class NewsConfig:
    enabled: bool = True
    lookback_hours: int = 16       # covers last night and pre-market
    min_score_to_buy: float = 0.0  # news score a stock needs (negative news blocks it)
    require_news: bool = False     # only buy stocks that have fresh good news
    news_weight: float = 1.0       # how much good news raises a stock's rank
    alert_min_score: float = 2.0   # send a phone alert for headlines at least this strong


@dataclass
class TradeConfig:
    style: str = "day"             # day = sell everything before the close; swing = hold overnight
    max_positions: int = 3
    risk_per_trade_pct: float = 1.0    # % of capital lost if the stop-loss hits
    max_position_pct: float = 30.0     # largest single position, % of capital
    stop_loss_pct: float = 3.0
    take_profit_pct: float = 6.0
    entry_delay_minutes: int = 15      # skip the wild first minutes after the open
    entry_cutoff_minutes: int = 60     # no new trades this close to the close
    flatten_minutes: int = 10          # day style: sell everything this long before the close
    max_trades_per_day: int = 5
    daily_loss_limit_pct: float = 3.0  # stop for the day after losing this % of capital
    respect_pdt: bool = True           # under $25k: don't make a 4th day trade in 5 days


@dataclass
class StockConfig:
    mode: str = "paper"
    capital_usd: float = 1000.0
    poll_seconds: int = 30
    data_feed: str = "iex"         # iex = free; sip = paid, full market volume
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    trade: TradeConfig = field(default_factory=TradeConfig)
    email: EmailSecrets = field(default_factory=EmailSecrets)
    api_key: str = ""
    api_secret: str = ""
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    data_dir: Path = Path("data-stocks")

    @property
    def trading_url(self):
        return ("https://api.alpaca.markets" if self.mode == "live"
                else "https://paper-api.alpaca.markets")

    @property
    def ntfy_url(self):
        return f"{self.ntfy_server.rstrip('/')}/{self.ntfy_topic}" if self.ntfy_topic else ""

    @property
    def state_path(self):
        return self.data_dir / f"state-{self.mode}.json"

    @property
    def trades_path(self):
        return self.data_dir / f"trades-{self.mode}.jsonl"

    @property
    def command_path(self):
        return self.data_dir / "command.txt"

    def validate(self):
        if self.mode not in ("paper", "live"):
            raise ConfigError("mode must be 'paper' or 'live'")
        if self.capital_usd <= 0:
            raise ConfigError("capital_usd must be positive")
        if self.data_feed not in ("iex", "sip"):
            raise ConfigError("data_feed must be 'iex' or 'sip'")
        t, s = self.trade, self.scanner
        if t.style not in ("day", "swing"):
            raise ConfigError("trade.style must be 'day' or 'swing'")
        if t.max_positions < 1:
            raise ConfigError("trade.max_positions must be at least 1")
        if not 0 < t.stop_loss_pct < 50 or t.take_profit_pct <= 0:
            raise ConfigError("trade.stop_loss_pct must be between 0 and 50, take_profit_pct above 0")
        if not 0 < t.risk_per_trade_pct <= 5:
            raise ConfigError("trade.risk_per_trade_pct must be between 0 and 5")
        if not 0 < t.max_position_pct <= 100:
            raise ConfigError("trade.max_position_pct must be between 0 and 100")
        if not 0 < t.daily_loss_limit_pct < 100:
            raise ConfigError("trade.daily_loss_limit_pct must be between 0 and 100")
        if s.min_price <= 0 or s.min_price >= s.max_price:
            raise ConfigError("scanner.min_price must be above 0 and below scanner.max_price")
        if s.min_gain_pct >= s.max_gain_pct:
            raise ConfigError("scanner.min_gain_pct must be below scanner.max_gain_pct")
        if s.scan_minutes < 1 or s.breakout_bars < 1:
            raise ConfigError("scanner.scan_minutes and scanner.breakout_bars must be at least 1")
        if not (self.api_key and self.api_secret):
            raise ConfigError("the stock bot needs ALPACA_API_KEY and ALPACA_API_SECRET in .env "
                              "(free paper-trading keys work; see STOCKS.md)")


def load_stock_config(path="config-stocks.yaml", env=None):
    env = os.environ if env is None else env
    try:
        raw = yaml.safe_load(Path(path).read_text()) or {}
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}")
    mode = str(raw.get("mode", "paper")).lower()
    # Alpaca gives paper and live accounts different keys.
    key_prefix = "ALPACA_LIVE" if mode == "live" else "ALPACA"
    cfg = StockConfig(
        mode=mode,
        capital_usd=float(raw.get("capital_usd", 1000)),
        poll_seconds=int(raw.get("poll_seconds", 30)),
        data_feed=str(raw.get("data_feed", "iex")).lower(),
        scanner=_section(ScannerConfig, raw.get("scanner")),
        news=_section(NewsConfig, raw.get("news")),
        trade=_section(TradeConfig, raw.get("trade")),
        email=EmailSecrets(
            gmail_address=env.get("GMAIL_ADDRESS", "").strip(),
            gmail_app_password=env.get("GMAIL_APP_PASSWORD", "").replace(" ", ""),
            to_address=env.get("ALERT_EMAIL_TO", "").strip() or env.get("GMAIL_ADDRESS", "").strip(),
            brevo_api_key=env.get("BREVO_API_KEY", "").strip(),
        ),
        api_key=env.get(f"{key_prefix}_API_KEY", "").strip(),
        api_secret=env.get(f"{key_prefix}_API_SECRET", "").strip(),
        ntfy_topic=env.get("NTFY_TOPIC", "").strip(),
        ntfy_server=env.get("NTFY_SERVER", "").strip() or "https://ntfy.sh",
        data_dir=Path(env.get("BOT_DATA_DIR", "data-stocks")),
    )
    cfg.trade.style = cfg.trade.style.lower()
    cfg.scanner.exclude = [str(s).upper() for s in cfg.scanner.exclude]
    cfg.validate()
    return cfg
