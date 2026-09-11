"""Alpaca adapter and paper execution lifecycle for cash-secured puts."""
from dataclasses import dataclass, replace
from collections import Counter
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
from config import credentials, STRATEGY_ID
from risk import capacity, parse_option, virtual_capacity
from strategy import bearish_at, exit_reason, indicators

NY = ZoneInfo("America/New_York")


def verify_owned_order(row, order):
    """Require both our durable ledger record and matching broker identity.

    Pre-identifier os- orders are permitted only when already in our ledger.
    A strategy-looking prefix on an untracked broker order is never sufficient.
    """
    if row is None:
        raise ValueError("Order is not recorded in this bot's ledger")
    client_id = row['client_id']
    permitted = client_id.startswith(f'{STRATEGY_ID}_{row["underlying"]}_') or client_id.startswith('os-')
    parsed = parse_option(row['symbol'])
    if (not permitted or not parsed or parsed['kind'] != 'P'
        or parsed['underlying'] != row['underlying']
        or getattr(order,'symbol',None) != row['symbol']
        or getattr(order,'client_order_id',None) != client_id
        or str(getattr(order.side,'value',order.side)) != row['side']
        or (row['broker_id'] and str(order.id) != row['broker_id'])):
        raise ValueError("Broker order strategy/ownership mismatch; operation blocked")


@dataclass(frozen=True)
class Candidate:
    symbol: str
    underlying: str
    strike: float
    expiry: date
    bid: float
    ask: float
    delta: float = -0.25
    underlying_price: float | None = None


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
        ledger = getattr(self, 'ledger', None)
        row = ledger.db.execute('SELECT * FROM orders WHERE broker_id=?', (str(broker_id),)).fetchone() if ledger else None
        if row is None:
            raise ValueError("Cannot cancel an order outside this bot's ledger")
        # Re-read immediately before mutation, rather than trusting a caller's ID.
        order = self.trading.get_order_by_id(broker_id)
        verify_owned_order(row, order)
        self.trading.cancel_order_by_id(broker_id)

    def settlement_activities(self, after):
        # Alpaca-py exposes the REST GET helper; retrieve all pages, then match
        # option ownership and paired stock delivery before changing our ledger.
        params={'activity_types':'OPASN,OPEXP,OPTRD','after':after,'direction':'asc','page_size':100}
        rows=[]
        while True:
            page=self.trading.get('/account/activities',data=params)
            if not isinstance(page,list):
                raise ValueError('Invalid settlement activity response')
            rows.extend(page)
            if len(page)<100:
                return rows
            params['page_token']=page[-1]['id']

    def submit(self, candidate, qty, side, price, client_id):
        if not client_id.startswith(f'{STRATEGY_ID}_{candidate.underlying}_'):
            raise ValueError('Every new order must carry this bot\'s strategy identifier')
        parsed=parse_option(candidate.symbol)
        if (qty!=1 or isinstance(qty,bool) or not parsed or parsed['kind']!='P'
            or side not in {'sell','buy'} or parsed['underlying']!=candidate.underlying
            or parsed['strike']!=candidate.strike or parsed['expiry']!=candidate.expiry
            or not math.isfinite(price) or price<=0):
            raise ValueError('Alpaca adapter accepts only one short-put contract or its buyback')
        self.validate_submission(candidate, qty, side, client_id)
        return self.trading.submit_order(order_data=LimitOrderRequest(
            symbol=candidate.symbol, qty=qty, side=OrderSide(side),
            position_intent=PositionIntent.SELL_TO_OPEN if side == "sell" else PositionIntent.BUY_TO_CLOSE,
            time_in_force=TimeInForce.DAY, limit_price=price, client_order_id=client_id))

    def validate_submission(self, candidate, qty, side, client_id):
        """Final fail-closed check immediately before any order reaches Alpaca.

        This repeats the execution layer's safeguards so calling the adapter
        directly cannot open a long put, an unsecured put, or an unowned close.
        """
        ledger=getattr(self,'ledger',None)
        cfg=getattr(self,'cfg',None)
        if ledger is None or cfg is None or not cfg.paper:
            raise ValueError('Paper configuration and owned ledger are required')
        row=ledger.db.execute('SELECT * FROM orders WHERE client_id=?',(client_id,)).fetchone()
        if (row is None or row['status']!='intent' or row['broker_id'] is not None
            or row['symbol']!=candidate.symbol or row['underlying']!=candidate.underlying
            or row['side']!=side or row['qty']!=qty or row['strike']!=candidate.strike
            or row['expiry']!=candidate.expiry.isoformat()):
            raise ValueError('Submission must match a fresh owned order intent')
        contract=self.trading.get_option_contract(candidate.symbol)
        if (str(getattr(contract.type,'value',contract.type))!='put'
            or contract.symbol!=candidate.symbol or contract.root_symbol!=candidate.underlying
            or float(contract.size or 0)!=100 or float(contract.strike_price)!=candidate.strike
            or contract.expiration_date!=candidate.expiry):
            raise ValueError('Only verified standard 100-share put contracts are permitted')
        positions=self.positions()
        orders=self.orders()
        if side=='sell':
            if not cfg.enable_entries or ledger.blocked():
                raise ValueError('New cash-secured put entries are disabled or blocked')
            # Current intent already reserves cash in SQLite. Exclude it once
            # when checking whether this proposed order fits; retain all others.
            reserved=ledger.reserved()-candidate.strike*qty*100
            allowed,reason,_=virtual_capacity(cfg,candidate.strike*qty*100,reserved,ledger.realized())
            if not allowed:
                raise ValueError(reason)
            pending=[o for o in ledger.pending() if o['client_id']!=client_id]
            allowed,reason=capacity(cfg,self.account(),positions,orders,candidate,
                own_lots=ledger.lots(),own_pending=pending,realized=ledger.realized())
            if not allowed:
                raise ValueError(reason)
        else:
            lot=ledger.lots().get(candidate.symbol)
            position=next((p for p in positions if p.symbol==candidate.symbol),None)
            if (not lot or lot['qty']<qty or position is None
                or float(position.qty)!=-lot['qty']):
                raise ValueError('Buyback requires this strategy\'s confirmed short-put position')
            if (any(o.symbol==candidate.symbol for o in orders)
                or any(o['symbol']==candidate.symbol and o['client_id']!=client_id for o in ledger.pending())):
                raise ValueError('Another order already exists for this put; duplicate buyback blocked')

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

    def reject_candidate(self, reason, candidate=None, **details):
        sink = getattr(self, "rejection_sink", None)
        if sink:
            sink(reason, candidate, **details)

    def candidates(self, underlying):
        cfg = self.cfg
        today = datetime.now(NY).date()
        latest = self.stocks.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=underlying, feed=DataFeed.IEX))[underlying]
        if not -5 <= (datetime.now(timezone.utc) - latest.timestamp).total_seconds() <= cfg.quote_max_age:
            bot_log(f"CONTRACT SCAN {underlying}: stale underlying trade")
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
        rejected = Counter()
        for contract in contracts:
            strike = float(contract.strike_price)
            if (contract.tradable and str(getattr(contract.type, "value", contract.type)) == "put"
                and float(contract.size or 0) == 100 and contract.root_symbol == underlying
                and 0 < strike < spot
                and float(contract.open_interest or 0) >= cfg.min_open_interest
                and cfg.min_dte <= (contract.expiration_date - today).days <= cfg.max_dte):
                eligible.append(contract)
            else:
                self.reject_candidate("INSUFFICIENT_LIQUIDITY" if float(contract.open_interest or 0)<cfg.min_open_interest else "NO_VALID_CONTRACT",
                    Candidate(contract.symbol,underlying,strike,contract.expiration_date,0,0,underlying_price=spot),
                    details="contract metadata/open-interest filter; unavailable quote fields are zero")
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
                raw = Candidate(contract.symbol, underlying, float(contract.strike_price), contract.expiration_date,
                    float(getattr(quote,'bid_price',0) or 0), float(getattr(quote,'ask_price',0) or 0), underlying_price=spot)
                if not valid_quote(quote, cfg.quote_max_age):
                    self.reject_candidate("OTHER", raw, details="quote unavailable, invalid, or stale")
                    rejected["quote_unavailable_or_stale"] += 1
                    continue
                if delta is None:
                    self.reject_candidate("NO_VALID_CONTRACT", raw, details="missing delta")
                    rejected["missing_delta"] += 1
                    continue
                delta = float(delta)
                bid, ask = float(quote.bid_price), float(quote.ask_price)
                strike = float(contract.strike_price)
                failures = {
                    "delta": not math.isfinite(delta) or delta >= 0 or abs(delta - cfg.target_delta) > cfg.delta_tolerance,
                    "spread": (ask - bid) / ((ask + bid) / 2) > cfg.max_spread,
                    "volume": volume < cfg.min_volume,
                    "credit_yield": bid / strike < cfg.min_credit_yield,
                }
                rejected.update(name for name, failed in failures.items() if failed)
                if any(failures.values()):
                    reason = ("NO_VALID_CONTRACT" if failures['delta'] else "SPREAD_TOO_WIDE" if failures['spread']
                              else "INSUFFICIENT_LIQUIDITY" if failures['volume'] else "NO_VALID_CONTRACT")
                    self.reject_candidate(reason, raw, details=','.join(k for k,v in failures.items() if v))
                    continue
                ranked.append(Candidate(contract.symbol, underlying, strike, contract.expiration_date, bid, ask, delta, spot))
        bot_log(f"CONTRACT SCAN {underlying}: chain={len(contracts)} metadata_pass={len(eligible)} "
                f"qualified={len(ranked)} rejected_filters={dict(rejected)} "
                f"collateral_cap=${cfg.max_collateral_per_trade:,.0f}")
        return sorted(ranked, key=lambda c: (abs(c.delta - cfg.target_delta), (c.ask - c.bid) / c.ask, c.strike))


class Trader:
    def __init__(self, cfg, broker, ledger):
        self.cfg, self.broker, self.ledger = cfg, broker, ledger
        self.broker.ledger = ledger

    def reconcile(self):
        healthy = True
        for row in self.ledger.pending():
            try:
                order = self.broker.lookup(row["client_id"])
                verify_owned_order(row, order)
                status = str(getattr(order.status, "value", order.status))
                self.ledger.reconcile(row["client_id"], str(order.id), status,
                                      float(order.filled_qty or 0), float(order.filled_avg_price or 0))
                if status=='rejected' and row['side']=='sell':
                    candidate=Candidate(row['symbol'],row['underlying'],row['strike'],date.fromisoformat(row['expiry']),0,0)
                    self.reject('OTHER',candidate,row['strategy'],row['signal_date'],'broker rejected order '+row['client_id'])
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["created"])).total_seconds()
                timeout = self.cfg.entry_timeout if row["side"] == "sell" else self.cfg.exit_timeout
                violates_allocation = row['side']=='sell' and (
                    row['qty']>self.cfg.max_contracts_per_trade
                    or row['strike']*row['qty']*100>min(self.cfg.max_collateral_per_trade,self.cfg.virtual_starting_capital)
                    or self.ledger.reserved()>min(self.cfg.max_total_collateral,self.cfg.virtual_starting_capital))
                if status not in TERMINAL and (age >= timeout or violates_allocation):
                    self.broker.cancel(str(order.id))
                    if violates_allocation:
                        bot_log(f'{STRATEGY_ID}: cancelling own pending entry outside virtual allocation: {row["client_id"]}')
                    # Keep reserves and pending status until cancellation is confirmed.
            except Exception as exc:
                healthy = False
                bot_log(f"Order reconciliation pending for {row['client_id']}: {exc}")
        positions = {p.symbol: p for p in self.broker.positions()}
        pending_symbols = {r["symbol"] for r in self.ledger.pending()}
        for symbol, lot in self.ledger.lots().items():
            actual = float(positions[symbol].qty) if symbol in positions else 0
            if actual != -lot["qty"] and symbol not in pending_symbols:
                if hasattr(self.broker,'settlement_activities'):
                    try:
                        opened=self.ledger.db.execute("SELECT MIN(created) FROM orders WHERE symbol=?",(symbol,)).fetchone()[0]
                        activities=self.broker.settlement_activities(opened[:10])
                        for activity in activities:
                            if activity.get('symbol')!=symbol or float(activity.get('qty',0))!=lot['qty']+actual:
                                continue
                            pairs=[a for a in activities if a.get('activity_type')=='OPTRD' and a.get('symbol')==lot['underlying'] and a.get('date')==activity.get('date') and float(a.get('qty',0))==float(activity.get('qty',0))*100 and float(a.get('price',0))==lot['strike']]
                            self.ledger.settlement(activity,pairs[0] if len(pairs)==1 else None)
                        if actual==-self.ledger.lots().get(symbol,{'qty':0})['qty']:
                            continue
                    except Exception as exc:
                        bot_log(f'Settlement reconciliation unavailable for {symbol}: {exc}')
                healthy = False
                if not self.ledger.blocked():
                    self.ledger.event("RECONCILIATION_REQUIRED", symbol, expected=-lot["qty"], actual=actual,
                                      reason="Possible assignment, expiration, or external trade; preserve basis and reserve")
                bot_log(f"RECONCILIATION REQUIRED: {symbol}, ledger={-lot['qty']}, broker={actual}")
        return healthy and not self.ledger.blocked()

    def submit(self, candidate, strategy, side, qty, price, signal_date=""):
        parsed = parse_option(candidate.symbol)
        if not parsed or parsed['kind']!='P' or parsed['underlying']!=candidate.underlying or parsed['strike']!=candidate.strike or side not in {'sell','buy'}:
            return self.reject("OTHER", candidate, strategy, signal_date, "Only short puts are permitted")
        if qty != 1 or qty > self.cfg.max_contracts_per_trade:
            return self.reject("MAX_CONTRACTS_REACHED", candidate, strategy, signal_date)
        if side=='sell':
            if not self.cfg.enable_entries or self.ledger.blocked():
                return self.reject("OTHER",candidate,strategy,signal_date,"entries disabled or reconciliation required")
            allowed, reason = self.entry_capacity(candidate)
            if not allowed:
                return self.reject(reason,candidate,strategy,signal_date)
        else:
            lot=self.ledger.lots().get(candidate.symbol)
            if not lot or qty>lot['qty']:
                return False
            positions={p.symbol:p for p in self.broker.positions()}
            if candidate.symbol not in positions or float(positions[candidate.symbol].qty)!=-lot['qty']:
                return False
        client_id = f"{STRATEGY_ID}_{candidate.underlying}_{uuid4().hex[:16]}"
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
            if side=='sell':
                self.reject("OTHER",candidate,strategy,signal_date,"submission uncertain: "+str(exc))
            bot_log(f"Submission requires reconciliation: {client_id}: {exc}")
            return False

    def reject(self, reason, candidate, strategy, signal_date, details=""):
        self.ledger.reject(reason,candidate,self.cfg,variant=strategy,signal_date=signal_date,details=details)
        bot_log(f"{STRATEGY_ID} ENTRY SKIP {candidate.symbol}: {reason} {details}")
        return False

    def entry_capacity(self,candidate):
        ok,reason,_=virtual_capacity(self.cfg,candidate.strike*100,self.ledger.reserved(),self.ledger.realized())
        if not ok:
            return False,reason
        return capacity(self.cfg,self.broker.account(),self.broker.positions(),self.broker.orders(),candidate,
            own_lots=self.ledger.lots(),own_pending=self.ledger.pending(),realized=self.ledger.realized())

    def enter(self, candidate, strategy, signal_date):
        # This is the preferred contract; failure must never trigger a cheaper fallback.
        if not self.cfg.enable_entries or not self.reconcile():
            return self.reject("OTHER",candidate,strategy,signal_date,"entries disabled or reconciliation incomplete")
        pending = self.ledger.pending()
        if any(r["side"] == "sell" or r["status"] == "intent" for r in pending):
            return self.reject("MAX_STRATEGY_EXPOSURE_REACHED",candidate,strategy,signal_date,"pending entry or uncertain order")
        if self.ledger.traded_bar(candidate.underlying, signal_date):
            return self.reject("DUPLICATE_POSITION",candidate,strategy,signal_date,"already submitted on signal bar")
        last_exit = self.ledger.last_exit(candidate.underlying)
        if last_exit and (datetime.now(NY).date() - last_exit).days < self.cfg.cooldown_days:
            return self.reject("OTHER",candidate,strategy,signal_date,"reentry cooldown")
        if not self.broker.earnings_clear(candidate.underlying, candidate.expiry):
            return self.reject("OTHER",candidate,strategy,signal_date,"earnings unknown or within holding window")
        snap = self.broker.snapshot(candidate.symbol)
        quote = getattr(snap, "latest_quote", None)
        if not valid_quote(quote, self.cfg.quote_max_age):
            return self.reject("OTHER",candidate,strategy,signal_date,"fresh entry quote unavailable")
        bid, ask = float(quote.bid_price), float(quote.ask_price)
        candidate=replace(candidate,bid=bid,ask=ask)
        if (ask-bid)/((ask+bid)/2)>self.cfg.max_spread:
            return self.reject("SPREAD_TOO_WIDE",candidate,strategy,signal_date)
        if bid/candidate.strike<self.cfg.min_credit_yield:
            return self.reject("NO_VALID_CONTRACT",candidate,strategy,signal_date,"credit yield below minimum")
        allowed, reason = self.entry_capacity(candidate)
        if not allowed:
            return self.reject(reason,candidate,strategy,signal_date)
        return self.submit(candidate,strategy,"sell",1,(bid+ask)/2,signal_date)

    def manage_exits(self):
        positions = {p.symbol: p for p in self.broker.positions()}
        open_orders = self.broker.orders()
        pending = {r["symbol"] for r in self.ledger.pending()} | {o.symbol for o in open_orders}
        marks = {}
        for assigned in self.ledger.assigned():
            root=parse_option(assigned['symbol'])['underlying']
            if root in positions and float(positions[root].qty)>=assigned['qty']*100:
                price=float(getattr(positions[root],'current_price',0) or 0)
                if math.isfinite(price) and price>0:
                    marks['stock:'+root]=price
        for symbol, lot in self.ledger.lots().items():
            try:
                if symbol not in positions or float(positions[symbol].qty) != -lot["qty"]:
                    continue
                snap = self.broker.snapshot(symbol)
                quote = getattr(snap, "latest_quote", None)
                if not valid_quote(quote, self.cfg.quote_max_age, for_exit=True):
                    bot_log(f"Exit quote unavailable/stale: {symbol}")
                    continue
                ask = float(quote.ask_price)
                marks[symbol] = ask
                if symbol in pending:
                    continue
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
