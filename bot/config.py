"""Loads config.yaml and the secrets in the environment (.env)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


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


@dataclass
class FeeConfig:
    maker_pct: float = 0.25
    taker_pct: float = 0.40


@dataclass
class RiskConfig:
    max_drawdown_pct: float = 10.0
    breakout_action: str = "pause"


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
    symbol: str = "SOL/USD"
    capital_usd: float = 400.0
    poll_seconds: int = 30
    paper_slippage_pct: float = 0.05
    grid: GridConfig = field(default_factory=GridConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    email: EmailSecrets = field(default_factory=EmailSecrets)
    kraken_api_key: str = ""
    kraken_api_secret: str = ""
    data_dir: Path = Path("data")

    @property
    def state_path(self):
        return self.data_dir / f"state-{self.mode}.json"

    @property
    def command_path(self):
        return self.data_dir / "command.txt"

    @property
    def history_path(self):
        return self.data_dir / f"history-{self.mode}.jsonl"

    @property
    def trades_path(self):
        return self.data_dir / f"trades-{self.mode}.jsonl"

    def validate(self):
        if self.mode not in ("paper", "live"):
            raise ConfigError("mode must be 'paper' or 'live'")
        if self.capital_usd <= 0:
            raise ConfigError("capital_usd must be positive")
        if self.risk.breakout_action not in ("pause", "recenter"):
            raise ConfigError("risk.breakout_action must be 'pause' or 'recenter'")
        if not 0 < self.risk.max_drawdown_pct < 100:
            raise ConfigError("risk.max_drawdown_pct must be between 0 and 100")
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
        if self.mode == "live" and not (self.kraken_api_key and self.kraken_api_secret):
            raise ConfigError("live mode needs KRAKEN_API_KEY and KRAKEN_API_SECRET in .env")


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

    cfg = Config(
        mode=str(raw.get("mode", "paper")).lower(),
        symbol=raw.get("symbol", "SOL/USD"),
        capital_usd=float(raw.get("capital_usd", 400)),
        poll_seconds=int((raw.get("loop") or {}).get("poll_seconds", 30)),
        paper_slippage_pct=float((raw.get("paper") or {}).get("slippage_pct", 0.05)),
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
        kraken_api_key=env.get("KRAKEN_API_KEY", "").strip(),
        kraken_api_secret=env.get("KRAKEN_API_SECRET", "").strip(),
        data_dir=Path(env.get("BOT_DATA_DIR", "data")),
    )
    cfg.validate()
    return cfg
