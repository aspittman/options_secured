"""Cash-secured put defaults; thresholds are hypotheses, not optimized results."""
import os
from dataclasses import dataclass
from math import isfinite
from dotenv import load_dotenv
from universe import DEFAULT_UNDERLYINGS, correlation_group

load_dotenv()
LOG_FILE = "logs/options_bot.log"
STRATEGY_ID = "cash_secured_put"


def flag(name, default=False):
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        raise ValueError(f"Invalid boolean {name}")
    return value in {"true", "1", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    paper: bool = True
    enable_entries: bool = False
    underlyings: tuple = DEFAULT_UNDERLYINGS
    min_dte: int = 30
    max_dte: int = 45
    exit_dte: int = 7
    target_delta: float = -0.25
    delta_tolerance: float = 0.10
    min_open_interest: int = 500
    min_volume: int = 100
    max_spread: float = 0.10
    min_credit_yield: float = 0.005
    virtual_starting_capital: float = 25000
    max_contracts_per_trade: int = 1
    max_collateral_per_trade: float = 25000
    max_total_collateral: float = 25000
    cash_buffer: float = 1000
    max_positions: int = 2
    max_per_group: int = 1
    profit_capture: float = 0.50
    stop_credit_multiple: float = 2.0
    option_trailing_stop_percent: float = .20  # Oasis only; regular retains its credit stop.
    max_sma_distance: float = 0.05
    max_20d_return: float = 0.08
    max_annual_vol: float = 0.35
    cooldown_days: int = 5
    entry_timeout: int = 900
    exit_timeout: int = 120
    quote_max_age: int = 120
    scan_seconds: int = 60
    option_feed: str = "indicative"
    db_path: str = "logs/options_secured.sqlite3"

    def __post_init__(self):
        if not 0 <= self.option_trailing_stop_percent < 1:
            raise ValueError('OPTION_TRAILING_STOP_PERCENT must be at least zero and less than one')
        if self.max_contracts_per_trade != 1:
            raise ValueError("Cash-secured put research requires exactly one contract per trade")
        if not self.paper:
            raise ValueError("This implementation supports paper trading only.")
        if not 0 <= self.exit_dte < self.min_dte <= self.max_dte:
            raise ValueError("Require 0 <= EXIT_DTE < MIN_DTE <= MAX_DTE")
        if not -1 < self.target_delta < 0 or not 0 < self.delta_tolerance < 1:
            raise ValueError("Put delta must be negative with a positive tolerance")
        for name in ("virtual_starting_capital", "max_collateral_per_trade", "max_total_collateral", "max_positions",
                     "max_per_group", "stop_credit_multiple", "quote_max_age", "scan_seconds",
                     "entry_timeout", "exit_timeout", "max_spread", "max_sma_distance",
                     "max_20d_return", "max_annual_vol"):
            value = getattr(self, name)
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        for name in ("cash_buffer", "min_credit_yield", "min_open_interest", "min_volume", "cooldown_days"):
            if not isfinite(getattr(self, name)) or getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative and finite")
        if not 0 < self.profit_capture < 1 or self.stop_credit_multiple <= 1:
            raise ValueError("Invalid profit target or stop credit multiple")
        if self.option_feed not in {"indicative", "opra"} or not self.underlyings:
            raise ValueError("Invalid feed or empty underlyings")

    @classmethod
    def from_env(cls):
        defaults = cls()
        values = {}
        for name in cls.__dataclass_fields__:
            key = {"paper": "ALPACA_PAPER", "enable_entries": "ENABLE_NEW_ENTRIES"}.get(name, name.upper())
            default = getattr(defaults, name)
            if isinstance(default, bool):
                values[name] = flag(key, default)
            elif isinstance(default, tuple):
                values[name] = tuple(s.strip().upper() for s in os.getenv(key, ",".join(default)).split(",") if s.strip())
            else:
                values[name] = cls.__dataclass_fields__[name].type(os.getenv(key, str(default)))
        return cls(**values)


def credentials():
    def first(names):
        return next((os.environ[n].strip() for n in names if os.getenv(n, "").strip()), None)
    key = first(("APCA_API_KEY_ID", "ALPACA_API_KEY", "API_KEY"))
    secret = first(("APCA_API_SECRET_KEY", "ALPACA_SECRET_KEY", "SECRET_KEY"))
    if not key or not secret:
        raise RuntimeError("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in .env")
    return key, secret
