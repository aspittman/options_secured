## Trailing stops

Option-premium trailing stops are enabled for **oasis only** with
`OPTION_TRAILING_STOP_PERCENT=0.20`:

- Bought calls/puts (direct and inverted): sell when the observed option premium
  falls 20% below its highest observed premium for the current holding.
- Sold covered calls/cash-secured puts: buy back when the ask rebounds 20% above
  its lowest observed buyback price for the current holding.

Regular retains its previous controls: direct/inverted use their existing 30%
fixed option stops and 3% underlying trails; covered/secured keep their 2x-credit
fixed stops without an option-premium trail. Oasis alone uses the 20% fixed stop
and 20% premium trail. The trail starts from its entry premium.
Long-option highs are rebuilt from confirmed fills and durable premium snapshots;
short-option lows are persisted in a small ledger table keyed to the entry order.
The trail never loosens as prices reverse, survives restarts, and resets for a new
trade. Existing fixed stops, regular underlying-price trails, technical exits,
Oasis closing times, collateral controls, and the shared loss block remain active.
Stops are monitored limit-order exits and do not guarantee execution at the trigger.
An existing short option starts from its entry credit/current ask because earlier
unrecorded intraday lows cannot be reconstructed.

# Regular and Oasis runtime update

Active variants: **regular** (the original sideways cash-secured-put rules) and
**oasis**, which sells cash-secured puts on **bullish** EMA/momentum signals. Its
stop buys back the put when the ask reaches **120% of the entry credit**, a loss
of 20% of that credit. This is an option-credit stop, not 20% of reserved collateral.
Full cash collateral remains required until the close is confirmed. No naked puts,
long puts, calls, or stock orders are introduced. The retired `bullish_pullback`
variant accepts no new trades, while historical labels and daily exit rules remain.

The regular variant keeps the existing daily entry and exit rules. Oasis uses
completed regular-session 5-minute candles: a 9/21 EMA cloud, both EMAs moving in
the trade direction, RSI(14), and a strengthening MACD(12/26/9) histogram. Bullish
entries require RSI between 50 and 70; bearish entries require RSI between 30 and
50. Only a fresh false-to-true setup can enter. Stale, incomplete and prior-session
bars cannot trigger entries. The existing daily market filter and contract-quality,
earnings, ownership and capital limits still apply.

Oasis stops opening entries 30 minutes before Alpaca's reported stock-session close
and starts closing its options 15 minutes before close, including shortened
sessions. Pending entries are canceled at the cutoff; cancellation remains pending
until confirmed by the broker. Any overnight remainder is closed on the next open
cycle. Momentum/cloud breakdown can exit earlier. Risk/order checks run every
60 seconds plus processing time. The original expiration windows are retained.
These are monitored limit-order exits, not guaranteed fills or maximum losses.
Delayed indicative option quotes limit intraday paper-execution realism.

A confirmed loss blocks all new contracts on that underlying through 30 calendar
days after the loss; reentry is permitted on day 31. The check applies across
regular, oasis and historical variants, survives restarts, and is enforced at the
final entry gate. Winning exits do not start/reset the loss block. Regular keeps
its existing ordinary reentry cooldown; oasis uses only the loss block and signal
bar deduplication. Pending closing orders must reconcile before reentry.

By default the block is also shared across all four sibling bots via read-only
checks of these ledgers:

- `options_direct/logs/trade_analytics.csv`
- `options_inverted/logs/trade_analytics.csv`
- `options_covered/logs/trades.sqlite3`
- `options_secured/logs/options_secured.sqlite3`

Set `LOSS_GUARD_SCOPE=bot` for checks only within each bot. For custom ledger
locations, set `LOSS_LEDGER_PATHS` to a JSON object mapping bot directory names to
absolute paths. Missing default ledgers are ignored; existing unreadable ledgers
block entries until readable. This conservatively combines the configured ledgers
without inferring whether they belong to the same taxpayer/account. Use ledgers
from the intended trading environment. No external/manual accounts are inspected.
This is not wash-sale tax accounting: it does not resolve substantially-identical
instruments, the pre-loss purchase window, or unrecorded transactions.

Historical daily backtests remain historical research, not Oasis simulations, and
do not simulate the new shared loss block. Existing trade records are preserved.
The changes load on the next restart; installation does not start or restart bots.

---

Existing setup and historical research reference:

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
earnings dates block corporate entries. Registered ETFs skip corporate earnings calendars; individual stocks retain the
full holding-window earnings check.

Sell-to-open uses a midpoint day limit. Buy-to-close uses an ask-priced day limit:

- Capture 50% of original credit.
- Exit when buyback price reaches 2× credit (a loss of approximately original credit).
- Exit at 7 DTE or when price falls below SMA200 / SMA50 falls below 98% of SMA200.

Limits can remain unfilled. An entry times out after 15 minutes; an exit after two
minutes. Cancellation must be confirmed before another order can be submitted.
Exit signals are reevaluated on subsequent five-minute cycles and repriced if still
active. These checks cannot guarantee an exit price or prevent early assignment.

These thresholds are starting hypotheses, **not empirically optimized settings**.

## Expanded research universe and premium highlights

The default live/paper universe is 59 symbols (29 ETFs/funds and 30 stocks), configured with `UNDERLYINGS`:

| Category | Symbols |
| --- | --- |
| Original broad ETFs | SPY, QQQ, IWM, DIA |
| Sector/regional/international ETFs | XLF, XLE, XLP, XLU, XLI, XLB, XLV, XLRE, KRE, XBI, EEM, EWZ, EFA |
| Additional requested funds | ARKK, VNQ, GDX, GLD, SLV, TLT, HYG, LQD, USO, XOP, TAN, IGV |
| Stocks | BAC, C, WFC, F, GM, T, VZ, PFE, KO, CSCO, INTC, HPQ, KR, WMT, DIS, XOM, AAPL, AMD, MU, SOFI, SNAP, NCLH, UBER, PINS, RIVN, HOOD, ROKU, PYPL, CVX, OXY |

The universe includes every symbol requested from the covered-call bot and retains
the previous additions. The first expansion was checked against Alpaca on
September 11, 2026; the subsequent symbols are user-specified additions whose
current data, contracts and eligibility are checked during each scan.
This is an expanded search universe, not an assertion that every symbol has an
eligible setup or adequate option liquidity. Prices and contract availability change.
ETF identities are listed explicitly in `universe.py`; related funds and stocks
share correlation limits (for example XLF/KRE/BAC/WFC and XLE/XOM). Existing groups
and the two-position maximum remain in effect. Metal funds share a precious-metals
group, bond funds share a fixed-income group, and energy-related additions share
the existing energy group. These are conservative exposure buckets, not measured
correlation estimates. The same SPY market filter and underlying strategy rules
apply to every symbol, including commodity and bond funds. Fund listings:
[State Street sector ETFs](https://www.ssga.com/us/en/individual/capabilities/equities/sector-investing/select-sector-etfs),
[State Street fund lineup](https://www.ssga.com/library-content/pdfs/etf/us/spdr-product-line-up/lpl.pdf),
[iShares funds](https://www.ishares.com/us/products/etf-investments).

Signals, DTE, delta, liquidity, spread, earnings, collateral, cash and ownership
rules are unchanged. `MAX_POSITIONS=2` limits simultaneous positions, not lifetime
trades. The broader universe expands scanned/rejected opportunities but does not
guarantee more fills, especially while existing positions reserve the allocation. Preferred contracts are still selected before checking
capital, with no cheaper-strike fallback. A `$500` premium is **not** a trade limit
or a ranking preference. For a short put it is income received, not the cash
required to enter the position; assignment collateral remains strike × 100.
Lower premium or notional does not establish profitability in real trading.

Each confirmed opening fill or buyback logs its actual total option premium:
`fill price × filled contracts × 100`. Totals from $0.01 through $500.00 are marked
`[PREMIUM <= $500]` and displayed in bold cyan on an interactive terminal.
`SELL_TO_OPEN` shows `credit_received`; `BUY_TO_CLOSE` shows `debit_paid`, not profit.
The line also shows strike collateral, so a small premium is not confused with a
small cash obligation. Pending, rejected, and simulated trades are never labeled
as confirmed fills. Repeated reconciliation does not repeat the highlight.
File logs retain the searchable marker without ANSI color codes. Redirected output
also retains the marker; set `NO_COLOR` to suppress terminal color if desired.

After updating a running foreground bot, press Ctrl+C and run `python3 launcher.py`
again to load the new universe. No background process is created by this change.

## Cash-secured-put-only execution guard

Immediately before sending an order, the Alpaca adapter requires a fresh matching
intent in this bot's ledger, the `cash_secured_put` client ID, and verified broker
metadata for a standard 100-share put whose underlying, strike and expiry match.
Calls, stocks, adjusted contracts, and mismatched contract descriptions are blocked.

A sale is always `sell_to_open`, with both virtual collateral and account cash
rechecked at submission. A purchase is always `buy_to_close`, requiring this bot's
ledger-owned short put, the matching broker short quantity, and no competing order
for that contract. New long puts and unowned buybacks are blocked. Existing owned
put exits remain available when entry limits are exceeded or entries are disabled.
Assigned shares may still be delivered by Alpaca; this bot tracks them but does
not submit stock trades or start a covered-call strategy.

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
limits. The four original broad ETFs share a group; added ETFs share appropriate sector
or regional groups with related holdings.
Unrelated stocks, long puts, and calls are never adopted, closed, or cancelled.
Same-contract exposure is rejected because Alpaca nets positions. Unknown pending
market-buy debits block new entries conservatively; known limit buys reserve their
maximum debit. Shared-account snapshots cannot eliminate races with other bots.

New IDs use `cash_secured_put_<underlying>_<unique suffix>`. Existing ledger-recorded
`os-` IDs remain owned and manageable. Prefix alone never grants ownership. New submissions must carry the strategy
prefix, and cancellation re-fetches the broker order and verifies strategy ID,
ledger ownership, contract, side, and broker ID immediately before cancelling.
There is no order-modification endpoint in this bot; repricing uses guarded
cancellation followed by a newly tagged order.

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
python launcher.py  # foreground; Ctrl+C stops the bot
python main.py      # equivalent foreground entry point
python backtester.py --paper-results
python -m unittest discover -s tests -v
```

Both launchers stay attached to the terminal and print logs there. Ctrl+C stops
the process and releases its lock; the next launch resumes from the existing
ledger. No background process or automatic restart is created. Stopping the bot
does not close broker positions or cancel outstanding orders; reconciliation
resumes on the next launch.

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
restricted to the 29 explicitly registered ETFs/funds because it has no historical
earnings model. The CLI prints and records the excluded stock symbols in each
summary before running the ETF subset; the live universe is not changed. Offline
data therefore needs SPY and the configured registered ETFs. A stock-only universe
is rejected for historical simulation rather than bypassing earnings controls.

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
