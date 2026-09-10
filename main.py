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
    trader.ledger.record_equity(marks,cfg)
    report=trader.ledger.report(marks,cfg.virtual_starting_capital)
    bot_log(json.dumps(report))
    Path(cfg.db_path).with_name('performance_summary.json').write_text(json.dumps(report,indent=2))
    trader.ledger.export()
    if not cfg.enable_entries or not healthy:
        return
    market = broker.history("SPY")
    if not regime_at(market, -1, cfg):
        bot_log("Market outside sideways-to-mildly-bullish regime; no entries.")
        return
    for underlying in cfg.underlyings:
        eligible=[]
        try:
            frame = broker.history(underlying)
            if frame.index[-1].date() != market.index[-1].date():
                continue
            signal_date = frame.index[-1].date().isoformat()
            eligible = [s for s in STRATEGIES if entry_at(frame, -1, s, cfg)]
            if not eligible:
                bot_log(f"ENTRY SKIP {underlying}: no eligible daily strategy on {signal_date}")
                continue
            broker.rejection_sink=lambda reason,candidate=None,**details: trader.ledger.reject(
                reason,candidate,cfg,underlying=underlying,variant=eligible[0],signal_date=signal_date,**details)
            candidates=broker.candidates(underlying)
            if candidates:
                trader.enter(candidates[0],eligible[0],signal_date)
            else:
                trader.ledger.reject('NO_VALID_CONTRACT',cfg=cfg,underlying=underlying,
                                    variant=eligible[0],signal_date=signal_date)
        except Exception as exc:
            bot_log(f"Entry scan unavailable for {underlying}: {exc}")
            if eligible:
                trader.ledger.reject('OTHER',cfg=cfg,underlying=underlying,details=str(exc))
    trader.ledger.export()


def run_bot(once=False):
    cfg = Settings.from_env()
    setup_logging()
    lock_path = Path(cfg.db_path).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            bot_log(
                "Options Secured is already running and holds this ledger's lock. "
                "No second instance was started. "
                "View activity with: tail -f logs/options_bot.log"
            )
            return
        ledger = Ledger(cfg.db_path)
        broker = AlpacaBroker(cfg)
        trader = Trader(cfg, broker, ledger)
        bot_log(f"Options Secured PAPER started; new entries={cfg.enable_entries}; "
                f"strategy=cash_secured_put; virtual_capital=${cfg.virtual_starting_capital:,.0f}; "
                f"collateral_per_trade=${cfg.max_collateral_per_trade:,.0f}; "
                f"total_collateral=${cfg.max_total_collateral:,.0f}; "
                f"max_per_group={cfg.max_per_group}; universe={','.join(cfg.underlyings)}")
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
