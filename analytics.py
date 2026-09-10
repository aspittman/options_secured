"""Durable order intents and idempotent cumulative-fill accounting."""
import csv
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from config import STRATEGY_ID
from performance import statistics
from risk import parse_option, virtual_capacity

TERMINAL = {"filled", "canceled", "expired", "rejected"}
REJECTION_FIELDS = ["timestamp", "strategy", "variant", "underlying", "contract_symbol",
    "call_or_put", "long_or_short", "strike", "expiration", "DTE", "underlying_price",
    "bid", "ask", "mid", "spread_dollars", "spread_percent", "option_premium",
    "required_capital", "virtual_capital_available", "rejection_reason", "signal_score",
    "market_regime", "signal_date", "details"]


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS orders (
                client_id TEXT PRIMARY KEY, broker_id TEXT, symbol TEXT,
                underlying TEXT, strategy TEXT, side TEXT, qty INTEGER,
                strike REAL, expiry TEXT, signal_date TEXT, created TEXT,
                status TEXT, filled REAL DEFAULT 0, notional REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS lots (
                symbol TEXT PRIMARY KEY, underlying TEXT, strategy TEXT,
                qty REAL, credit REAL, strike REAL, expiry TEXT);
            CREATE TABLE IF NOT EXISTS events (
                timestamp TEXT, event TEXT, symbol TEXT, details TEXT);
            CREATE TABLE IF NOT EXISTS pnl (
                timestamp TEXT, symbol TEXT, strategy TEXT, qty REAL, realized REAL);
            CREATE TABLE IF NOT EXISTS research_rejections (timestamp TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS fills (timestamp TEXT, client_id TEXT, symbol TEXT,
                side TEXT, qty REAL, price REAL, strike REAL, estimated_time INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS equity_samples (timestamp TEXT, equity REAL, reserved_collateral REAL);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS settlements (activity_id TEXT PRIMARY KEY, timestamp TEXT,
                symbol TEXT, outcome TEXT, qty REAL, credit REAL, strike REAL);
        """)
        # Migrate only once. Existing positions/orders are never discarded.
        if not self.db.execute("SELECT 1 FROM metadata WHERE key='fills_migrated'").fetchone():
            with self.db:
                self.db.execute("INSERT INTO fills SELECT created,client_id,symbol,side,filled,notional/filled,strike,1 FROM orders WHERE filled>0 ORDER BY created")
                self.db.execute("INSERT INTO metadata VALUES ('fills_migrated',?)", (now_iso(),))

    def event(self, event, symbol="", **details):
        details['strategy']=STRATEGY_ID
        with self.db:
            self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (now_iso(), event, symbol, json.dumps(details)))

    def intent(self, client_id, candidate, strategy, side, qty, signal_date=""):
        parsed = parse_option(candidate.symbol)
        if not parsed or parsed['kind'] != 'P' or side not in {'sell','buy'}:
            raise ValueError('Ledger only accepts short-put entry/close orders')
        with self.db:
            self.db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0)", (
                client_id, None, candidate.symbol, candidate.underlying, strategy, side,
                qty, candidate.strike, candidate.expiry.isoformat(), signal_date, now_iso(), "intent"))

    def pending(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM orders") if r["status"] not in TERMINAL]

    def lots(self):
        return {r["symbol"]: dict(r) for r in self.db.execute("SELECT * FROM lots WHERE qty > 0")}

    def blocked(self):
        return (bool(self.db.execute("SELECT 1 FROM events WHERE event='RECONCILIATION_REQUIRED' LIMIT 1").fetchone())
                or bool(self.db.execute("SELECT 1 FROM settlements WHERE outcome='assignment' LIMIT 1").fetchone()))

    def assigned(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM settlements WHERE outcome='assignment'")]

    def settlement(self, activity, paired_stock_activity=None):
        """Only confirmed, symbol-matched Alpaca activities settle a ledger-owned put.

        Assignment retains the full stock cost as employed capital and pauses new
        entries for review. No stock orders are submitted by this strategy.
        """
        if self.db.execute('SELECT 1 FROM settlements WHERE activity_id=?',(activity.get('id'),)).fetchone():
            return False
        lot=self.lots().get(activity.get('symbol'))
        if not lot or activity.get('status')!='executed' or activity.get('activity_type') not in {'OPASN','OPEXP'}:
            return False
        qty=float(activity.get('qty',0))
        if qty<=0 or qty!=int(qty) or qty>lot['qty']:
            return False
        latest=self.db.execute("SELECT MAX(timestamp) FROM fills WHERE symbol=? AND side='sell'",(lot['symbol'],)).fetchone()[0]
        if not latest or activity.get('date','')<latest[:10]:
            return False
        outcome='assignment' if activity['activity_type']=='OPASN' else 'expiration'
        if outcome=='assignment':
            stock=paired_stock_activity or {}
            if (stock.get('activity_type')!='OPTRD' or stock.get('status')!='executed'
                or stock.get('symbol')!=lot['underlying'] or stock.get('date')!=activity['date']
                or float(stock.get('qty',0))!=qty*100 or float(stock.get('price',0))!=lot['strike']):
                return False
        elif activity['date']<lot['expiry']:
            return False
        stamp=activity['date']+'T23:59:59+00:00'
        with self.db:
            self.db.execute('INSERT INTO settlements VALUES (?,?,?,?,?,?,?)',
                (activity['id'],stamp,lot['symbol'],outcome,qty,lot['credit'],lot['strike']))
            self.db.execute('INSERT INTO pnl VALUES (?,?,?,?,?)',(stamp,lot['symbol'],lot['strategy'],qty,lot['credit']*qty*100))
            self.db.execute('UPDATE lots SET qty=qty-? WHERE symbol=?',(qty,lot['symbol']))
        return True

    def traded_bar(self, underlying, signal_date):
        return bool(self.db.execute("SELECT 1 FROM orders WHERE underlying=? AND signal_date=? AND side='sell'",
                                    (underlying, signal_date)).fetchone())

    def last_exit(self, underlying):
        row = self.db.execute("SELECT MAX(created) FROM orders WHERE underlying=? AND side='buy' AND filled>0", (underlying,)).fetchone()
        return datetime.fromisoformat(row[0]).date() if row[0] else None

    def reconcile(self, client_id, broker_id, status, filled, average):
        """Apply only the incremental quantity and notional; cancellation is not a fill."""
        if not math.isfinite(filled) or filled < 0 or filled != int(filled):
            raise ValueError("Invalid filled quantity")
        if not math.isfinite(average) or (filled > 0 and average <= 0):
            raise ValueError("Invalid broker fill price")
        with self.db:
            row = self.db.execute("SELECT * FROM orders WHERE client_id=?", (client_id,)).fetchone()
            if filled < row["filled"] or filled > row["qty"]:
                raise ValueError("Inconsistent broker cumulative quantity")
            delta = filled - row["filled"]
            notional = filled * average
            if delta > 0:
                price = (notional - row["notional"]) / delta
                if price <= 0:
                    raise ValueError("Invalid incremental fill price")
                self.db.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,0)",
                    (now_iso(), client_id, row['symbol'], row['side'], delta, price, row['strike']))
                lot = self.db.execute("SELECT * FROM lots WHERE symbol=?", (row["symbol"],)).fetchone()
                if row["side"] == "sell":
                    old_qty, old_credit = (lot["qty"], lot["credit"]) if lot else (0, 0)
                    self.db.execute("INSERT OR REPLACE INTO lots VALUES (?,?,?,?,?,?,?)", (
                        row["symbol"], row["underlying"], row["strategy"], old_qty + delta,
                        (old_credit * old_qty + price * delta) / (old_qty + delta), row["strike"], row["expiry"]))
                else:
                    if not lot or delta > lot["qty"]:
                        raise ValueError("Close fill exceeds owned short quantity")
                    self.db.execute("INSERT INTO pnl VALUES (?,?,?,?,?)", (
                        now_iso(), row["symbol"], row["strategy"], delta, (lot["credit"] - price) * delta * 100))
                    self.db.execute("UPDATE lots SET qty=qty-? WHERE symbol=?", (delta, row["symbol"]))
            self.db.execute("UPDATE orders SET broker_id=?, status=?, filled=?, notional=? WHERE client_id=?",
                            (broker_id, status, filled, notional, client_id))

    def realized(self):
        return float(self.db.execute('SELECT COALESCE(SUM(realized),0) FROM pnl').fetchone()[0])

    def reserved(self):
        return (sum(l['qty']*l['strike']*100 for l in self.lots().values())
                + sum((o['qty']-o['filled'])*o['strike']*100 for o in self.pending() if o['side']=='sell')
                + float(self.db.execute("SELECT COALESCE(SUM(qty*strike*100),0) FROM settlements WHERE outcome='assignment'").fetchone()[0]))

    def available(self, cfg):
        return virtual_capacity(cfg, 0, self.reserved(), self.realized())[2]

    def reject(self, reason, candidate=None, cfg=None, **context):
        row = dict.fromkeys(REJECTION_FIELDS)
        row.update(timestamp=now_iso(), strategy=STRATEGY_ID, call_or_put='put', long_or_short='short',
                   rejection_reason=reason, market_regime='sideways_to_mildly_bullish')
        row.update({k:v for k,v in context.items() if k in row})
        if candidate is not None:
            mid = (candidate.bid+candidate.ask)/2
            row.update(underlying=candidate.underlying, contract_symbol=candidate.symbol,
                strike=candidate.strike, expiration=candidate.expiry.isoformat(),
                DTE=(candidate.expiry-datetime.now(timezone.utc).date()).days,
                underlying_price=getattr(candidate,'underlying_price',None),
                bid=candidate.bid, ask=candidate.ask, mid=mid,
                spread_dollars=candidate.ask-candidate.bid,
                spread_percent=(candidate.ask-candidate.bid)/mid*100 if mid>0 else None,
                option_premium=mid*100, required_capital=candidate.strike*100)
            if candidate.bid==candidate.ask==0:
                for field in ('bid','ask','mid','spread_dollars','spread_percent','option_premium'):
                    row[field]=None
        if cfg is not None:
            row['virtual_capital_available']=self.available(cfg)
        with self.db:
            self.db.execute('INSERT INTO research_rejections VALUES (?,?)',(row['timestamp'],json.dumps(row)))

    def record_equity(self, marks, cfg):
        lots = self.lots()
        unrealized=self.unrealized(marks)
        equity = cfg.virtual_starting_capital+self.realized()+unrealized if unrealized is not None else None
        with self.db:
            self.db.execute('INSERT INTO equity_samples VALUES (?,?,?)',(now_iso(),equity,self.reserved()))

    def unrealized(self,marks):
        lots=self.lots()
        if self.db.execute("SELECT 1 FROM events WHERE event='RECONCILIATION_REQUIRED' LIMIT 1").fetchone():
            return None
        stock_rows=self.assigned()
        if not all(s in marks for s in lots) or not all('stock:'+parse_option(r['symbol'])['underlying'] in marks for r in stock_rows):
            return None
        return (sum((l['credit']-marks[s])*l['qty']*100 for s,l in lots.items())
                + sum((marks['stock:'+parse_option(r['symbol'])['underlying']]-r['strike'])*r['qty']*100 for r in stock_rows))

    def report(self, marks=None, starting_capital=25000):
        marks = marks or {}
        results = {}
        for row in self.db.execute("SELECT strategy, SUM(realized) AS realized FROM pnl GROUP BY strategy"):
            results[row["strategy"]] = {"realized_pnl": row["realized"]}
        for lot in self.lots().values():
            stats = results.setdefault(lot["strategy"], {"realized_pnl": 0})
            stats["collateral"] = stats.get("collateral", 0) + lot["qty"] * lot["strike"] * 100
            if lot["symbol"] in marks:
                stats["unrealized_pnl"] = stats.get("unrealized_pnl", 0) + (lot["credit"] - marks[lot["symbol"]]) * lot["qty"] * 100
            else:
                stats["unmarked_positions"] = stats.get("unmarked_positions", 0) + 1
        inventory, trades = {}, []
        entries=[{'collateral':r['capital'],'premium':r['premium']} for r in self.db.execute(
            "SELECT client_id,SUM(strike*qty*100) capital,SUM(price*qty*100) premium FROM fills WHERE side='sell' GROUP BY client_id")]
        for fill in self.db.execute('SELECT * FROM fills ORDER BY timestamp,rowid'):
            if fill['side']=='sell':
                inventory.setdefault(fill['symbol'],[]).append(dict(fill))
            else:
                remaining=fill['qty']
                while remaining>0 and inventory.get(fill['symbol']):
                    entry=inventory[fill['symbol']][0]
                    qty=min(remaining,entry['qty'])
                    trades.append({'pnl':(entry['price']-fill['price'])*qty*100,
                        'hold_days':(datetime.fromisoformat(fill['timestamp'])-datetime.fromisoformat(entry['timestamp'])).total_seconds()/86400})
                    entry['qty']-=qty; remaining-=qty
                    if entry['qty']==0: inventory[fill['symbol']].pop(0)
        for row in self.db.execute('SELECT * FROM settlements'):
            opened=next(iter(inventory.get(row['symbol'],[])),None)
            hold=(datetime.fromisoformat(row['timestamp'])-datetime.fromisoformat(opened['timestamp'])).total_seconds()/86400 if opened else None
            trades.append({'pnl':row['credit']*row['qty']*100, 'hold_days':hold, 'outcome':row['outcome']})
        lots=self.lots()
        unrealized=self.unrealized(marks)
        curve=[dict(r) for r in self.db.execute('SELECT equity,reserved_collateral FROM equity_samples ORDER BY timestamp')]
        changes=[(r['timestamp'],r['strike']*r['qty']*100*(1 if r['side']=='sell' else -1))
                 for r in self.db.execute('SELECT * FROM fills')]
        changes.extend((r['timestamp'],-r['strike']*r['qty']*100) for r in self.db.execute("SELECT * FROM settlements WHERE outcome='expiration'"))
        historical_reserved=historical_peak=0
        for _,change in sorted(changes,key=lambda r:r[0]):
            historical_reserved=max(0,historical_reserved+change)
            historical_peak=max(historical_peak,historical_reserved)
        metrics=statistics(starting_capital,self.realized(),unrealized,trades,entries,curve)
        metrics['current_capital_employed']=self.reserved()
        metrics['maximum_capital_employed']=max(metrics['maximum_capital_employed'],self.reserved(),historical_peak)
        metrics['exposure_pct']=self.reserved()/starting_capital*100
        metrics['legacy_over_allocation']=any(e['collateral']>starting_capital for e in entries)
        metrics['research_comparable']=not metrics['legacy_over_allocation']
        metrics['fill_times_estimated']=bool(self.db.execute('SELECT 1 FROM fills WHERE estimated_time=1 LIMIT 1').fetchone())
        metrics['drawdown_basis']='observed marked equity since research upgrade; not reconstructed historical marks'
        metrics['outcome_rate_denominator']='completed option positions; assigned stock remains employed and marked separately'
        metrics['assigned_share_cost']=sum(r['strike']*r['qty']*100 for r in self.assigned())
        metrics['settlement_times_estimated']=bool(self.db.execute('SELECT 1 FROM settlements LIMIT 1').fetchone())
        return {"strategy": STRATEGY_ID, "metrics":metrics, "by_strategy": results, "open_positions": list(lots.values()),
                "pending_orders": len(self.pending()), "reconciliation_required": self.blocked()}

    def export(self, path="logs/trade_analytics.csv"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        cursor = self.db.execute("SELECT timestamp,symbol,'cash_secured_put' AS strategy,strategy AS variant,qty,realized FROM pnl ORDER BY timestamp")
        with open(path, "w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow([col[0] for col in cursor.description])
            writer.writerows(cursor)
        with Path(path).with_name('rejected_trades.csv').open('w',newline='') as file:
            writer=csv.DictWriter(file,fieldnames=REJECTION_FIELDS)
            writer.writeheader()
            for row in self.db.execute('SELECT payload FROM research_rejections ORDER BY rowid'):
                writer.writerow(json.loads(row[0]))
