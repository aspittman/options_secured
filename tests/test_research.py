import csv
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
from types import SimpleNamespace as NS
from unittest.mock import patch
import pandas as pd
import math
from config import Settings
from risk import capacity, virtual_capacity
from analytics import Ledger
from options_trader import Candidate,Trader,AlpacaBroker
from main import cycle
from backtester import simulate


def contract(strike=175,root='IWM'):
    expiry=date.today()+timedelta(days=35)
    return Candidate(f'{root}{expiry:%y%m%d}P{int(strike*1000):08d}',root,strike,expiry,2,2.05,-.25,strike*1.05)


def account(cash=500000):
    return NS(cash=cash,options_buying_power=cash,options_trading_level=3,trading_blocked=False,account_blocked=False)


class ResearchCapitalTests(unittest.TestCase):
    def test_fixed_allocation_no_account_scaling(self):
        c=Settings()
        for cash in [50000,500000,5000000]:
            self.assertEqual(capacity(c,account(cash),[],[],contract(300))[1],'COLLATERAL_OVER_LIMIT')
            self.assertTrue(capacity(c,account(cash),[],[],contract(175))[0])
        self.assertEqual(virtual_capacity(c,25001,realized=999999)[1],'COLLATERAL_OVER_LIMIT')
        self.assertEqual(virtual_capacity(c,25000,realized=-1)[1],'MAX_STRATEGY_EXPOSURE_REACHED')

    def test_aggregate_pending_and_open_collateral(self):
        pending=[dict(side='sell',symbol='pending',underlying='BAC',qty=1,filled=0,strike=100)]
        ok,reason=capacity(Settings(),account(),[],[],contract(175),own_pending=pending)
        self.assertFalse(ok)
        self.assertEqual(reason,'MAX_STRATEGY_EXPOSURE_REACHED')

    def test_other_strategies_do_not_take_virtual_slots(self):
        call=contract(100,'AAPL').symbol.replace('P','C')
        longput=contract(100,'QQQ').symbol
        positions=[NS(symbol=call,qty=-1),NS(symbol=longput,qty=1),NS(symbol='AAPL',qty=100)]
        orders=[NS(symbol=call,side='sell',qty=1,filled_qty=0,client_order_id='covered_call_AAPL_123')]
        self.assertTrue(capacity(Settings(),account(),positions,orders,contract())[0])
        self.assertEqual(capacity(Settings(),account(),[NS(symbol=contract().symbol,qty=1)],[],contract())[1],'DUPLICATE_POSITION')

    def test_one_contract_configuration(self):
        with self.assertRaises(ValueError): Settings(max_contracts_per_trade=2)


class ResearchLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.ledger=Ledger(str(Path(self.temp.name)/'ledger.db'))
        self.c=contract(175)

    def tearDown(self):
        self.ledger.db.close(); self.temp.cleanup()

    def entry(self,credit=2):
        self.ledger.intent('cash_secured_put_entry',self.c,'sideways','sell',1)
        self.ledger.reconcile('cash_secured_put_entry','broker','filled',1,credit)

    def test_rejection_has_quotes_and_capital_without_trade(self):
        self.ledger.reject('COLLATERAL_OVER_LIMIT',contract(300),Settings(),variant='sideways',signal_date='2026-09-09')
        self.ledger.export(str(Path(self.temp.name)/'trades.csv'))
        with (Path(self.temp.name)/'rejected_trades.csv').open() as f:
            row=next(csv.DictReader(f))
        self.assertEqual(row['required_capital'],'30000')
        self.assertEqual(float(row['virtual_capital_available']),25000)
        self.assertEqual(row['strategy'],'cash_secured_put')
        self.assertEqual(self.ledger.report()['metrics']['trade_count'],0)
        self.assertEqual(self.ledger.pending(),[])

    def test_returns_use_virtual_start_and_collateral(self):
        self.entry()
        self.ledger.record_equity({self.c.symbol:3},Settings())
        report=self.ledger.report({self.c.symbol:3})['metrics']
        self.assertEqual(report['ending_virtual_capital'],24900)
        self.assertAlmostEqual(report['total_return_pct'],-.4)
        self.assertEqual(report['premium_received'],200)
        self.assertEqual(report['average_collateral_committed'],17500)
        self.ledger.intent('close',self.c,'sideways','buy',1)
        self.ledger.reconcile('close','broker2','filled',1,1)
        metrics=self.ledger.report()['metrics']
        self.assertEqual(metrics['trade_count'],1)
        self.assertEqual(metrics['realized_pnl'],100)
        self.assertEqual(metrics['win_rate'],1)
        self.assertEqual(metrics['buy_to_close_rate'],1)
        self.assertEqual(metrics['ending_virtual_capital'],25100)
        self.assertEqual(self.ledger.available(Settings()),25000)

    def test_unknown_marks_are_not_zero_pnl(self):
        self.entry()
        self.assertIsNone(self.ledger.report()['metrics']['unrealized_pnl'])
        self.assertIsNone(self.ledger.report()['metrics']['ending_virtual_capital'])

    def test_confirmed_assignment_keeps_capital_and_marks_stock(self):
        with patch('analytics.now_iso', return_value='2026-09-14T16:00:00+00:00'):
            self.entry()
        day='2026-09-14'
        activity=dict(id='assignment1',symbol=self.c.symbol,qty=1,date=day,activity_type='OPASN',status='executed')
        self.assertFalse(self.ledger.settlement(activity))
        stock=dict(symbol='IWM',qty=100,price=175,date=day,activity_type='OPTRD',status='executed')
        self.assertTrue(self.ledger.settlement(activity,stock))
        self.assertFalse(self.ledger.settlement(activity,stock))
        self.assertEqual(self.ledger.reserved(),17500)
        self.assertTrue(self.ledger.blocked())
        report=self.ledger.report({'stock:IWM':170})['metrics']
        self.assertEqual(report['realized_pnl'],200)
        self.assertEqual(report['unrealized_pnl'],-500)
        self.assertEqual(report['ending_virtual_capital'],24700)
        self.assertEqual(report['assignment_rate'],1)

    def test_expiration_requires_broker_proof(self):
        self.entry()
        a=dict(id='exp1',symbol=self.c.symbol,qty=1,date=self.c.expiry.isoformat(),activity_type='OPEXP',status='executed')
        self.assertTrue(self.ledger.settlement(a))
        self.assertEqual(self.ledger.report()['metrics']['expiration_rate'],1)
        self.assertEqual(self.ledger.reserved(),0)

    def test_restart_migration_preserves_fills_and_legacy_lot(self):
        c=contract(741,'SPY')
        self.ledger.intent('os-legacy',c,'sideways','sell',1)
        self.ledger.reconcile('os-legacy','oldbroker','filled',1,6.13)
        reopened=Ledger(str(Path(self.temp.name)/'ledger.db'))
        self.assertEqual(reopened.available(Settings()),0)
        self.assertTrue(reopened.report()['metrics']['legacy_over_allocation'])
        self.assertEqual(reopened.db.execute('select count(*) from fills').fetchone()[0],1)
        reopened.db.close()


class SelectionTests(unittest.TestCase):
    def test_no_cheaper_fallback_after_preferred_rejection(self):
        frame=pd.DataFrame({'close':[300]},index=pd.DatetimeIndex(['2026-09-09']))
        broker=NS(trading=NS(get_clock=lambda:NS(is_open=True, timestamp=datetime.now(timezone.utc),
                      next_close=datetime.now(timezone.utc)+timedelta(hours=2))),history=lambda s:frame,
                  candidates=lambda s:[contract(300),contract(175)])
        from unittest.mock import Mock
        trader=Mock()
        trader.reconcile.return_value=True
        trader.manage_exits.return_value={}
        trader.ledger.report.return_value={}
        trader.enter.return_value=False
        trader.ledger.traded_bar.return_value=False
        with patch('main.regime_at',return_value=True),patch('main.entry_at',return_value=True),patch('main.bot_log'):
            cycle(replace(Settings(),underlyings=('IWM',),enable_entries=True),broker,trader)
        trader.enter.assert_called_once()
        self.assertEqual(trader.enter.call_args.args[0].strike,300)

    def test_candidate_ranking_does_not_prefilter_capital(self):
        c=contract(300)
        broker=AlpacaBroker.__new__(AlpacaBroker)
        broker.cfg=Settings(); broker.feed='indicative'
        contracts=[NS(symbol=c.symbol,strike_price=300,tradable=True,type='put',size=100,root_symbol='IWM',open_interest=1000,expiration_date=c.expiry)]
        broker.stocks=NS(get_stock_latest_trade=lambda r:{'IWM':NS(price=315,timestamp=datetime.now(timezone.utc))})
        broker.trading=NS(get_option_contracts=lambda r:NS(option_contracts=contracts,next_page_token=None))
        snapshot=NS(greeks=NS(delta=-.25),latest_quote=NS(bid_price=3,ask_price=3.05,timestamp=datetime.now(timezone.utc)))
        broker.options=NS(get_option_snapshot=lambda r:{c.symbol:snapshot},get_option_bars=lambda r:NS(data={c.symbol:[NS(volume=200)]}))
        with patch('options_trader.bot_log'):
            ranked=broker.candidates('IWM')
        self.assertEqual(ranked[0].strike,300)

    def test_adapter_rejects_calls_and_multiple_contracts(self):
        b=AlpacaBroker.__new__(AlpacaBroker)
        with self.assertRaises(ValueError): b.submit(contract(),2,'sell',2,'bad')
        with self.assertRaises(ValueError): b.submit(replace(contract(),symbol=contract().symbol.replace('P','C')),1,'sell',2,'bad')


class BacktestResearchTests(unittest.TestCase):
    def test_capital_comparison_reports_rejected_signals(self):
        days=pd.bdate_range('2024-01-01',periods=350)
        series=pd.Series([300+5*math.sin(i/5)+i*.02 for i in range(350)],index=days)
        cfg=replace(Settings(),underlyings=('SPY',),min_credit_yield=.0001)
        _,_,small=simulate({'SPY':series},cfg,days[205].date())
        self.assertGreater(small['qualified_signals'],0)
        self.assertEqual(small['executed_trades'],0)
        self.assertGreater(small['rejected_solely_capital'],0)
        bigcfg=replace(cfg,virtual_starting_capital=50000,max_collateral_per_trade=50000,max_total_collateral=50000)
        trades,curve,big=simulate({'SPY':series},bigcfg,days[205].date())
        self.assertGreater(big['executed_trades'],0)
        self.assertAlmostEqual(big['ending_virtual_capital'],curve[-1]['equity'])
        self.assertEqual(big['qualified_signals'],big['executed_trades']+sum(big['rejected'].values()))
        self.assertEqual(small['starting_virtual_capital'],25000)

if __name__=='__main__': unittest.main()
