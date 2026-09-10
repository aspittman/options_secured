"""Independent strategy allocation plus separate account cash safeguards."""
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


def virtual_capacity(cfg, required, reserved=0, realized=0):
    # Premium is not available until realized. Profits never enlarge the allocation;
    # losses reduce it. Account equity is deliberately absent from this calculation.
    budget = max(0, min(cfg.virtual_starting_capital, cfg.virtual_starting_capital + realized))
    available = max(0, min(budget, cfg.max_total_collateral) - reserved)
    if required > min(cfg.max_collateral_per_trade, cfg.virtual_starting_capital):
        return False, "COLLATERAL_OVER_LIMIT", available
    if required > available:
        return False, "MAX_STRATEGY_EXPOSURE_REACHED", available
    return True, "", available


def capacity(cfg, account, positions, orders, candidate, *, own_lots=None, own_pending=(), realized=0):
    """Broker positions protect cash; only ledger-owned lots consume strategy slots.

    Unrelated calls/stocks/long puts are never adopted or managed. Same-contract
    exposure is blocked because Alpaca nets it and ownership cannot be separated.
    Unknown external cash debits fail closed, without cancelling the other order.
    """
    parsed = parse_option(candidate.symbol)
    if not parsed or parsed["kind"] != "P" or parsed["strike"] != candidate.strike:
        return False, "OTHER"
    own_lots = own_lots or {}
    reserved = sum(l["strike"] * l["qty"] * 100 for l in own_lots.values())
    reserved += sum(o["strike"] * (o["qty"] - o["filled"]) * 100 for o in own_pending if o["side"] == "sell")
    ok, reason, _ = virtual_capacity(cfg, candidate.strike * 100, reserved, realized)
    if not ok:
        return False, reason
    exposures = {s: l["underlying"] for s, l in own_lots.items()}
    exposures.update({o["symbol"]: o["underlying"] for o in own_pending if o["side"] == "sell"})
    if candidate.underlying in exposures.values():
        return False, "DUPLICATE_POSITION"
    if len(exposures) >= cfg.max_positions:
        return False, "MAX_STRATEGY_EXPOSURE_REACHED"
    if sum(correlation_group(s) == correlation_group(candidate.underlying) for s in exposures.values()) >= cfg.max_per_group:
        return False, "MAX_STRATEGY_EXPOSURE_REACHED"
    if account.trading_blocked or account.account_blocked or getattr(account, "trade_suspended_by_user", False):
        return False, "OTHER"
    cash = float(account.cash) - float(getattr(account, "pending_transfer_out", 0) or 0) - float(getattr(account, "accrued_fees", 0) or 0)
    buying_power = float(account.options_buying_power or 0)
    if not all(math.isfinite(x) for x in (cash, buying_power)) or int(account.options_trading_level or 0) < 1:
        return False, "OTHER"
    account_reserve = 0.0
    for position in positions:
        contract = parse_option(position.symbol)
        qty = float(position.qty)
        if not math.isfinite(qty):
            return False, "OTHER"
        if position.symbol == candidate.symbol:
            return False, "DUPLICATE_POSITION"
        if contract and contract["kind"] == "P" and qty < 0:
            account_reserve += contract["strike"] * abs(qty) * 100
    for order in orders:
        contract = parse_option(order.symbol or "")
        side = getattr(order.side, "value", order.side)
        remaining = float(order.qty or 0) - float(order.filled_qty or 0)
        if not math.isfinite(remaining):
            return False, "OTHER"
        if remaining <= 0:
            continue
        if order.symbol == candidate.symbol:
            return False, "DUPLICATE_POSITION"
        if contract and contract["kind"] == "P" and side == "sell":
            account_reserve += contract["strike"] * remaining * 100
        elif side == "buy":
            price = float(getattr(order, "limit_price", None) or 0)
            if not math.isfinite(price) or price <= 0:
                return False, "OTHER"  # Unbounded pending market debit in shared account.
            account_reserve += price * remaining * (100 if contract else 1)
    if candidate.strike * 100 > max(0, cash - account_reserve - cfg.cash_buffer) or candidate.strike * 100 > buying_power:
        return False, "INSUFFICIENT_BROKER_CASH"
    return True, ""
