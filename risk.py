"""Pure collateral checks. Margin buying power is never treated as cash."""
import math
import re
from datetime import datetime
from config import correlation_group


def parse_option(symbol):
    match = re.fullmatch(r"([A-Z]+)(\d{6})([CP])(\d{8})", symbol)
    if not match:
        return None
    root, expiry, kind, strike = match.groups()
    return dict(underlying=root, expiry=datetime.strptime(expiry, "%y%m%d").date(), kind=kind, strike=int(strike) / 1000)


def capacity(cfg, account, positions, orders, candidate):
    """Account-wide reserves include external puts and unfilled sell quantities.

    Unknown external orders block entries: estimating their future cash impact is
    unsafe. The broker snapshot must be refreshed before each submission.
    """
    if account.trading_blocked or account.account_blocked or getattr(account, "trade_suspended_by_user", False):
        return False, "account_blocked"
    cash = float(account.cash)
    cash -= float(getattr(account, "pending_transfer_out", 0) or 0)
    cash -= float(getattr(account, "accrued_fees", 0) or 0)
    buying_power = float(account.options_buying_power or 0)
    if not all(math.isfinite(x) for x in (cash, buying_power)):
        return False, "invalid_cash"
    if int(account.options_trading_level or 0) < 1:
        return False, "options_permission"
    reserve = 0.0
    exposures = {}
    for position in positions:
        parsed = parse_option(position.symbol)
        qty = float(position.qty)
        if not math.isfinite(qty):
            return False, "invalid_position"
        if parsed and parsed["kind"] == "P" and qty < 0:
            reserve += parsed["strike"] * abs(qty) * 100
            exposures[position.symbol] = parsed["underlying"]
        elif qty < 0:
            return False, "other_short_exposure"
        if position.symbol == candidate.underlying or (parsed and parsed["underlying"] == candidate.underlying):
            return False, "existing_underlying_exposure"
    for order in orders:
        parsed = parse_option(order.symbol or "")
        side = getattr(order.side, "value", order.side)
        remaining = float(order.qty or 0) - float(order.filled_qty or 0)
        if remaining <= 0:
            continue
        if not str(order.client_order_id).startswith("os-"):
            return False, "external_pending_order"
        if not parsed or parsed["kind"] != "P":
            return False, "unsupported_pending_order"
        if side == "sell":
            reserve += parsed["strike"] * remaining * 100
            exposures[order.symbol] = parsed["underlying"]
        if parsed["underlying"] == candidate.underlying:
            return False, "pending_underlying_order"
    needed = candidate.strike * 100  # exactly one standard contract per entry
    if needed > cfg.max_collateral_per_trade:
        return False, "per_trade_collateral"
    if reserve + needed > cfg.max_total_collateral:
        return False, "portfolio_collateral"
    # Cash includes premium from filled shorts; reserve the full strike anyway.
    if needed > max(0, cash - reserve - cfg.cash_buffer) or needed > buying_power:
        return False, "insufficient_cash"
    if len(exposures) >= cfg.max_positions:
        return False, "max_positions"
    if sum(correlation_group(root) == correlation_group(candidate.underlying) for root in exposures.values()) >= cfg.max_per_group:
        return False, "correlation_limit"
    return True, ""
