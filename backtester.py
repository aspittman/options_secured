"""Estimated daily CSP portfolio simulation; synthetic prices are not historical fills."""
import argparse
from collections import Counter
from dataclasses import replace
import csv
from datetime import date, timedelta
import json
import math
from pathlib import Path
from statistics import NormalDist

import pandas as pd

from analytics import Ledger, REJECTION_FIELDS
from performance import statistics
from risk import virtual_capacity
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


def simulate(histories, cfg, start, starting_cash=None, slippage=0.05, fee=0.65):
    """Signals use yesterday's close; hypothetical orders fill at today's close.

    Return full daily marked equity, including open liabilities. Stocks/ETFs use
    raw closes; actual option chains, liquidity, earnings, and early assignment
    are not modeled. Only default broad ETFs are supported here.
    """
    if starting_cash is None:
        starting_cash=cfg.virtual_starting_capital
    elif starting_cash != cfg.virtual_starting_capital:
        cfg=replace(cfg,virtual_starting_capital=starting_cash)
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
    entries, rejections = [], []
    qualified_signals=0
    realized=0.0
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
                realized += pnl
                trades.append({"symbol": symbol, "strategy": lot["strategy"], "entry_date": lot["entry_date"],
                               "exit_date": day.date().isoformat(), "strike": lot["strike"],
                               "entry_credit": lot["credit"], "exit_debit": ask, "collateral": lot["strike"] * 100,
                               "pnl": pnl, "return_on_collateral": pnl / (lot["strike"] * 100), "reason": reason,
                               "hold_days": (day.date()-date.fromisoformat(lot["entry_date"])).days, "outcome":"buy_to_close"})
                del lots[symbol]
                last_exit[symbol] = day.date()
        market = frames["SPY"]
        market_idx = market.index.get_loc(day) if day in market.index else -1
        market_ok = market_idx >= 205 and regime_at(market, market_idx - 1, cfg)
        if market_ok:
            for symbol in cfg.underlyings:
                if symbol not in frames:
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
                qualified_signals += 1
                previous = frame.iloc[index - 1]
                dte = (cfg.min_dte + cfg.max_dte) // 2
                strike = strike_for_delta(float(previous.close), dte, float(previous.vol), cfg.target_delta)
                spot = float(frame.iloc[index].close)
                mid=put_price(spot,strike,dte,float(previous.vol))
                credit=max(0,mid-slippage)
                collateral=strike*100
                reserved=sum(lot['strike']*100 for lot in lots.values())
                allowed,capital_reason,available=virtual_capacity(cfg,collateral,reserved,realized-len(lots)*fee)
                reason=''
                capital_only=False
                if strike>=spot or credit/strike<cfg.min_credit_yield:
                    reason='NO_VALID_CONTRACT'
                elif symbol in lots:
                    reason='DUPLICATE_POSITION'
                elif symbol in last_exit and (day.date()-last_exit[symbol]).days<cfg.cooldown_days:
                    reason='OTHER'
                elif len(lots)>=cfg.max_positions or sum(correlation_group(s)==correlation_group(symbol) for s in lots)>=cfg.max_per_group:
                    reason='MAX_STRATEGY_EXPOSURE_REACHED'
                elif not allowed:
                    reason=capital_reason
                    capital_only=True
                elif collateral+fee>available:
                    reason='MAX_STRATEGY_EXPOSURE_REACHED'
                    capital_only=True
                if reason:
                    row=dict.fromkeys(REJECTION_FIELDS)
                    row.update(timestamp=day.isoformat(),strategy='cash_secured_put',variant=eligible[0],underlying=symbol,
                        call_or_put='put',long_or_short='short',strike=strike,expiration=(day.date()+timedelta(days=dte)).isoformat(),
                        DTE=dte,underlying_price=spot,bid=credit,ask=mid+slippage,mid=mid,
                        spread_dollars=2*slippage,spread_percent=2*slippage/mid*100 if mid else None,
                        option_premium=credit*100,required_capital=collateral,virtual_capital_available=available,
                        rejection_reason=reason,market_regime='sideways_to_mildly_bullish',
                        signal_date=frame.index[index-1].date().isoformat(),details='Synthetic contract; no historical liquidity/spread observations')
                    # Count capital-only rejections only after all non-capital rules pass.
                    row['capital_only']=capital_only
                    rejections.append(row)
                    continue
                entries.append({'collateral':collateral,'premium':credit*100})
                cash += credit * 100 - fee
                lots[symbol] = dict(strategy=eligible[0], strike=strike, credit=credit,
                                    expiry=day.date() + timedelta(days=dte), entry_date=day.date().isoformat())
                last_marks[symbol] = credit + 2 * slippage
        liability = sum(last_marks[symbol] * 100 for symbol in lots)
        curve.append({"date": day.date().isoformat(), "cash": cash, "short_liability": liability,
                      "reserved_collateral": sum(lot["strike"] * 100 for lot in lots.values()), "equity": cash - liability})
    ending=curve[-1]['equity'] if curve else starting_cash
    open_unrealized=sum((lot['credit']-last_marks[symbol])*100-fee for symbol,lot in lots.items())
    summary=statistics(starting_cash,realized,open_unrealized,trades,entries,curve)
    summary.update(model='synthetic European put estimates, not historical option fills',
        open_positions=len(lots),ending_equity=ending,qualified_signals=qualified_signals,
        executed_trades=len(entries),rejected=dict(Counter(r['rejection_reason'] for r in rejections)),
        rejected_solely_capital=sum(r['capital_only'] for r in rejections),rejected_opportunities=rejections,
        liquidity_and_spread_validation='not modeled: historical chain data unavailable')
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
    parser.add_argument("--starting-cash", type=float, help="Virtual allocation for this run")
    parser.add_argument("--compare-capital", nargs="+", type=float, help="Compare virtual/collateral ceilings, e.g. 10000 25000 50000")
    parser.add_argument("--slippage", type=float, default=0.05, help="Per-share cost on each side")
    parser.add_argument("--fee", type=float, default=0.65, help="Per-contract cost on each side")
    parser.add_argument("--paper-results", action="store_true")
    parser.add_argument("--data-dir", help="Offline SYMBOL.csv files with Date,Close columns (include SPY and warmup)")
    args = parser.parse_args()
    cfg = Settings.from_env()
    if args.paper_results:
        print(json.dumps(Ledger(cfg.db_path).report(starting_capital=cfg.virtual_starting_capital), indent=2))
        return
    if args.years <= 0:
        parser.error("--years must be positive")
    if args.starting_cash is not None and (not math.isfinite(args.starting_cash) or args.starting_cash<=0):
        parser.error('--starting-cash must be positive and finite')
    if args.compare_capital and any(not math.isfinite(v) or v<=0 for v in args.compare_capital):
        parser.error('--compare-capital values must be positive and finite')
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
    capitals=args.compare_capital or [args.starting_cash or cfg.virtual_starting_capital]
    for capital in capitals:
        # Explicit historical scenario: both virtual and collateral ceilings change together.
        run_cfg=replace(cfg,virtual_starting_capital=capital,max_collateral_per_trade=capital,max_total_collateral=capital) if args.compare_capital or args.starting_cash else cfg
        trades,curve,summary=simulate(histories,run_cfg,start,None,args.slippage,args.fee)
        suffix=f"_{capital:g}" if args.compare_capital else ""
        save_csv(f"logs/options_backtest_trades{suffix}.csv",trades,["symbol","strategy","entry_date","exit_date","strike","entry_credit","exit_debit","collateral","pnl","return_on_collateral","reason","hold_days","outcome"])
        save_csv(f"logs/options_backtest_equity_curve{suffix}.csv",curve,["date","cash","short_liability","reserved_collateral","equity"])
        rejected=summary.pop('rejected_opportunities')
        save_csv(f"logs/options_backtest_rejected{suffix}.csv",rejected,REJECTION_FIELDS+['capital_only'])
        Path('logs').mkdir(exist_ok=True)
        Path(f"logs/options_backtest_summary{suffix}.json").write_text(json.dumps(summary,indent=2))
        print(json.dumps(summary,indent=2))



if __name__ == "__main__":
    main()
