"""Durable order intents and idempotent cumulative-fill accounting."""
import csv
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

TERMINAL = {"filled", "canceled", "expired", "rejected"}


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
        """)

    def event(self, event, symbol="", **details):
        with self.db:
            self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (now_iso(), event, symbol, json.dumps(details)))

    def intent(self, client_id, candidate, strategy, side, qty, signal_date=""):
        with self.db:
            self.db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0)", (
                client_id, None, candidate.symbol, candidate.underlying, strategy, side,
                qty, candidate.strike, candidate.expiry.isoformat(), signal_date, now_iso(), "intent"))

    def pending(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM orders") if r["status"] not in TERMINAL]

    def lots(self):
        return {r["symbol"]: dict(r) for r in self.db.execute("SELECT * FROM lots WHERE qty > 0")}

    def blocked(self):
        return bool(self.db.execute("SELECT 1 FROM events WHERE event='RECONCILIATION_REQUIRED' LIMIT 1").fetchone())

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

    def report(self, marks=None):
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
        return {"by_strategy": results, "open_positions": list(self.lots().values()),
                "pending_orders": len(self.pending()), "reconciliation_required": self.blocked()}

    def export(self, path="logs/trade_analytics.csv"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        cursor = self.db.execute("SELECT * FROM pnl ORDER BY timestamp")
        with open(path, "w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow([col[0] for col in cursor.description])
            writer.writerows(cursor)
