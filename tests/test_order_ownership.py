import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
from analytics import Ledger
from options_trader import AlpacaBroker
from test_research import contract


class OrderOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.ledger=Ledger(str(Path(self.temp.name)/'ledger.db'))
        self.broker=AlpacaBroker.__new__(AlpacaBroker)
        self.broker.ledger=self.ledger
        self.broker.trading=Mock()
        self.c=contract()

    def tearDown(self):
        self.ledger.db.close()
        self.temp.cleanup()

    def record(self, client_id):
        self.ledger.intent(client_id,self.c,'sideways','sell',1)
        self.ledger.reconcile(client_id,'broker-id','new',0,0)
        self.broker.trading.get_order_by_id.return_value=NS(id='broker-id',client_order_id=client_id,symbol=self.c.symbol,side='sell')

    def test_owned_strategy_order_can_be_cancelled(self):
        self.record('cash_secured_put_IWM_123')
        self.broker.cancel('broker-id')
        self.broker.trading.cancel_order_by_id.assert_called_once_with('broker-id')

    def test_untracked_order_never_cancelled_even_with_matching_prefix(self):
        with self.assertRaises(ValueError): self.broker.cancel('unknown')
        self.broker.trading.cancel_order_by_id.assert_not_called()

    def test_foreign_strategy_in_ledger_cannot_be_cancelled(self):
        self.record('long_put_IWM_123')
        with self.assertRaises(ValueError): self.broker.cancel('broker-id')
        self.broker.trading.cancel_order_by_id.assert_not_called()

    def test_changed_broker_identity_cannot_be_cancelled(self):
        self.record('cash_secured_put_IWM_123')
        self.broker.trading.get_order_by_id.return_value.client_order_id='covered_call_IWM_123'
        with self.assertRaises(ValueError): self.broker.cancel('broker-id')
        self.broker.trading.cancel_order_by_id.assert_not_called()

    def test_documented_legacy_order_remains_manageable(self):
        self.record('os-legacy123')
        self.broker.cancel('broker-id')
        self.broker.trading.cancel_order_by_id.assert_called_once()

    def test_new_orders_require_strategy_prefix(self):
        for client_id in ('os-new','long_put_IWM_123','cash_secured_put_SPY_123'):
            with self.assertRaises(ValueError): self.broker.submit(self.c,1,'sell',2,client_id)
        self.broker.trading.submit_order.assert_not_called()
