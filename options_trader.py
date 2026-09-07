"""Alpaca adapter and paper execution lifecycle for cash-secured puts."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import math
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import OptionBarsRequest, OptionSnapshotRequest, StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, ContractType, OrderSide, PositionIntent, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOptionContractsRequest, GetOrdersRequest, LimitOrderRequest

from analytics import TERMINAL
from bot_logger import bot_log
from config import credentials
from risk import capacity
from strategy import bearish_at, exit_reason, indicators

NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Candidate:
    symbol: str
    underlying: str
    strike: float
    expiry: date
    bid: float
    ask: float
    delta: float = -0.25


def valid_quote(quote, max_age, now=None, for_exit=False):
    if quote is None or quote.timestamp is None:
        return False
    now = now or datetime.now(timezone.utc)
    stamp = quote.timestamp
    if stamp.tzinfo is None:
        return False
    bid, ask = float(quote.bid_price), float(quote.ask_price)
    return (math.isfinite(bid) and math.isfinite(ask) and 0 <= bid <= ask and ask > 0
            and (for_exit or bid > 0)
            and -5 <= (now - stamp).total_seconds() <= max_age)


def limit_price(value):
    # Rounding up is conservative for sell credit and marketable buyback limits.
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_CEILING))


class AlpacaBroker:
    def __init__(self, cfg):
        key, secret = credentials()
        self.cfg = cfg
        self.trading = TradingClient(key, secret, paper=True)
        self.stocks = StockHistoricalDataClient(key, secret)
        self.options = OptionHistoricalDataClient(key, secret)
        self.feed = OptionsFeed(cfg.option_feed)
        self.history_cache = {}

    def account(self):
        return self.trading.get_account()

    def positions(self):
        return self.trading.get_all_positions()

    def orders(self):
        orders = self.trading.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        if len(orders) >= 500:
            raise RuntimeError("Open order snapshot may be truncated; entries blocked")
        return orders

    def lookup(self, client_id):
        return self.trading.get_order_by_client_id(client_id)

    def cancel(self, broker_id):
        self.trading.cancel_order_by_id(broker_id)

    def submit(self, candidate, qty, side, price, client_id):
        return self.trading.submit_order(order_data=LimitOrderRequest(
            symbol=candidate.symbol, qty=qty, side=OrderSide(side),
            position_intent=PositionIntent.SELL_TO_OPEN if side == "sell" else PositionIntent.BUY_TO_CLOSE,
            time_in_force=TimeInForce.DAY, limit_price=price, client_order_id=client_id))

    def snapshot(self, symbol):
        return self.options.get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=[symbol], feed=self.feed)).get(symbol)

    def history(self, symbol):
        now = datetime.now(timezone.utc)
        cached = self.history_cache.get(symbol)
        if cached and (now - cached[0]).total_seconds() < 240:
            return cached[1]
        bars = self.stocks.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=now - timedelta(days=500), end=now, feed=DataFeed.IEX)).data.get(symbol, [])
        today = now.astimezone(NY).date()
        bars = [bar for bar in bars if bar.timestamp.astimezone(NY).date() < today]
        if not bars or (today - bars[-1].timestamp.astimezone(NY).date()).days > 4:
            raise RuntimeError(f"Missing or stale completed daily bars for {symbol}")
        close = pd.Series([float(bar.close) for bar in bars], index=pd.DatetimeIndex([bar.timestamp for bar in bars]))
        frame = indicators(close.sort_index())
        if len(frame) < 205:
            raise RuntimeError(f"Insufficient daily history for {symbol}")
        self.history_cache[symbol] = (now, frame)
        return frame

    def earnings_clear(self, underlying, expiry):
        if underlying in {"SPY", "QQQ", "IWM", "DIA"}:
            return True
        # Unknown calendars fail closed; cover the whole potential holding period.
        import yfinance as yf
        try:
            calendar = yf.Ticker(underlying).calendar
            dates = calendar.get("Earnings Date", []) if isinstance(calendar, dict) else []
            dates = [d.date() if isinstance(d, datetime) else d for d in dates]
            future = [d for d in dates if d >= datetime.now(NY).date()]
            return bool(future) and min(future) > expiry + timedelta(days=1)
        except Exception:
            return False

    def candidates(self, underlying):
        cfg = self.cfg
        today = datetime.now(NY).date()
        latest = self.stocks.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=underlying, feed=DataFeed.IEX))[underlying]
        if not -5 <= (datetime.now(timezone.utc) - latest.timestamp).total_seconds() <= cfg.quote_max_age:
            return []
        spot = float(latest.price)
        if not math.isfinite(spot) or spot <= 0:
            return []
        request = GetOptionContractsRequest(
            underlying_symbols=[underlying], status=AssetStatus.ACTIVE, type=ContractType.PUT,
            expiration_date_gte=today + timedelta(days=cfg.min_dte),
            expiration_date_lte=today + timedelta(days=cfg.max_dte),
            strike_price_gte=f"{spot * .80:.2f}", strike_price_lte=f"{spot:.2f}", limit=1000)
        contracts = []
        while True:
            response = self.trading.get_option_contracts(request)
            contracts.extend(response.option_contracts or [])
            if not response.next_page_token:
                break
            request.page_token = response.next_page_token
        eligible = []
        for contract in contracts:
            strike = float(contract.strike_price)
            if (contract.tradable and str(getattr(contract.type, "value", contract.type)) == "put"
                and float(contract.size or 0) == 100 and contract.root_symbol == underlying
                and 0 < strike < spot and strike * 100 <= cfg.max_collateral_per_trade
                and float(contract.open_interest or 0) >= cfg.min_open_interest
                and cfg.min_dte <= (contract.expiration_date - today).days <= cfg.max_dte):
                eligible.append(contract)
        ranked = []
        for start in range(0, len(eligible), 100):
            batch = eligible[start:start + 100]
            symbols = [c.symbol for c in batch]
            snapshots = self.options.get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=symbols, feed=self.feed))
            volumes = self.options.get_option_bars(OptionBarsRequest(
                symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
                start=datetime.combine(today, datetime.min.time(), NY), feed=self.feed)).data
            for contract in batch:
                snap = snapshots.get(contract.symbol)
                quote = getattr(snap, "latest_quote", None)
                delta = getattr(getattr(snap, "greeks", None), "delta", None)
                volume = sum(float(b.volume) for b in volumes.get(contract.symbol, []))
                if not valid_quote(quote, cfg.quote_max_age) or delta is None:
                    continue
                delta = float(delta)
                bid, ask = float(quote.bid_price), float(quote.ask_price)
                strike = float(contract.strike_price)
                if (not math.isfinite(delta) or delta >= 0 or abs(delta - cfg.target_delta) > cfg.delta_tolerance
                    or (ask - bid) / ((ask + bid) / 2) > cfg.max_spread
                    or volume < cfg.min_volume or bid / strike < cfg.min_credit_yield):
                    continue
                ranked.append(Candidate(contract.symbol, underlying, strike, contract.expiration_date, bid, ask, delta))
        return sorted(ranked, key=lambda c: (abs(c.delta - cfg.target_delta), (c.ask - c.bid) / c.ask, c.strike))


class Trader:
    def __init__(self, cfg, broker, ledger):
        self.cfg, self.broker, self.ledger = cfg, broker, ledger

    def reconcile(self):
        healthy = True
        for row in self.ledger.pending():
            try:
                order = self.broker.lookup(row["client_id"])
                status = str(getattr(order.status, "value", order.status))
                self.ledger.reconcile(row["client_id"], str(order.id), status,
                                      float(order.filled_qty or 0), float(order.filled_avg_price or 0))
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["created"])).total_seconds()
                timeout = self.cfg.entry_timeout if row["side"] == "sell" else self.cfg.exit_timeout
                if status not in TERMINAL and age >= timeout:
                    self.broker.cancel(str(order.id))
                    # Keep reserves and pending status until cancellation is confirmed.
            except Exception as exc:
                healthy = False
                bot_log(f"Order reconciliation pending for {row['client_id']}: {exc}")
        positions = {p.symbol: p for p in self.broker.positions()}
        pending_symbols = {r["symbol"] for r in self.ledger.pending()}
        for symbol, lot in self.ledger.lots().items():
            actual = float(positions[symbol].qty) if symbol in positions else 0
            if actual != -lot["qty"] and symbol not in pending_symbols:
                healthy = False
                if not self.ledger.blocked():
                    self.ledger.event("RECONCILIATION_REQUIRED", symbol, expected=-lot["qty"], actual=actual,
                                      reason="Possible assignment, expiration, or external trade; preserve basis and reserve")
                bot_log(f"RECONCILIATION REQUIRED: {symbol}, ledger={-lot['qty']}, broker={actual}")
        return healthy and not self.ledger.blocked()

    def submit(self, candidate, strategy, side, qty, price, signal_date=""):
        client_id = f"os-{uuid4().hex}"
        self.ledger.intent(client_id, candidate, strategy, side, qty, signal_date)
        try:
            order = self.broker.submit(candidate, qty, side, limit_price(price), client_id)
            status = str(getattr(order.status, "value", order.status))
            self.ledger.reconcile(client_id, str(order.id), status,
                                  float(order.filled_qty or 0), float(order.filled_avg_price or 0))
            self.ledger.event("ORDER_SUBMITTED", candidate.symbol, side=side, qty=qty, client_id=client_id)
            return True
        except Exception as exc:
            # A timeout can occur after acceptance. Never retry with a new ID.
            self.ledger.event("SUBMISSION_UNCERTAIN", candidate.symbol, client_id=client_id, error=str(exc))
            bot_log(f"Submission requires reconciliation: {client_id}: {exc}")
            return False

    def enter(self, candidate, strategy, signal_date):
        if not self.cfg.enable_entries or not self.reconcile():
            return False
        pending = self.ledger.pending()
        # Await every entry before another; prevents broker snapshot races.
        if any(r["side"] == "sell" or r["status"] == "intent" for r in pending):
            return False
        if self.ledger.traded_bar(candidate.underlying, signal_date):
            return False
        last_exit = self.ledger.last_exit(candidate.underlying)
        if last_exit and (datetime.now(NY).date() - last_exit).days < self.cfg.cooldown_days:
            return False
        if not self.broker.earnings_clear(candidate.underlying, candidate.expiry):
            return False
        allowed, reason = capacity(self.cfg, self.broker.account(), self.broker.positions(), self.broker.orders(), candidate)
        if not allowed:
            self.ledger.event("SKIP", candidate.symbol, reason=reason)
            return False
        snap = self.broker.snapshot(candidate.symbol)
        quote = getattr(snap, "latest_quote", None)
        if not valid_quote(quote, self.cfg.quote_max_age):
            return False
        bid, ask = float(quote.bid_price), float(quote.ask_price)
        if (ask - bid) / ((ask + bid) / 2) > self.cfg.max_spread or bid / candidate.strike < self.cfg.min_credit_yield:
            return False
        return self.submit(candidate, strategy, "sell", 1, (bid + ask) / 2, signal_date)

    def manage_exits(self):
        positions = {p.symbol: p for p in self.broker.positions()}
        open_orders = self.broker.orders()
        pending = {r["symbol"] for r in self.ledger.pending()} | {o.symbol for o in open_orders}
        marks = {}
        for symbol, lot in self.ledger.lots().items():
            try:
                if symbol in pending or symbol not in positions or float(positions[symbol].qty) != -lot["qty"]:
                    continue
                snap = self.broker.snapshot(symbol)
                quote = getattr(snap, "latest_quote", None)
                if not valid_quote(quote, self.cfg.quote_max_age, for_exit=True):
                    bot_log(f"Exit quote unavailable/stale: {symbol}")
                    continue
                ask = float(quote.ask_price)
                marks[symbol] = ask
                expiry = date.fromisoformat(lot["expiry"])
                dte = (expiry - datetime.now(NY).date()).days
                bearish = False
                try:
                    bearish = bearish_at(self.broker.history(lot["underlying"]), -1)
                except Exception as exc:
                    bot_log(f"Technical exit unavailable for {symbol}: {exc}")
                reason = exit_reason(lot["credit"], ask, dte, bearish, self.cfg)
                if reason:
                    candidate = Candidate(symbol, lot["underlying"], lot["strike"], expiry, float(quote.bid_price), ask)
                    self.ledger.event("EXIT_SIGNAL", symbol, reason=reason)
                    self.submit(candidate, lot["strategy"], "buy", int(lot["qty"]), ask)
            except Exception as exc:
                bot_log(f"Exit monitoring failed for {symbol}: {exc}")
        return marks
