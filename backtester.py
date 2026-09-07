"""Estimated daily CSP portfolio simulation; synthetic prices are not historical fills."""
import argparse
import csv
from datetime import date, timedelta
import json
import math
from pathlib import Path
from statistics import NormalDist

import pandas as pd

from analytics import Ledger
from config import Settings, correlation_group
from strategy import STRATEGIES, bearish_at, entry_at, exit_reason, indicators, regime_at

NORMAL = NormalDist()


def put_price(spot, strike, dte, volatility, rate=0.04):
    if dte <= 0:
        return max(strike - spot, 0.0)
    years = dte / 365
    sigma = max(volatility, 0.05)
    d1 = (math.log(spot / strike) + (rate + sigma * sigma / 2) * years) / (sigma * math.sqrt(years))
    d2 = d1 - sigma * math.sqrt(years)
    return max(0.0, strike * math.exp(-rate * years) * NORMAL.cdf(-d2) - spot * NORMAL.cdf(-d1))


def strike_for_delta(spot, dte, volatility, delta):
    years = dte / 365
    sigma = max(volatility, 0.05)
    d1 = NORMAL.inv_cdf(1 + delta)
    value = spot * math.exp((0.04 + sigma * sigma / 2) * years - d1 * sigma * math.sqrt(years))
    return max(1, math.floor(min(value, spot - 1)))


def simulate(histories, cfg, start, starting_cash=100000, slippage=0.05, fee=0.65):
    """Signals use yesterday's close; hypothetical orders fill at today's close.

    Return full daily marked equity, including open liabilities. Stocks/ETFs use
    raw closes; actual option chains, liquidity, earnings, and early assignment
    are not modeled. Only default broad ETFs are supported here.
    """
    if starting_cash <= 0 or slippage < 0 or fee < 0:
        raise ValueError("Invalid simulation costs or starting cash")
    if "SPY" not in histories:
        raise ValueError("SPY market-regime history is required")
    if any(s not in {"SPY", "QQQ", "IWM", "DIA"} for s in cfg.underlyings):
        raise ValueError("Synthetic backtest supports broad ETFs only; no earnings model")
    frames = {s: indicators(close) for s, close in histories.items()}
    dates = sorted(set().union(*(set(f.index) for f in frames.values())))
    cash, lots, trades, curve, last_exit = float(starting_cash), {}, [], [], {}
    last_marks = {}
    for day in dates:
        if day.date() < start:
            continue
        for symbol, lot in list(lots.items()):
            frame = frames[symbol]
            if day not in frame.index:
                continue
            index = frame.index.get_loc(day)
            if index < 1:
                continue
            spot = float(frame.loc[day, "close"])
            vol = float(frame.iloc[index - 1].vol)
            dte = (lot["expiry"] - day.date()).days
            theoretical = put_price(spot, lot["strike"], dte, vol)
            ask = theoretical + slippage
            last_marks[symbol] = ask
            reason = exit_reason(lot["credit"], ask, dte, bearish_at(frame, index - 1), cfg)
            if reason:
                cash -= ask * 100 + fee
                pnl = (lot["credit"] - ask) * 100 - 2 * fee
                trades.append({"symbol": symbol, "strategy": lot["strategy"], "entry_date": lot["entry_date"],
                               "exit_date": day.date().isoformat(), "strike": lot["strike"],
                               "entry_credit": lot["credit"], "exit_debit": ask, "collateral": lot["strike"] * 100,
                               "pnl": pnl, "return_on_collateral": pnl / (lot["strike"] * 100), "reason": reason})
                del lots[symbol]
                last_exit[symbol] = day.date()
        market = frames["SPY"]
        market_idx = market.index.get_loc(day) if day in market.index else -1
        market_ok = market_idx >= 205 and regime_at(market, market_idx - 1, cfg)
        if market_ok:
            for symbol in cfg.underlyings:
                if symbol not in frames or symbol in lots or len(lots) >= cfg.max_positions:
                    continue
                if symbol in last_exit and (day.date() - last_exit[symbol]).days < cfg.cooldown_days:
                    continue
                if sum(correlation_group(s) == correlation_group(symbol) for s in lots) >= cfg.max_per_group:
                    continue
                frame = frames[symbol]
                if day not in frame.index:
                    continue
                index = frame.index.get_loc(day)
                if index < 205 or frame.index[index - 1] != market.index[market_idx - 1]:
                    continue
                eligible = [s for s in STRATEGIES if entry_at(frame, index - 1, s, cfg)]
                if not eligible:
                    continue
                previous = frame.iloc[index - 1]
                dte = (cfg.min_dte + cfg.max_dte) // 2
                strike = strike_for_delta(float(previous.close), dte, float(previous.vol), cfg.target_delta)
                spot = float(frame.iloc[index].close)
                if strike >= spot:
                    continue
                credit = max(0, put_price(spot, strike, dte, float(previous.vol)) - slippage)
                collateral = strike * 100
                reserved = sum(lot["strike"] * 100 for lot in lots.values())
                if (credit / strike < cfg.min_credit_yield or collateral > cfg.max_collateral_per_trade
                    or reserved + collateral > cfg.max_total_collateral
                    or collateral + fee > cash - reserved - cfg.cash_buffer):
                    continue
                cash += credit * 100 - fee
                lots[symbol] = dict(strategy=eligible[0], strike=strike, credit=credit,
                                    expiry=day.date() + timedelta(days=dte), entry_date=day.date().isoformat())
                last_marks[symbol] = credit + 2 * slippage
        liability = sum(last_marks[symbol] * 100 for symbol in lots)
        curve.append({"date": day.date().isoformat(), "cash": cash, "short_liability": liability,
                      "reserved_collateral": sum(lot["strike"] * 100 for lot in lots.values()), "equity": cash - liability})
    peak = starting_cash
    drawdown = 0
    for row in curve:
        peak = max(peak, row["equity"])
        drawdown = max(drawdown, (peak - row["equity"]) / peak)
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] < 0]
    summary = {"model": "synthetic European put estimates, not historical option fills",
               "completed_trades": len(trades), "open_positions": len(lots),
               "realized_pnl": sum(t["pnl"] for t in trades),
               "ending_equity": curve[-1]["equity"] if curve else starting_cash,
               "win_rate": len(wins) / len(trades) if trades else None,
               "profit_factor": sum(wins) / -sum(losses) if losses else None,
               "expectancy": sum(t["pnl"] for t in trades) / len(trades) if trades else None,
               "max_drawdown": drawdown}
    return trades, curve, summary


def save_csv(path, rows, fields):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, default=1)
    parser.add_argument("--starting-cash", type=float, default=100000)
    parser.add_argument("--slippage", type=float, default=0.05, help="Per-share cost on each side")
    parser.add_argument("--fee", type=float, default=0.65, help="Per-contract cost on each side")
    parser.add_argument("--paper-results", action="store_true")
    parser.add_argument("--data-dir", help="Offline SYMBOL.csv files with Date,Close columns (include SPY and warmup)")
    args = parser.parse_args()
    cfg = Settings.from_env()
    if args.paper_results:
        print(json.dumps(Ledger(cfg.db_path).report(), indent=2))
        return
    if args.years <= 0:
        parser.error("--years must be positive")
    start = date.today() - timedelta(days=args.years * 365)
    histories = {}
    for symbol in dict.fromkeys(("SPY",) + cfg.underlyings):
        if args.data_dir:
            data = pd.read_csv(Path(args.data_dir) / f"{symbol}.csv", parse_dates=["Date"]).set_index("Date")
        else:
            import yfinance as yf
            data = yf.download(symbol, start=start - timedelta(days=500), auto_adjust=False, progress=False)
        if data.empty:
            raise RuntimeError(f"No history for {symbol}")
        close = data["Close"].squeeze().dropna().sort_index()
        close = close[[stamp.date() < date.today() for stamp in close.index]]
        if close.index.has_duplicates or (close <= 0).any() or not all(math.isfinite(float(v)) for v in close):
            raise ValueError(f"Invalid history for {symbol}")
        histories[symbol] = close
    trades, curve, summary = simulate(histories, cfg, start, args.starting_cash, args.slippage, args.fee)
    save_csv("logs/options_backtest_trades.csv", trades, ["symbol", "strategy", "entry_date", "exit_date", "strike", "entry_credit", "exit_debit", "collateral", "pnl", "return_on_collateral", "reason"])
    save_csv("logs/options_backtest_equity_curve.csv", curve, ["date", "cash", "short_liability", "reserved_collateral", "equity"])
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
