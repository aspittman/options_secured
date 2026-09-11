import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
from analytics import Ledger
from config import Settings
from options_trader import AlpacaBroker
from test_research import contract, account


class CSPOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.ledger=Ledger(str(Path(self.temp.name)/'ledger.db'))
        self.broker=AlpacaBroker.__new__(AlpacaBroker)
        self.broker.cfg=replace(Settings(),enable_entries=True)
        self.broker.ledger=self.ledger
        self.broker.trading=Mock()
        self.c=contract()
        self.id='cash_secured_put_IWM_test'
        self.broker.trading.get_option_contract.return_value=NS(
            type='put',symbol=self.c.symbol,root_symbol='IWM',size=100,
            strike_price=self.c.strike,expiration_date=self.c.expiry)
        self.broker.trading.get_all_positions.return_value=[]
        self.broker.trading.get_orders.return_value=[]
        self.broker.trading.get_account.return_value=account()

    def tearDown(self):
        self.ledger.db.close(); self.temp.cleanup()

    def intent(self,side='sell'):
        self.ledger.intent(self.id,self.c,'sideways',side,1)

    def submit(self,side='sell',c=None):
        return self.broker.submit(c or self.c,1,side,2,self.id)

    def blocked(self,side='sell',c=None):
        with self.assertRaises(ValueError): self.submit(side,c)
        self.broker.trading.submit_order.assert_not_called()

    def test_verified_cash_secured_entry_allowed(self):
        self.intent(); self.submit()
        req=self.broker.trading.submit_order.call_args.kwargs['order_data']
        self.assertEqual(req.position_intent.value,'sell_to_open')
        self.assertEqual(self.ledger.reserved(),17500)

    def test_premium_over_500_remains_allowed(self):
        self.intent()
        self.broker.submit(self.c,1,'sell',6.25,self.id)
        request=self.broker.trading.submit_order.call_args.kwargs['order_data']
        self.assertEqual(request.limit_price,6.25)
        self.assertEqual(request.position_intent.value,'sell_to_open')

    def test_no_direct_adapter_bypass_without_owned_intent(self):
        self.blocked()

    def test_calls_stocks_and_mismatched_strikes_blocked(self):
        self.intent()
        for c in (replace(self.c,symbol='IWM'),replace(self.c,symbol=self.c.symbol.replace('P','C')),
                  replace(self.c,strike=5)):
            self.blocked(c=c)

    def test_adjusted_contract_blocked(self):
        self.intent()
        self.broker.trading.get_option_contract.return_value.size=10
        self.blocked()

    def test_insufficient_real_cash_blocks_unsecured_put(self):
        self.intent()
        self.broker.trading.get_account.return_value=account(1000)
        self.blocked()

    def test_insufficient_virtual_cash_blocks_despite_large_account(self):
        self.intent()
        self.broker.cfg=replace(self.broker.cfg,virtual_starting_capital=10000)
        self.blocked()

    def test_buy_cannot_open_long_put_or_close_other_bot_put(self):
        self.intent('buy')
        self.blocked('buy')
        self.broker.trading.get_all_positions.return_value=[NS(symbol=self.c.symbol,qty=-1)]
        self.blocked('buy')

    def own_short(self):
        self.ledger.intent('os-owned',self.c,'sideways','sell',1)
        self.ledger.reconcile('os-owned','old','filled',1,3)
        self.broker.trading.get_all_positions.return_value=[NS(symbol=self.c.symbol,qty=-1)]
        self.intent('buy')

    def test_owned_buyback_allowed_even_if_new_entries_disabled(self):
        self.own_short()
        self.broker.cfg=replace(self.broker.cfg,enable_entries=False,virtual_starting_capital=10000)
        self.submit('buy')
        self.assertEqual(self.broker.trading.submit_order.call_args.kwargs['order_data'].position_intent.value,'buy_to_close')

    def test_duplicate_buyback_and_long_position_blocked(self):
        self.own_short()
        self.broker.trading.get_orders.return_value=[NS(symbol=self.c.symbol)]
        self.blocked('buy')
        self.broker.trading.get_orders.return_value=[]
        self.broker.trading.get_all_positions.return_value=[NS(symbol=self.c.symbol,qty=1)]
        self.blocked('buy')

    def test_completed_intent_cannot_be_resubmitted(self):
        self.intent()
        self.ledger.reconcile(self.id,'existing','new',0,0)
        self.blocked()
