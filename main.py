"""Five-minute paper loop, daily entries, continuously monitored exits."""
import argparse
import fcntl
import json
from pathlib import Path
import time

from analytics import Ledger
from bot_logger import bot_log, setup_logging
from config import Settings
from options_trader import AlpacaBroker, Trader
from strategy import STRATEGIES, entry_at, regime_at


def cycle(cfg, broker, trader):
    healthy = trader.reconcile()
    clock = broker.trading.get_clock()
    if not clock.is_open:
        bot_log("Market closed; orders reconciled.")
        return
    marks = trader.manage_exits()
    bot_log(json.dumps(trader.ledger.report(marks)))
    trader.ledger.export()
    if not cfg.enable_entries or not healthy:
        return
    market = broker.history("SPY")
    if not regime_at(market, -1, cfg):
        bot_log("Market outside sideways-to-mildly-bullish regime; no entries.")
        return
    for underlying in cfg.underlyings:
        try:
            frame = broker.history(underlying)
            if frame.index[-1].date() != market.index[-1].date():
                continue
            signal_date = frame.index[-1].date().isoformat()
            if trader.ledger.traded_bar(underlying, signal_date):
                continue
            eligible = [s for s in STRATEGIES if entry_at(frame, -1, s, cfg)]
            if not eligible:
                continue
            for candidate in broker.candidates(underlying):
                if trader.enter(candidate, eligible[0], signal_date):
                    break
        except Exception as exc:
            bot_log(f"Entry scan unavailable for {underlying}: {exc}")


def run_bot(once=False):
    cfg = Settings.from_env()
    setup_logging()
    lock_path = Path(cfg.db_path).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another bot is already using this ledger") from None
        ledger = Ledger(cfg.db_path)
        broker = AlpacaBroker(cfg)
        trader = Trader(cfg, broker, ledger)
        bot_log(f"Options Secured PAPER started; new entries={cfg.enable_entries}")
        while True:
            try:
                cycle(cfg, broker, trader)
            except Exception as exc:
                bot_log(f"Cycle unavailable: {exc}")
                if once:
                    raise
            if once:
                return
            time.sleep(cfg.scan_seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one paper cycle")
    run_bot(parser.parse_args().once)
