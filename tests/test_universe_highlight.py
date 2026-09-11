import logging
import tempfile
import unittest
from datetime import date,timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from analytics import Ledger
from bot_logger import TerminalFormatter, log_option_fill
from config import Settings,correlation_group
from options_trader import AlpacaBroker
from universe import DEFAULT_UNDERLYINGS,ETF_SYMBOLS,ADDITIONAL_STOCKS,backtest_universe
from test_research import contract


class UniverseTests(unittest.TestCase):
    def test_originals_retained_and_all_new_symbols_classified(self):
        self.assertEqual(len(DEFAULT_UNDERLYINGS),59)
        self.assertEqual(len(set(DEFAULT_UNDERLYINGS)),59)
        self.assertEqual(DEFAULT_UNDERLYINGS[:4],('SPY','QQQ','IWM','DIA'))
        for symbol in DEFAULT_UNDERLYINGS:
            self.assertNotEqual(correlation_group(symbol),symbol)
        self.assertFalse(set(ADDITIONAL_STOCKS)&ETF_SYMBOLS)

    def test_full_requested_covered_call_universe_is_present(self):
        requested_etfs=set('SPY QQQ IWM DIA XLF XLE XLV XLI XLP XLU XLB KRE XBI ARKK EEM EFA VNQ GDX GLD SLV TLT HYG LQD USO XOP TAN IGV'.split())
        requested_stocks=set('AAPL AMD INTC BAC F T PFE CSCO MU SOFI SNAP NCLH UBER PINS RIVN HOOD ROKU PYPL WMT KO GM XOM CVX OXY'.split())
        self.assertTrue(requested_etfs <= ETF_SYMBOLS)
        self.assertTrue(requested_stocks <= set(ADDITIONAL_STOCKS))
        self.assertFalse(requested_stocks & ETF_SYMBOLS)
        self.assertEqual(len(ETF_SYMBOLS),29)
        self.assertEqual(len(ADDITIONAL_STOCKS),30)
        self.assertTrue(set('XLRE EWZ C WFC VZ HPQ KR DIS'.split()) <= set(DEFAULT_UNDERLYINGS))

    def test_related_sector_etfs_share_stock_limits(self):
        for a,b in [('XLF','BAC'),('KRE','WFC'),('XLE','XOM'),('XLV','PFE'),('XBI','XLV'),('XLP','KO'),('T','VZ'),('EEM','EWZ'),('VNQ','XLRE'),('GDX','GLD'),('SLV','GLD'),('TLT','HYG'),('LQD','HYG'),('USO','XOP'),('TAN','XLE'),('IGV','AMD'),('ARKK','ROKU'),('HOOD','SOFI'),('PYPL','BAC')]:
            self.assertEqual(correlation_group(a),correlation_group(b))

    def test_custom_universe_override_still_supported(self):
        with patch.dict('os.environ',{'UNDERLYINGS':'XLF,BAC'}):
            self.assertEqual(Settings.from_env().underlyings,('XLF','BAC'))

    def test_etfs_do_not_request_corporate_earnings(self):
        broker=AlpacaBroker.__new__(AlpacaBroker)
        ticker=Mock(side_effect=AssertionError('ETF must not request earnings'))
        with patch.dict('sys.modules',{'yfinance':NS(Ticker=ticker)}):
            for symbol in ETF_SYMBOLS:
                self.assertTrue(broker.earnings_clear(symbol,date.today()+timedelta(days=35)))
        ticker.assert_not_called()

    def test_stocks_still_block_unknown_and_near_earnings(self):
        broker=AlpacaBroker.__new__(AlpacaBroker)
        expiry=date.today()+timedelta(days=35)
        for calendar in ({},{'Earnings Date':[date.today()+timedelta(days=20)]}):
            with patch.dict('sys.modules',{'yfinance':NS(Ticker=lambda s:NS(calendar=calendar))}):
                self.assertFalse(broker.earnings_clear('BAC',expiry))
        with patch.dict('sys.modules',{'yfinance':NS(Ticker=lambda s:NS(calendar={'Earnings Date':[expiry+timedelta(days=10)]}))}):
            self.assertTrue(broker.earnings_clear('BAC',expiry))

    def test_backtest_excludes_stocks_explicitly_but_includes_new_etfs(self):
        included,excluded=backtest_universe(DEFAULT_UNDERLYINGS)
        self.assertEqual(set(included),ETF_SYMBOLS)
        self.assertEqual(set(excluded),set(ADDITIONAL_STOCKS))
        self.assertIn('XLF',included)


class PremiumHighlightTests(unittest.TestCase):
    def record(self,price,qty=1,side='sell'):
        with self.assertLogs('cash_secured_put',level='INFO') as logs:
            log_option_fill('EXAMPLE',side,qty,price,100,'cash_secured_put_TEST')
        return logs.records[0]

    def test_inclusive_total_premium_threshold(self):
        self.assertTrue(self.record(5).premium_highlight)
        self.assertFalse(self.record(5.0001).premium_highlight)
        self.assertFalse(self.record(5.01).premium_highlight)
        self.assertFalse(self.record(3,qty=2).premium_highlight)
        self.assertTrue(self.record(2.5,qty=2).premium_highlight)

    def test_labels_distinguish_credit_debit_and_collateral(self):
        sell=self.record(3.4).getMessage()
        buy=self.record(1,side='buy').getMessage()
        self.assertIn('[PREMIUM <= $500]',sell)
        self.assertIn('credit_received=$340.00',sell)
        self.assertIn('strike_collateral=$10000.00',sell)
        self.assertIn('BUY_TO_CLOSE',buy)
        self.assertIn('debit_paid=$100.00',buy)

    def test_color_only_changes_terminal_copy(self):
        record=self.record(5)
        colored=TerminalFormatter('%(message)s',color=True).format(record)
        plain=TerminalFormatter('%(message)s',color=False).format(record)
        self.assertIn('\033[1;36m',colored)
        self.assertNotIn('\033',plain)
        self.assertNotIn('\033',record.getMessage())
        self.assertNotIn('\033',TerminalFormatter('%(message)s',color=True).format(self.record(6)))

    def test_only_new_committed_fills_are_highlighted(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=Ledger(str(Path(directory)/'ledger.db'))
            c=contract()
            with patch('analytics.log_option_fill') as emit:
                ledger.intent('cash_secured_put_IWM_test',c,'sideways','sell',1)
                ledger.reconcile('cash_secured_put_IWM_test','b','new',0,0)
                emit.assert_not_called()
                ledger.reconcile('cash_secured_put_IWM_test','b','filled',1,5)
                ledger.reconcile('cash_secured_put_IWM_test','b','filled',1,5)
                emit.assert_called_once()
                self.assertEqual(ledger.lots()[c.symbol]['credit'],5)
            ledger.db.close()
