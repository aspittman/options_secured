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

Two signal variants share one cash-secured-put virtual allocation:

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

The research allocation is **$25,000 for the whole CashSecuredPutBot**, shared by
its `sideways` and `bullish_pullback` variants. The other three bots are separate
projects and do not contribute equity, P/L, or virtual position slots here.

```dotenv
VIRTUAL_STARTING_CAPITAL=25000
MAX_CONTRACTS_PER_TRADE=1
MAX_COLLATERAL_PER_TRADE=25000
MAX_TOTAL_COLLATERAL=25000
```

Capital employed is full `strike × 100 × contracts`. No $500 premium cap applies.
A $50 put requires $5,000; a $175 put requires $17,500; a $300 put is rejected.
First rank valid contracts by the existing delta/spread rules, then check the
**preferred** contract's collateral. If it exceeds the allocation, log the rejection;
never choose a lower strike to fit the budget. This deliberately replaces the
September 9 $80,000 settings and may produce no ETF entries at current prices.

Available virtual capital is `min(starting capital, starting capital + realized P/L,
MAX_TOTAL_COLLATERAL) − reserved capital`, floored at zero. Pending sell quantities
and assigned share cost remain reserved. Opening premium is unavailable until
realized; losses reduce the budget and gains never raise its fixed ceiling. The
broker's larger account balance cannot increase a trade or strategy allocation.

The independent broker-cash check retains the $1,000 account cash buffer, deducts
all account short-put obligations and known pending buy debits, and also requires
sufficient Alpaca options buying power. The SDK path does not supply a separate
contract-level collateral preview, so full strike collateral is the conservative
fallback. The $1,000 buffer applies to account cash, not as an extra reduction of
the virtual $25,000 allocation. Both checks must pass.

Only ledger-owned puts count toward the two-position and one-per-correlation-group
limits. All four default ETFs share a group, so at most one can be open here.
Unrelated stocks, long puts, and calls are never adopted, closed, or cancelled.
Same-contract exposure is rejected because Alpaca nets positions. Unknown pending
market-buy debits block new entries conservatively; known limit buys reserve their
maximum debit. Shared-account snapshots cannot eliminate races with other bots.

New IDs use `cash_secured_put_<underlying>_<unique suffix>`. Existing ledger-recorded
`os-` IDs remain owned and manageable. Prefix alone never grants ownership.

SQLite in `logs/options_secured.sqlite3` records intent **before** network submission,
cumulative partial fills, realized credit-minus-debit P/L, and order status.
An uncertain submission is looked up by its original client ID and never blindly
resent. Pending intent remains blocked if lookup fails, including a definite
rejection that did not produce a broker order; review that intent against Alpaca.
A process lock prevents two instances sharing this database.

When a ledger put disappears or shrinks, the bot checks Alpaca `OPASN`/`OPEXP`
activities. Assignment requires a matching `OPTRD` share delivery at the strike.
Only confirmed owned quantities are settled, idempotently by activity ID. Expired
options realize remaining premium; assigned options realize premium while their
share cost stays reserved and stock P/L is marked separately. Assignment pauses
new entries for review; it does not start covered calls or liquidate stock.
Unexplained discrepancies still latch `RECONCILIATION_REQUIRED`; no fabricated
expiration gains or automatic ledger reset occurs. Assigned-share disposal and
releasing that capital require manual reconciliation in this version.

The existing $74,100 SPY lot predates this policy. It retains normal exit management
and consumes capital, blocking new entries. Reports flag `legacy_over_allocation`
and `research_comparable=false` if historical entries exceeded the allocation.
Its history is retained rather than disguised as a compliant $25,000 experiment.
Pending own entries outside the new allocation are cancelled; filled positions
are not force-closed solely because the configuration changed.

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

`logs/options_bot.log` contains startup settings, cycle metrics, contract-filter
counts and errors, under the `cash_secured_put` logger. SQLite remains the source
of truth. `logs/performance_summary.json` is the most recent market-hours report.
`logs/trade_analytics.csv` exports realized P/L with strategy ID and signal variant.

`logs/rejected_trades.csv` exports qualifying-signal rejection observations:
strategy/variant, underlying, contract, put/short direction, strike, expiration,
DTE, spot, bid/ask/mid, dollar/percentage spread, premium, required collateral,
virtual capital available, reason, signal date and market regime. Missing data is
blank; `signal_score` is blank because the strategy has boolean rules, not a score.
Reasons include `COLLATERAL_OVER_LIMIT`, `MAX_STRATEGY_EXPOSURE_REACHED`,
`MAX_CONTRACTS_REACHED`, `DUPLICATE_POSITION`, `INSUFFICIENT_LIQUIDITY`,
`SPREAD_TOO_WIDE`, `NO_VALID_CONTRACT`, `INSUFFICIENT_BROKER_CASH` and `OTHER`.
Metadata/liquidity rejections and final preferred-contract rejections are separate
observations. Repeated scans can observe the same contract again: group by
underlying, signal date and contract for unique-opportunity research. These rows
never increment executed trades or P/L.

Reports include virtual starting/ending equity, realized/unrealized P/L, return %,
return on capital employed, average/max/current collateral, exposure %, trade count,
win rate, average/largest winners and losers, expectancy, profit factor, observed
max drawdown, hold days, received premium, assignment/expiration/buyback rates and
premium return on collateral. Returns use one $25,000 starting allocation; capital
returns use cumulative entry collateral (not account equity or received premium).
Outcome rates use completed option positions; assigned stock remains marked and
reserved separately. Decimal rates use 0–1, while `_pct` values use 0–100.

Unknown marks produce null equity/return, not zero P/L. Offline `--paper-results`
shows unmarked open positions; use the timestamped live log/report for current
marks. Profit factor is null when no losses exist. Drawdown is based on recorded
marked samples from this upgrade onward, not invented historical marks. Imported
legacy fill times and date-only settlement hold times are flagged as estimates.
Paper P/L excludes broker fees; backtest costs are explicit. Preserve the database
while positions or orders remain open.

## Historical estimates

```bash
python backtester.py --years 1
python backtester.py --years 3 --compare-capital 10000 25000 50000
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

Default simulation allocation is $25,000. `--starting-cash` sets a single virtual
capital/collateral scenario; `--compare-capital` runs independent scenarios against
the same downloaded history. Signals, delta targets and liquidity settings are
never loosened by these flags. The synthetic simulator cannot count actual
historical spread/liquidity rejections and reports that limitation explicitly.

Outputs include `options_backtest_trades.csv`, `options_backtest_equity_curve.csv`,
`options_backtest_rejected.csv` and `options_backtest_summary.json` in `logs/`.
Comparison runs suffix each filename with the capital amount. Summaries count
qualified symbol/day signals, executed entries, rejection reasons and rejections
solely due to capital after other modeled rules pass. Two variants share one portfolio; they
are not reported as separate accounts with duplicated starting cash.
