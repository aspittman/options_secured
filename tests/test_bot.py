import math
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import pandas as pd

from analytics import Ledger
from backtester import put_price, simulate
from config import Settings
from options_trader import AlpacaBroker, Candidate, Trader, valid_quote
from risk import capacity
from strategy import bearish_at, entry_at, exit_reason, indicators, regime_at


def candidate(root="IWM", strike=100):
    expiry = date.today() + timedelta(days=35)
    return Candidate(f"{root}{expiry:%y%m%d}P{int(strike * 1000):08d}", root, strike, expiry, 1.0, 1.05)


def account(cash=100000, buying_power=100000):
    return NS(cash=str(cash), options_buying_power=str(buying_power),
              options_trading_level=1, trading_blocked=False, account_blocked=False)


def quote(bid=1, ask=1.05, age=0):
    return NS(bid_price=bid, ask_price=ask, timestamp=datetime.now(timezone.utc) - timedelta(seconds=age))


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Settings()
        self.c = candidate()

    def test_cash_not_leveraged_buying_power(self):
        self.assertEqual(capacity(self.cfg, account(5000, 100000), [], [], self.c), (False, "INSUFFICIENT_BROKER_CASH"))
        self.assertTrue(capacity(self.cfg, account(11000), [], [], self.c)[0])

    def test_filled_external_put_reserves_full_strike(self):
        external = candidate("BAC", 50)
        position = NS(symbol=external.symbol, qty="-1")
        self.assertEqual(capacity(self.cfg, account(15000), [position], [], self.c)[1], "INSUFFICIENT_BROKER_CASH")

    def test_pending_partial_fill_reserves_remaining(self):
        c = candidate("BAC", 50)
        position = NS(symbol=c.symbol, qty="-1")
        order = NS(symbol=c.symbol, qty="2", filled_qty="1", side="sell", client_order_id="os-test")
        self.assertEqual(capacity(self.cfg, account(20000), [position], [order], self.c)[1], "INSUFFICIENT_BROKER_CASH")
        self.assertTrue(capacity(self.cfg, account(21000), [position], [order], self.c)[0])

    def test_correlation_and_stock_exposure(self):
        position = NS(symbol=candidate("SPY").symbol, qty="-1")
        own={position.symbol:dict(underlying='SPY',strike=100,qty=1)}
        self.assertEqual(capacity(self.cfg,account(),[position],[],self.c,own_lots=own)[1],"MAX_STRATEGY_EXPOSURE_REACHED")
        # A different strategy's stock position does not consume this bot's slots.
        self.assertTrue(capacity(self.cfg,account(),[NS(symbol='IWM',qty='100')],[],self.c)[0])

    def test_external_open_buy_blocks(self):
        order = NS(symbol="BAC", qty="100", filled_qty="0", side="buy", client_order_id="manual")
        self.assertEqual(capacity(self.cfg, account(), [], [order], self.c)[1], "OTHER")

    def test_collateral_caps(self):
        self.assertEqual(capacity(self.cfg, account(), [], [], candidate(strike=900))[1], "COLLATERAL_OVER_LIMIT")
        cfg = replace(self.cfg, max_total_collateral=5000)
        self.assertEqual(capacity(cfg, account(), [], [], self.c)[1], "MAX_STRATEGY_EXPOSURE_REACHED")

    def test_virtual_ceiling_supersedes_previous_etf_alignment(self):
        spy=candidate('SPY',741)
        self.assertEqual(capacity(self.cfg,account(500000),[],[],spy)[1],'COLLATERAL_OVER_LIMIT')
        for strike in (50,175,250):
            self.assertTrue(capacity(self.cfg,account(500000),[],[],candidate(strike=strike))[0])
        self.assertEqual(capacity(self.cfg,account(500000),[],[],candidate(strike=300))[1],'COLLATERAL_OVER_LIMIT')

    def test_quote_sanity_and_age(self):
        self.assertTrue(valid_quote(quote(), 120))
        for q in (quote(age=121), quote(bid=2, ask=1), quote(bid=float("nan")), quote(ask=float("inf")), quote(age=-60)):
            self.assertFalse(valid_quote(q, 120))
        self.assertFalse(valid_quote(quote(bid=0, ask=.05), 120))
        self.assertTrue(valid_quote(quote(bid=0, ask=.05), 120, for_exit=True))

    def test_invalid_config(self):
        for values in ({"paper": False}, {"exit_dte": 40}, {"target_delta": .25},
                       {"cash_buffer": -1}, {"max_total_collateral": math.nan}, {"profit_capture": 1}):
            with self.assertRaises(ValueError):
                Settings(**values)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "ledger.sqlite3")
        self.ledger = Ledger(self.path)
        self.c = candidate()

    def tearDown(self):
        self.ledger.db.close()
        self.temp.cleanup()

    def intent(self, key, side="sell", qty=2):
        self.ledger.intent(key, self.c, "sideways", side, qty, date.today().isoformat())

    def test_partial_fills_are_idempotent_and_persist(self):
        self.intent("entry")
        self.ledger.reconcile("entry", "broker", "partially_filled", 1, 2)
        self.ledger.reconcile("entry", "broker", "partially_filled", 1, 2)
        self.assertEqual(self.ledger.lots()[self.c.symbol]["qty"], 1)
        self.assertEqual(len(self.ledger.pending()), 1)
        self.ledger.reconcile("entry", "broker", "filled", 2, 3)
        reopened = Ledger(self.path)
        self.assertEqual(reopened.lots()[self.c.symbol]["credit"], 3)
        reopened.db.close()
        self.intent("exit", "buy")
        self.ledger.reconcile("exit", "broker2", "partially_filled", 1, 1)
        self.ledger.reconcile("exit", "broker2", "canceled", 1, 1)
        report = self.ledger.report({self.c.symbol: 2})["by_strategy"]["sideways"]
        self.assertEqual(report["realized_pnl"], 200)
        self.assertEqual(report["unrealized_pnl"], 100)
        self.assertEqual(report["collateral"], 10000)
        self.assertEqual(self.ledger.pending(), [])

    def test_zero_fill_cancellation_never_creates_profit(self):
        self.intent("entry")
        self.ledger.reconcile("entry", "broker", "canceled", 0, 0)
        self.assertEqual(self.ledger.lots(), {})
        self.assertEqual(self.ledger.report()["by_strategy"], {})

    def test_close_cannot_overbuy_or_corrupt_ledger(self):
        self.intent("entry", qty=1)
        self.ledger.reconcile("entry", "broker", "filled", 1, 2)
        self.intent("exit", "buy", qty=2)
        with self.assertRaises(ValueError):
            self.ledger.reconcile("exit", "broker2", "filled", 2, 1)
        self.assertEqual(self.ledger.lots()[self.c.symbol]["qty"], 1)
        self.assertEqual(self.ledger.pending()[0]["filled"], 0)


class FakeBroker:
    def __init__(self):
        self.submitted = []
        self.open = []
        self.pos = []
        self.by_id = {}
        self.canceled = []
        self.fail_submit = False

    def account(self):
        return account()

    def positions(self):
        return self.pos

    def orders(self):
        return self.open

    def lookup(self, client_id):
        if client_id not in self.by_id:
            raise RuntimeError("Order unavailable")
        return self.by_id[client_id]

    def cancel(self, broker_id):
        self.canceled.append(broker_id)

    def earnings_clear(self, root, expiry):
        return True

    def snapshot(self, symbol):
        return NS(latest_quote=quote())

    def history(self, symbol):
        raise RuntimeError("Daily data outage")

    def submit(self, candidate, qty, side, price, client_id):
        self.submitted.append((candidate, qty, side, price, client_id))
        if self.fail_submit:
            raise TimeoutError("Connection lost after submit")
        order = NS(id="broker-" + client_id, status="new", filled_qty=0, filled_avg_price=None,
                   symbol=candidate.symbol, side=side, qty=qty, client_order_id=client_id)
        self.by_id[client_id] = order
        self.open.append(order)
        return order


class TraderTests(LedgerTests):
    def setUp(self):
        super().setUp()
        self.broker = FakeBroker()
        self.trader = Trader(replace(Settings(), enable_entries=True), self.broker, self.ledger)

    def test_sell_entry_and_duplicate_prevention(self):
        self.assertTrue(self.trader.enter(self.c, "sideways", "2026-09-04"))
        self.assertEqual(self.broker.submitted[0][2], "sell")
        self.assertFalse(self.trader.enter(self.c, "sideways", "2026-09-04"))
        self.assertEqual(len(self.broker.submitted), 1)

    def test_ambiguous_submit_blocks_retry(self):
        self.broker.fail_submit = True
        self.assertFalse(self.trader.enter(self.c, "sideways", "2026-09-04"))
        self.assertFalse(self.trader.enter(self.c, "sideways", "2026-09-05"))
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertEqual(self.ledger.pending()[0]["status"], "intent")

    def test_cancellation_request_retains_reservation(self):
        self.trader.enter(self.c, "sideways", "2026-09-04")
        row = self.ledger.pending()[0]
        with self.ledger.db:
            self.ledger.db.execute("UPDATE orders SET created=?", ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),))
        self.assertTrue(self.trader.reconcile())
        self.assertEqual(self.broker.canceled, [row["broker_id"]])
        self.assertEqual(len(self.ledger.pending()), 1)

    def test_assignment_mismatch_preserves_basis_and_blocks(self):
        self.intent("entry", qty=1)
        self.ledger.reconcile("entry", "broker", "filled", 1, 2)
        self.broker.pos = [NS(symbol="IWM", qty=100)]
        self.assertFalse(self.trader.reconcile())
        self.assertTrue(self.ledger.blocked())
        self.assertEqual(self.ledger.lots()[self.c.symbol]["qty"], 1)
        self.assertEqual(self.ledger.report()["by_strategy"]["sideways"]["realized_pnl"], 0)

    def test_profit_exit_buys_to_close_despite_daily_data_outage(self):
        self.intent("entry", qty=1)
        self.ledger.reconcile("entry", "broker", "filled", 1, 3)
        self.broker.pos = [NS(symbol=self.c.symbol, qty=-1)]
        self.trader.manage_exits()
        self.assertEqual(self.broker.submitted[0][2], "buy")
        self.assertEqual(self.broker.submitted[0][3], 1.05)

    def test_disabled_entries_still_manage_exits(self):
        self.trader.cfg = replace(self.trader.cfg, enable_entries=False)
        self.assertFalse(self.trader.enter(self.c, "sideways", "2026-09-04"))
        self.test_profit_exit_buys_to_close_despite_daily_data_outage()


class StrategyTests(unittest.TestCase):
    def test_regime_accepts_sideways_bull_rejects_bear_and_rally(self):
        frame = pd.DataFrame([dict(close=101, sma50=100, sma200=99, sma50_change=.001,
                                   return20=.01, vol=.15, rsi=50)])
        cfg = Settings()
        self.assertTrue(regime_at(frame, 0, cfg))
        self.assertTrue(entry_at(frame, 0, "sideways", cfg))
        self.assertTrue(entry_at(frame, 0, "bullish_pullback", cfg))
        frame.loc[0, "close"] = 95
        self.assertFalse(regime_at(frame, 0, cfg))
        self.assertTrue(bearish_at(frame, 0))
        frame.loc[0, "close"] = 101
        frame.loc[0, "return20"] = .15
        self.assertFalse(regime_at(frame, 0, cfg))

    def test_short_put_exit_math(self):
        cfg = Settings()
        self.assertEqual(exit_reason(2, 1, 30, False, cfg), "profit_capture")
        self.assertEqual(exit_reason(2, 4, 30, False, cfg), "credit_stop")
        self.assertEqual(exit_reason(2, 2, 7, False, cfg), "expiration_risk")
        self.assertEqual(exit_reason(2, 2, 30, False, cfg), "")

    def test_put_payoff(self):
        self.assertEqual(put_price(90, 100, 0, .2), 10)
        self.assertEqual(put_price(110, 100, 0, .2), 0)
        self.assertGreater(put_price(90, 100, 30, .2), put_price(110, 100, 30, .2))

    def test_indicators_do_not_look_ahead(self):
        closes = pd.Series([100 + math.sin(i / 4) + i * .01 for i in range(350)])
        full, truncated = indicators(closes), indicators(closes.iloc[:300])
        pd.testing.assert_frame_equal(full.iloc[:300], truncated)

    def test_backtest_marks_liabilities_and_enforces_cash(self):
        dates = pd.bdate_range("2024-01-01", periods=450)
        closes = pd.Series([100 + 2 * math.sin(i / 5) + i * .01 for i in range(450)], index=dates)
        cfg = replace(Settings(), underlyings=("SPY",), min_credit_yield=.0001)
        trades, curve, summary = simulate({"SPY": closes}, cfg, dates[205].date(), 20000)
        self.assertTrue(trades or summary["open_positions"])
        for row in curve:
            self.assertAlmostEqual(row["equity"], row["cash"] - row["short_liability"])
            self.assertLessEqual(row["reserved_collateral"], row["cash"] - cfg.cash_buffer)
        trades, curve, summary = simulate({"SPY": closes}, cfg, dates[205].date(), 1000)
        self.assertEqual(trades, [])
        self.assertEqual(summary["ending_equity"], 1000)


class AdapterTests(unittest.TestCase):
    def test_sdk_requests_have_explicit_short_put_intents(self):
        broker = AlpacaBroker.__new__(AlpacaBroker)
        captured = []
        broker.trading = NS(submit_order=lambda **kwargs: captured.append(kwargs["order_data"]))
        c = candidate()
        broker.submit(c, 1, "sell", 1.05, "os-entry")
        broker.submit(c, 1, "buy", .50, "os-exit")
        self.assertEqual(captured[0].position_intent.value, "sell_to_open")
        self.assertEqual(captured[1].position_intent.value, "buy_to_close")
        self.assertEqual(captured[0].time_in_force.value, "day")

    def test_environment_numbers_allow_decimal_collateral(self):
        with patch.dict("os.environ", {"MAX_COLLATERAL_PER_TRADE": "5000.50", "ALPACA_PAPER": "true"}):
            self.assertEqual(Settings.from_env().max_collateral_per_trade, 5000.5)


if __name__ == "__main__":
    unittest.main()
