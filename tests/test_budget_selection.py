from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import unittest
from config import Settings
from options_trader import AlpacaBroker

class BudgetSelectionTests(unittest.TestCase):
    def setUp(self):
        self.broker=AlpacaBroker.__new__(AlpacaBroker)
        self.broker.cfg=Settings();self.broker.feed='indicative'
        expiry=datetime.now(timezone.utc).date()+timedelta(days=35)
        self.contracts=[NS(symbol=f'ABC{expiry:%y%m%d}P{strike*1000:08d}',strike_price=strike,
            expiration_date=expiry,tradable=True,type='put',size=100,root_symbol='ABC',open_interest=1000)
            for strike in (300,249)]
        self.broker.stocks=NS(get_stock_latest_trade=lambda r:{'ABC':NS(price=310,timestamp=datetime.now(timezone.utc))})
        self.broker.trading=NS(get_option_contracts=lambda r:NS(option_contracts=self.contracts,next_page_token=None))
        self.volumes={c.symbol:[NS(volume=50)] for c in self.contracts}
        self.snapshots={c.symbol:NS(greeks=NS(delta=-.25),latest_quote=NS(bid_price=3,ask_price=3.05,
            timestamp=datetime.now(timezone.utc))) for c in self.contracts}
        self.get_snapshots=Mock(side_effect=lambda r:self.snapshots)
        self.broker.options=NS(get_option_snapshot=self.get_snapshots,
            get_option_bars=lambda r:NS(data=self.volumes))
        self.broker.rejection_sink=Mock()

    def test_only_affordable_contract_reaches_ranking(self):
        self.assertEqual(self.broker.cfg.min_volume,50)
        with patch('options_trader.bot_log'):
            rows=self.broker.candidates('ABC')
        self.assertEqual([c.strike for c in rows],[249])
        request=self.get_snapshots.call_args.args[0]
        self.assertEqual(request.symbol_or_symbols,[self.contracts[1].symbol])

    def test_reserved_capital_limits_scan(self):
        self.assertEqual(self.broker.candidates('ABC',max_collateral=24000),[])
        self.get_snapshots.assert_not_called()

    def test_missing_volume_retry_and_recorded_zero(self):
        self.volumes.clear()
        self.assertEqual(self.broker.candidates('ABC'),[])
        self.assertTrue(any('volume_data_unavailable' in c.kwargs.get('details','') for c in self.broker.rejection_sink.call_args_list))
        symbol=self.contracts[1].symbol
        self.volumes[symbol]=[NS(volume=0)]
        self.broker.rejection_sink.reset_mock()
        self.assertEqual(self.broker.candidates('ABC'),[])
        self.assertTrue(any(c.args[0]=='INSUFFICIENT_LIQUIDITY' for c in self.broker.rejection_sink.call_args_list))
        self.volumes[symbol]=[NS(volume=50)]
        self.assertEqual(len(self.broker.candidates('ABC')),1)

    def test_missing_interest_fail_closed(self):
        self.contracts[1].open_interest=None
        self.assertEqual(self.broker.candidates('ABC'),[])
        self.assertTrue(any('open_interest_data_unavailable' in c.kwargs.get('details','') for c in self.broker.rejection_sink.call_args_list))
