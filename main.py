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
from strategy import entry_at, regime_at
from oasis import refresh_oasis_data, get_oasis_signal_state, entry_window


def cycle(cfg, broker, trader):
    healthy = trader.reconcile()
    clock = broker.trading.get_clock()
    if not clock.is_open:
        bot_log("Market closed; orders reconciled.")
        return
    cancellations_ok = trader.cancel_blocked_entries(clock)
    if hasattr(broker, 'stocks') and hasattr(broker, 'trading'):
        refresh_oasis_data(cfg.underlyings, broker.stocks, broker.trading, clock.timestamp)
    marks = trader.manage_exits()
    trader.ledger.record_equity(marks,cfg)
    report=trader.ledger.report(marks,cfg.virtual_starting_capital)
    bot_log(json.dumps(report))
    Path(cfg.db_path).with_name('performance_summary.json').write_text(json.dumps(report,indent=2))
    trader.ledger.export()
    if not cfg.enable_entries or not healthy or not cancellations_ok:
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
            if entry_at(frame, -1, 'regular', cfg) and not trader.ledger.traded_bar(underlying, signal_date):
                eligible.append(('regular', signal_date))
            state = get_oasis_signal_state(underlying)
            if entry_window(broker.trading.get_clock()) and state['new_signal']:
                eligible.append(('oasis', state['signal_date']))
            if not eligible:
                continue
            variant, signal_date = eligible[0]
            broker.rejection_sink=lambda reason,candidate=None,**details: trader.ledger.reject(
                reason,candidate,cfg,underlying=underlying,variant=variant,signal_date=signal_date,**details)
            candidates=broker.candidates(underlying)
            if candidates:
                trader.enter(candidates[0],variant,signal_date)
            else:
                trader.ledger.reject('NO_VALID_CONTRACT',cfg=cfg,underlying=underlying,
                                    variant=variant,signal_date=signal_date)
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
                "Stop the existing instance in its terminal before launching another."
            )
            return
        ledger = Ledger(cfg.db_path)
        try:
            broker = AlpacaBroker(cfg)
            trader = Trader(cfg, broker, ledger)
            bot_log(f"Options Secured PAPER started in this terminal (Ctrl+C to stop); new entries={cfg.enable_entries}; "
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
                time.sleep(min(cfg.scan_seconds, 60))
        except KeyboardInterrupt:
            bot_log("Options Secured stopped by Ctrl+C. Trade history preserved.")
        finally:
            ledger.db.close()



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one paper cycle")
    run_bot(parser.parse_args().once)
