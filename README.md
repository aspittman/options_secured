# Options Secured

Alpaca **paper-trading** cash-secured put bot adapted from
[aspittman/options_direct](https://github.com/aspittman/options_direct), inspected at
commit `66737da21b00b51b25d93cca30388030d76f3a51`.

Retains the reference's Python module layout, Alpaca integration, daily signals,
five-minute monitoring, named strategies, limit orders, logging, and backtest/report
commands. Execution and accounting are rewritten for short puts. The original
long-call simulations and legacy-position adoption are not used.

## Strategy

New puts are sold only when SPY and the underlying are sideways to mildly bullish:
price at/above the 200-day SMA, 50-day SMA at least 99% of the 200-day SMA,
five-day SMA50 change between −1% and +2%, 20-day price return between −3% and +8%,
price within 5% of SMA50, and 20-day annualized realized volatility at most 35%.
Indicators use completed daily IEX candles; missing/stale data blocks entries.

Two variants share account-wide limits:

| Variant | Additional entry requirements |
| --- | --- |
| `sideways` | RSI 40–60; absolute 20-day return at most 4% |
| `bullish_pullback` | RSI 40–65; SMA50 at/above SMA200; price within 2% of SMA50 |

The sideways variant takes precedence when both qualify. Unlike the reference's
false-to-true entry trigger, an eligible regime can be entered on any completed
daily bar; at most one submission per underlying/bar is persisted across restarts.
A five-calendar-day cooldown follows a closing order with a fill.

Contracts: one standard 100-share, out-of-the-money put, 30–45 calendar DTE,
delta −0.25 ± 0.10, open interest ≥500, current-day volume ≥100, bid/ask spread ≤10%
of midpoint, and bid premium ≥0.5% of strike. Nonstandard roots/sizes are excluded.
Corporate earnings must be known to occur after expiration plus one day; unknown
earnings dates block corporate entries. The default ETF universe avoids earnings.

Sell-to-open uses a midpoint day limit. Buy-to-close uses an ask-priced day limit:

- Capture 50% of original credit.
- Exit when buyback price reaches 2× credit (a loss of approximately original credit).
- Exit at 7 DTE or when price falls below SMA200 / SMA50 falls below 98% of SMA200.

Limits can remain unfilled. An entry times out after 15 minutes; an exit after two
minutes. Cancellation must be confirmed before another order can be submitted.
Exit signals are reevaluated on subsequent five-minute cycles and repriced if still
active. These checks cannot guarantee an exit price or prevent early assignment.

These thresholds are starting hypotheses, **not empirically optimized settings**.

## Cash and reconciliation

Reserve `strike × 100 × contracts`, without subtracting expected premium. A $50 put
requires $5,000 of collateral even if its premium is only $100. Defaults cap one
trade at $25,000 and the portfolio at $50,000, retain $1,000 cash, and allow two
positions with one per correlation group. All default ETFs share one group, so
defaults allow one concurrent ETF position. Expensive strikes simply get skipped.

Cash checks include all account short puts and remaining open sell quantities,
including other positions. Margin buying power never substitutes for cash;
options buying power is an additional upper bound. External pending orders and
other short exposures block entries. Use a dedicated paper account: other bots or
manual activity can race any account snapshot, and Alpaca cash is not an independent
settled-funds ledger.

SQLite in `logs/options_secured.sqlite3` records intent **before** network submission,
cumulative partial fills, realized credit-minus-debit P/L, and order status.
An uncertain submission is looked up by its original client ID and never blindly
resent. Pending intent remains blocked if lookup fails, including a definite
rejection that did not produce a broker order; review that intent against Alpaca.
A process lock prevents two instances sharing this database.

Unexpected broker/ledger differences latch `RECONCILIATION_REQUIRED` and block new
entries. The ledger preserves the position and its basis; it does not invent an
expiration profit or silently delete an assigned put. Check Alpaca assignment/
expiration activities, reconcile shares, cash, and the ledger before resuming.
There is deliberately no automatic reset or stock liquidation. Other matching
bot positions continue exit monitoring. Assignment settlement/P&L is **manual** in
this version, and this bot does not run covered calls or an automatic wheel.

Alpaca documents [cash-secured put orders](https://docs.alpaca.markets/us/docs/options-orders)
and [assignment activities](https://docs.alpaca.markets/us/docs/options-trading-overview).
The [Options Industry Council strategy description](https://www.optionseducation.org/strategies/all-strategies/cash-secured-put)
explains the obligation to acquire shares: cash collateral does not prevent stock
losses after assignment.

## Setup and running

Python 3.11+ on Linux (uses `fcntl` for the process lock):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set your Alpaca **paper** credentials in `.env`. New entries default to disabled;
set `ENABLE_NEW_ENTRIES=true` when you want paper entries. `ALPACA_PAPER=false`
is rejected. No real-money execution is implemented.

```bash
python main.py --once
python main.py
python backtester.py --paper-results
python -m unittest discover -s tests -v
```

The indicative option feed may not meet the 120-second freshness requirement;
unusable quotes are skipped. OPRA requires the appropriate data access. Entries
may also be absent early in the day before the volume threshold is met.

`logs/options_bot.log` contains cycle results and errors. SQLite is the source of
truth; `logs/trade_analytics.csv` exports realized closing fills. Reports distinguish
realized P/L, current ask-marked unrealized P/L, collateral, and unmarked positions.
Offline paper reports leave unrealized P/L unknown. Broker fees are not included
in paper fill P/L. Do not delete the database while positions/orders remain open.

## Historical estimates

```bash
python backtester.py --years 1
python backtester.py --years 3 --starting-cash 100000
python backtester.py --years 1 --data-dir historical_data
```

Offline data requires `SYMBOL.csv` with `Date,Close` columns for each configured
underlying and SPY, including at least 205 trading days of warmup. Online mode
downloads daily underlying history from Yahoo Finance. Synthetic backtesting is
restricted to the four default ETFs because it has no earnings model.

The simulator shares entry/exit rules and portfolio limits, uses prior-day signals
and volatility, hypothetical next-close fills, Black–Scholes European put prices,
$0.05/share slippage per side, and $0.65/contract fees per side. It marks open short
liabilities daily and reports portfolio drawdown. It does **not** reproduce actual
chains, American early assignment, dividends, intraday stops, or liquidity filters.
This is a mechanics/research estimate, not evidence of executable profits or live
performance. Validate with actual historical option data and paper fills before
calling the strategy tuned. No actual-chain backtest is implemented in this version.

Outputs: `logs/options_backtest_trades.csv` and
`logs/options_backtest_equity_curve.csv`. Two variants share one portfolio; they
are not reported as separate accounts with duplicated starting cash.
