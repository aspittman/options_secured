"""Publish fresh cycle marks and all recorded realized results."""
from bot_logger import bot_log
from pathlib import Path
from cycle_performance import publish


def report_cycle(bot):
    def calculate():
        report = bot.ledger.report(getattr(bot, 'cycle_marks', {}), bot.cfg.virtual_starting_capital)['metrics']
        first = bot.ledger.db.execute('SELECT MIN(timestamp) FROM fills').fetchone()[0]
        return dict(starting_capital=bot.cfg.virtual_starting_capital,
                    realized_pnl=report['realized_pnl'], unrealized_pnl=report['unrealized_pnl'],
                    inception=first, status='ok' if report['unrealized_pnl'] is not None else 'incomplete: missing marks or reconciliation required')
    publish(Path(__file__).resolve().parent, 'options_secured', calculate, bot_log, bot.cfg.paper)
