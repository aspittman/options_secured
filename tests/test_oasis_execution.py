from zoneinfo import ZoneInfo
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock,patch
from analytics import Ledger
from config import Settings
from options_trader import Candidate,Trader


class SecuredOasisExecutionTests(unittest.TestCase):
    def test_confirmed_short_loss_blocks_regular_and_oasis_after_restart(self):
        now=datetime.now(timezone.utc);expiry=now.date()+timedelta(days=35)
        c=Candidate(f'IWM{expiry:%y%m%d}P00100000','IWM',100,expiry,1,1.05)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ledger.db';ledger=Ledger(path)
            for i,(side,price) in enumerate([('sell',1),('buy',1.2)]):
                cid=f'cash_secured_put_IWM_{i}';ledger.intent(cid,c,'oasis',side,1)
                ledger.reconcile(cid,cid,'filled',1,price)
            ledger.db.close();ledger=Ledger(path)
            self.assertTrue(ledger.loss_blocked('IWM',now.astimezone(ZoneInfo("America/New_York")).date()+timedelta(days=30)))
            self.assertFalse(ledger.loss_blocked('IWM',now.astimezone(ZoneInfo("America/New_York")).date()+timedelta(days=31)))
            broker=MagicMock();trader=Trader(Settings(enable_entries=True),broker,ledger)
            for variant in ('regular','oasis'):
                self.assertFalse(trader.submit(c,variant,'sell',1,1))
            broker.submit.assert_not_called();ledger.db.close()

    def test_manage_uses_oasis_twenty_percent_stop(self):
        now=datetime.now(timezone.utc);expiry=now.date()+timedelta(days=35);symbol=f'IWM{expiry:%y%m%d}P00100000'
        ledger=MagicMock();ledger.pending.return_value=[];ledger.assigned.return_value=[]
        ledger.lots.return_value={symbol:dict(strategy='oasis',underlying='IWM',qty=1,credit=1,strike=100,expiry=expiry.isoformat())}
        ledger.latest_entry_timestamp.return_value=now.isoformat()
        broker=MagicMock();broker.positions.return_value=[NS(symbol=symbol,qty=-1)];broker.orders.return_value=[]
        broker.snapshot.return_value=NS(latest_quote=NS(bid_price=1.19,ask_price=1.2,timestamp=now))
        broker.trading.get_clock.return_value=NS(is_open=True,timestamp=now,next_close=now+timedelta(hours=2))
        trader=Trader(Settings(),broker,ledger)
        with patch.object(trader,'submit') as submit,patch('options_trader.bearish_at',return_value=False):
            trader.manage_exits()
            self.assertEqual(submit.call_args.args[1:3],('oasis','buy'))
            self.assertEqual(ledger.event.call_args.kwargs['reason'],'oasis_20_percent_credit_stop')
