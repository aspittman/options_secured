"""Completed-daily signals shared by paper execution and the backtester."""
import math
import pandas as pd
from ta.momentum import RSIIndicator

STRATEGIES = ("sideways", "bullish_pullback")  # historical daily research
LIVE_STRATEGIES = ("regular", "oasis")


def indicators(close):
    close = pd.Series(close, dtype=float)
    return pd.DataFrame({
        "close": close,
        "sma50": close.rolling(50).mean(),
        "sma200": close.rolling(200).mean(),
        "sma50_change": close.rolling(50).mean().pct_change(5),
        "return20": close.pct_change(20),
        "vol": close.pct_change().rolling(20).std() * math.sqrt(252),
        "rsi": RSIIndicator(close, window=14).rsi(),
    })


def regime_at(frame, index, cfg):
    row = frame.iloc[index]
    if not all(math.isfinite(float(v)) for v in row):
        return False
    return bool(
        row.close >= row.sma200 and row.sma50 >= row.sma200 * 0.99
        and -0.01 <= row.sma50_change <= 0.02
        and -0.03 <= row.return20 <= cfg.max_20d_return
        and abs(row.close / row.sma50 - 1) <= cfg.max_sma_distance
        and row.vol <= cfg.max_annual_vol
    )


def entry_at(frame, index, variant, cfg):
    if variant == "regular":
        variant = "sideways"
    if variant not in STRATEGIES:
        raise ValueError(f"Unknown strategy {variant}")
    if not regime_at(frame, index, cfg):
        return False
    row = frame.iloc[index]
    if variant == "sideways":
        return bool(40 <= row.rsi <= 60 and abs(row.return20) <= 0.04)
    return bool(40 <= row.rsi <= 65 and row.sma50 >= row.sma200
                and 0.98 <= row.close / row.sma50 <= 1.02)


def bearish_at(frame, index):
    row = frame.iloc[index]
    return bool(row.close < row.sma200 or row.sma50 < row.sma200 * 0.98)


def exit_reason(entry_credit, ask, dte, bearish, cfg):
    if dte <= cfg.exit_dte:
        return "expiration_risk"
    if bearish:
        return "bearish_regime"
    if ask >= entry_credit * cfg.stop_credit_multiple:
        return "credit_stop"
    if ask <= entry_credit * (1 - cfg.profit_capture):
        return "profit_capture"
    return ""
