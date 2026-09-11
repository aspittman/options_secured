import fcntl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from config import Settings
from main import run_bot


class ForegroundRuntimeTests(unittest.TestCase):
    def test_ctrl_c_closes_ledger_and_releases_lock(self):
        for interrupt_during_cycle in (True,False):
            with self.subTest(interrupt_during_cycle=interrupt_during_cycle), tempfile.TemporaryDirectory() as directory:
                cfg=Settings(db_path=str(Path(directory)/'ledger.db'))
                ledger=Mock()
                with patch('main.Settings.from_env',return_value=cfg), patch('main.setup_logging'), patch('main.bot_log') as log, patch('main.Ledger',return_value=ledger), patch('main.AlpacaBroker'), patch('main.Trader'), patch('main.cycle',side_effect=KeyboardInterrupt if interrupt_during_cycle else None), patch('main.time.sleep',side_effect=KeyboardInterrupt):
                    run_bot()
                    ledger.db.close.assert_called_once()
                    self.assertIn('stopped by Ctrl+C',log.call_args.args[0])
                with Path(cfg.db_path).with_suffix('.lock').open('a') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)

    def test_duplicate_foreground_launch_does_not_create_broker(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=Settings(db_path=str(Path(directory)/'ledger.db'))
            with Path(cfg.db_path).with_suffix('.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with patch('main.Settings.from_env',return_value=cfg),patch('main.setup_logging'),patch('main.bot_log'),patch('main.AlpacaBroker') as broker:
                    run_bot()
                    broker.assert_not_called()
