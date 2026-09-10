"""Shared CSP research statistics: capital employed is full strike collateral."""

def statistics(starting, realized, unrealized, trades, entries, curve):
    pnls = [t['pnl'] for t in trades]
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p < 0]
    avg = lambda values: sum(values) / len(values) if values else None
    capital = sum(e['collateral'] for e in entries)
    premium = sum(e['premium'] for e in entries)
    outcomes = [t.get('outcome', 'buy_to_close') for t in trades]
    total = realized + unrealized if unrealized is not None else None
    peak, drawdown = starting, 0.0
    for point in curve:
        equity = point.get('equity')
        if equity is None:
            continue
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak-equity)/peak if peak > 0 else 0)
    return {
        'strategy': 'cash_secured_put', 'starting_virtual_capital': starting,
        'ending_virtual_capital': starting + total if total is not None else None,
        'realized_pnl': realized, 'unrealized_pnl': unrealized,
        'total_return_pct': 100*total/starting if total is not None else None,
        'return_on_capital_employed_pct': 100*total/capital if total is not None and capital else None,
        'average_capital_employed_per_trade': avg([e['collateral'] for e in entries]),
        'maximum_capital_employed': max((p.get('reserved_collateral',0) for p in curve), default=0),
        'trade_count': len(entries), 'completed_trades': len(trades),
        'win_rate': len(wins)/len(trades) if trades else None,
        'average_winner': avg(wins), 'average_loser': avg(losses), 'expectancy': avg(pnls),
        'profit_factor': sum(wins)/-sum(losses) if losses else None,
        'max_drawdown': drawdown if curve else None,
        'average_hold_days': avg([t['hold_days'] for t in trades if t['hold_days'] is not None]),
        'largest_winner': max(wins, default=None), 'largest_loser': min(losses, default=None),
        'premium_received': premium,
        'assignment_rate': outcomes.count('assignment')/len(outcomes) if outcomes else None,
        'expiration_rate': outcomes.count('expiration')/len(outcomes) if outcomes else None,
        'buy_to_close_rate': outcomes.count('buy_to_close')/len(outcomes) if outcomes else None,
        'average_collateral_committed': avg([e['collateral'] for e in entries]),
        'premium_return_on_collateral_pct': premium/capital*100 if capital else None,
    }
