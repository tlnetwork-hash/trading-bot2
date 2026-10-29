"""Loads config.yaml and the secrets in the environment (.env)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


# ccxt exchange id -> name shown in alerts and on the dashboard
EXCHANGES = {"kraken": "Kraken", "binanceus": "Binance.US"}


@dataclass
class GridConfig:
    timeframe: str = "1h"
    atr_period: int = 14
    atr_multiplier: float = 6.0
    min_range_pct: float = 5.0
    max_range_pct: float = 20.0
    min_spacing_pct: float = 1.0
    max_levels: int = 20
    fee_reserve_pct: float = 2.0
    # Fixed grid width around the price (percent below / above). 0 = size it from volatility.
    range_below_pct: float = 0.0
    range_above_pct: float = 0.0
    # When (re)building, sell the coins already held through the grid's sell
    # orders instead of a market sale. Buys fill in as those sells complete.
    keep_coins_on_build: bool = False
    # How many levels apart each buy and its matching sell are. 1 = next level
    # (normal grid); 2 = buy two steps below the last sell and sell two steps
    # above the last buy (wider gap, more profit per trip, fewer trades).
    pair_steps: int = 1
    # Levels above a filled buy to put its sell. 0 = same as pair_steps.
    sell_steps: int = 0


@dataclass
class TrendConfig:
    # Coins to hold while each one's daily close is above its sma_days average.
    symbols: list = field(default_factory=lambda: ["BTC/USD", "ETH/USD", "SOL/USD"])
    sma_days: int = 50


@dataclass
class BollingerConfig:
    # strategy: bollinger (paper only). Bands from closed candles of `timeframe`.
    timeframe: str = "1h"
    period: int = 20               # middle band = average of this many closes
    std_dev: float = 2.0           # upper/lower = middle +/- this many standard deviations
    min_profit_pct: float = 1.0    # each buy sells at the middle band or this % up, whichever is higher
    buy_size_pct: float = 15.0     # full-size buy = this % of capital_usd
    max_coin_pct: float = 60.0     # never hold more than this % of the bot's money in the coin
    step_down_pct: float = 0.5     # buys in a row: each waits this much further below the last (0.5, 1, 1.5...)
    # Trend filter: below the trend_period average on trend_timeframe AND it is sloping down -> half size
    trend_timeframe: str = "4h"
    trend_period: int = 50
    trend_slope_candles: int = 6   # slope = change in the average over this many candles (6 x 4h = 1 day)
    trend_flat_pct: float = 0.1    # a slope within +/- this % counts as flat
    downtrend_size: float = 0.5
    # Squeeze: band width in the lowest squeeze_percentile % of the last squeeze_lookback candles
    squeeze_percentile: float = 20.0
    squeeze_lookback: int = 120
    squeeze_size: float = 0.5
    indicator_exchange: str = ""   # where candles come from ("" = dashboard.chart_exchange)


@dataclass
class FeeConfig:
    maker_pct: float = 0.40
    taker_pct: float = 0.80


@dataclass
class RiskConfig:
    max_drawdown_pct: float = 10.0
    # When the kill switch fires, also sell the bot's coins so it ends in cash.
    kill_switch_sells: bool = False
    breakout_action: str = "pause"
    # Profit lock (trailing stop): once equity is up profit_lock_start_pct, sell
    # everything if it gives back more than (100 - profit_lock_keep_pct)% of its
    # best gain. 0 turns it off.
    profit_lock_start_pct: float = 2.0
    profit_lock_keep_pct: float = 50.0
    # The profit lock never measures from below this dollar amount, so it only
    # arms once equity is profit_lock_start_pct above it (0 = no minimum).
    profit_lock_min_base_usd: float = 0.0
    # Sell the bot's coins when the price falls out of the bottom of the grid.
    sell_on_breakout_down: bool = True
    # After either of those sales, start trading again by itself once the price
    # has bounced restart_bounce_pct off its low, is no higher than where it
    # sold, and at least restart_wait_minutes have passed.
    auto_restart: bool = True
    restart_bounce_pct: float = 2.0
    restart_wait_minutes: int = 60
    # When the bot pauses because the price fell out of the bottom of the grid
    # (and it kept its coins), start trading again by itself once what it holds
    # is worth its starting amount again (break-even).
    resume_at_break_even: bool = False
    # If paused because the price broke ABOVE the grid, rebuild around the new
    # price by itself once it has stayed above for this many minutes, or right
    # away if it drops back to the old range first. 0 = off (wait for you).
    recenter_above_after_minutes: int = 0
    # Never place a buy below this price (0 = off). Buy levels under it are
    # held and go back on if you lower or remove it. Sells are not affected.
    no_buy_below_usd: float = 0.0
    # Never place a buy above this price (0 = off). Held like the floor.
    no_buy_above_usd: float = 0.0
    # Follow the market risk monitor (bot/risk.py): hold off on new buys while
    # its level is HIGH. Off unless a config turns it on.
    guard: bool = False
    # Slide guard: also hold off on new buys while the price is more than
    # slide_guard_pct below where it was slide_guard_hours ago (hourly candles).
    # Works even when guard is off. 0 turns it off. Backtest 2023-2026:
    # /root/backtest (slide_sim.py, restart_sim.py).
    slide_guard_pct: float = 0.0
    slide_guard_hours: int = 12


@dataclass
class AlertConfig:
    email_on_every_fill: bool = True
    daily_summary_hour_utc: int = 13
    error_email_cooldown_minutes: int = 30


@dataclass
class EmailSecrets:
    gmail_address: str = ""
    gmail_app_password: str = ""
    to_address: str = ""
    brevo_api_key: str = ""

    @property
    def method(self):
        """Brevo's web API if a key is set (works where email ports are blocked), else Gmail."""
        if not self.to_address or not self.gmail_address:
            return None
        if self.brevo_api_key:
            return "brevo"
        if self.gmail_app_password:
            return "gmail"
        return None

    @property
    def enabled(self):
        return self.method is not None


@dataclass
class Config:
    mode: str = "paper"
    strategy: str = "grid"         # grid | trend | bollinger
    exchange: str = "kraken"
    symbol: str = "SOL/USD"
    capital_usd: float = 400.0
    poll_seconds: int = 30
    paper_slippage_pct: float = 0.05
    paper_fill_through_pct: float = 0.05
    grid: GridConfig = field(default_factory=GridConfig)
    trend: TrendConfig = field(default_factory=TrendConfig)
    bollinger: BollingerConfig = field(default_factory=BollingerConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    email: EmailSecrets = field(default_factory=EmailSecrets)
    api_key: str = ""
    api_secret: str = ""
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    ntfy_command_topic: str = ""   # private topic the bots read phone commands from
    ntfy_command_pin: str = ""     # every phone command must start with this
    data_dir: Path = Path("data")
    dashboard_links: list = field(default_factory=list)  # [{"label": ..., "port": ..., "url": ...}]
    chart_exchange: str = ""  # where the dashboard's price chart comes from; "" = exchange
    service_name: str = "bot"  # docker compose service, used in help text for commands
    # Set (NTFY_COMMANDS_PRIMARY=1) for a bot with its own command topic, so it
    # answers "help" and rejected-command alerts there like the main bot does.
    own_command_topic: bool = False

    @property
    def remote_primary(self):
        return self.service_name == "bot" or self.own_command_topic

    @property
    def chart_source(self):
        return self.chart_exchange or self.exchange

    @property
    def exchange_name(self):
        return EXCHANGES.get(self.exchange, self.exchange)

    @property
    def env_prefix(self):
        """Prefix of this exchange's API key variables in .env, e.g. KRAKEN_API_KEY."""
        return self.exchange.upper()

    @property
    def ntfy_url(self):
        return f"{self.ntfy_server.rstrip('/')}/{self.ntfy_topic}" if self.ntfy_topic else ""

    @property
    def ntfy_command_url(self):
        if not (self.ntfy_command_topic and self.ntfy_command_pin):
            return ""
        return f"{self.ntfy_server.rstrip('/')}/{self.ntfy_command_topic}"

    @property
    def _file_tag(self):
        """Trend runs keep their own files, so switching back to grid finds its old state."""
        if self.strategy == "bollinger":
            return f"bb-{self.mode}"   # own files: the grid's state and logs stay as they were
        return f"trend-{self.mode}" if self.strategy == "trend" else self.mode

    @property
    def state_path(self):
        return self.data_dir / f"state-{self._file_tag}.json"

    @property
    def command_path(self):
        return self.data_dir / "command.txt"

    @property
    def history_path(self):
        return self.data_dir / f"history-{self._file_tag}.jsonl"

    @property
    def trades_path(self):
        return self.data_dir / f"trades-{self._file_tag}.jsonl"

    def validate(self):
        if self.mode not in ("paper", "live"):
            raise ConfigError("mode must be 'paper' or 'live'")
        if self.strategy not in ("grid", "trend", "bollinger"):
            raise ConfigError("strategy must be 'grid', 'trend' or 'bollinger'")
        if self.strategy == "bollinger":
            b = self.bollinger
            if b.period < 2 or b.std_dev <= 0 or b.trend_period < 2:
                raise ConfigError("bollinger.period and trend_period must be 2+ and std_dev above 0")
            if not 0 < b.max_coin_pct < 100 or not 0 < b.buy_size_pct <= b.max_coin_pct:
                raise ConfigError("bollinger.max_coin_pct must be 0-100 and buy_size_pct at most that")
        if self.strategy == "trend":
            if self.mode != "paper":
                raise ConfigError("the trend strategy is paper-only for now")
            if not self.trend.symbols or self.trend.sma_days < 2:
                raise ConfigError("trend needs at least one symbol and sma_days of 2 or more")
        if self.exchange not in EXCHANGES:
            raise ConfigError(f"exchange must be one of: {', '.join(EXCHANGES)}")
        if self.chart_source not in EXCHANGES:
            raise ConfigError(f"dashboard.chart_exchange must be one of: {', '.join(EXCHANGES)}")
        if self.capital_usd <= 0:
            raise ConfigError("capital_usd must be positive")
        if self.risk.breakout_action not in ("pause", "recenter"):
            raise ConfigError("risk.breakout_action must be 'pause' or 'recenter'")
        if not 0 < self.risk.max_drawdown_pct < 100:
            raise ConfigError("risk.max_drawdown_pct must be between 0 and 100")
        r = self.risk
        if r.profit_lock_start_pct < 0:
            raise ConfigError("risk.profit_lock_start_pct cannot be negative (0 turns it off)")
        if r.restart_bounce_pct <= 0 or r.restart_wait_minutes < 0:
            raise ConfigError("risk.restart_bounce_pct must be above 0 and "
                              "risk.restart_wait_minutes cannot be negative")
        if r.profit_lock_start_pct > 0 and not 0 < r.profit_lock_keep_pct < 100:
            raise ConfigError("risk.profit_lock_keep_pct must be between 0 and 100")
        if self.grid.min_range_pct > self.grid.max_range_pct:
            raise ConfigError("grid.min_range_pct cannot be larger than grid.max_range_pct")
        if self.grid.max_levels < 2:
            raise ConfigError("grid.max_levels must be at least 2")
        # Each grid step earns one spacing and pays a maker fee on both legs.
        if self.grid.min_spacing_pct <= 2 * self.fees.maker_pct:
            raise ConfigError(
                f"grid.min_spacing_pct ({self.grid.min_spacing_pct}%) must be larger than "
                f"2x the maker fee ({2 * self.fees.maker_pct}%) or every trade loses money"
            )
        if self.mode == "live" and not (self.api_key and self.api_secret):
            raise ConfigError(f"live mode needs {self.env_prefix}_API_KEY and "
                              f"{self.env_prefix}_API_SECRET in .env")
        if self.ntfy_command_topic or self.ntfy_command_pin:
            if not (self.ntfy_command_topic and self.ntfy_command_pin):
                raise ConfigError("phone commands need both NTFY_COMMAND_TOPIC and NTFY_COMMAND_PIN in .env")
            if not (self.ntfy_command_pin.isdigit() and 4 <= len(self.ntfy_command_pin) <= 8):
                raise ConfigError("NTFY_COMMAND_PIN must be 4 to 8 digits")
            if self.ntfy_command_topic == self.ntfy_topic:
                raise ConfigError("NTFY_COMMAND_TOPIC must be different from NTFY_TOPIC "
                                  "(anyone who can read your alerts could control the bots)")
        for link in self.dashboard_links:
            if not isinstance(link, dict) or not link.get("label") or not isinstance(link.get("port"), int):
                raise ConfigError("each dashboard link needs a label and a port number")
            url = link.get("url")
            if url is not None and not (isinstance(url, str) and url.startswith("https://")):
                raise ConfigError("a dashboard link's url must start with https://")


def _section(cls, raw):
    raw = raw or {}
    known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
    unknown = set(raw) - set(known)
    if unknown:
        raise ConfigError(f"unknown setting(s) in {cls.__name__}: {', '.join(sorted(unknown))}")
    return cls(**known)


def load_config(path="config.yaml", env=None):
    env = os.environ if env is None else env
    try:
        raw = yaml.safe_load(Path(path).read_text()) or {}
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}")

    exchange = str(raw.get("exchange", "kraken")).lower()
    prefix = exchange.upper()
    cfg = Config(
        mode=str(raw.get("mode", "paper")).lower(),
        strategy=str(raw.get("strategy", "grid")).lower(),
        trend=_section(TrendConfig, raw.get("trend")),
        bollinger=_section(BollingerConfig, raw.get("bollinger")),
        exchange=exchange,
        symbol=raw.get("symbol", "SOL/USD"),
        capital_usd=float(raw.get("capital_usd", 400)),
        poll_seconds=int((raw.get("loop") or {}).get("poll_seconds", 30)),
        paper_slippage_pct=float((raw.get("paper") or {}).get("slippage_pct", 0.05)),
        paper_fill_through_pct=float((raw.get("paper") or {}).get("fill_through_pct", 0.05)),
        grid=_section(GridConfig, raw.get("grid")),
        fees=_section(FeeConfig, raw.get("fees")),
        risk=_section(RiskConfig, raw.get("risk")),
        alerts=_section(AlertConfig, raw.get("alerts")),
        email=EmailSecrets(
            gmail_address=env.get("GMAIL_ADDRESS", "").strip(),
            gmail_app_password=env.get("GMAIL_APP_PASSWORD", "").replace(" ", ""),
            to_address=env.get("ALERT_EMAIL_TO", "").strip() or env.get("GMAIL_ADDRESS", "").strip(),
            brevo_api_key=env.get("BREVO_API_KEY", "").strip(),
        ),
        api_key=env.get(f"{prefix}_API_KEY", "").strip(),
        api_secret=env.get(f"{prefix}_API_SECRET", "").strip(),
        ntfy_topic=env.get("NTFY_TOPIC", "").strip(),
        ntfy_server=env.get("NTFY_SERVER", "").strip() or "https://ntfy.sh",
        ntfy_command_topic=env.get("NTFY_COMMAND_TOPIC", "").strip(),
        ntfy_command_pin=env.get("NTFY_COMMAND_PIN", "").strip(),
        data_dir=Path(env.get("BOT_DATA_DIR", "data")),
        dashboard_links=list((raw.get("dashboard") or {}).get("links") or []),
        chart_exchange=str((raw.get("dashboard") or {}).get("chart_exchange") or "").lower(),
        service_name=env.get("BOT_SERVICE", "").strip() or "bot",
        own_command_topic=env.get("NTFY_COMMANDS_PRIMARY", "").strip().lower() in ("1", "true", "yes"),
    )
    cfg.validate()
    return cfg
