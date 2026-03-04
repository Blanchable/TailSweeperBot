"""
SQLite persistence layer.
Thread-safe via check_same_thread=False and explicit locking.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone, date
from typing import List, Optional, Dict, Any

from models.data_models import (
    Position, OpenOrder, TradeRecord, EventLogEntry,
    EquitySnapshot, PnLSummary,
)
from config import Settings


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return date.today().isoformat()


class Database:
    """SQLite storage for all app state."""

    def __init__(self, db_path: str):
        self._path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _create_tables(self):
        with self._lock:
            c = self._conn
            c.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id TEXT NOT NULL,
                    condition_id TEXT,
                    market_question TEXT,
                    outcome TEXT,
                    shares REAL DEFAULT 0,
                    avg_entry REAL DEFAULT 0,
                    cost_basis REAL DEFAULT 0,
                    current_mark REAL DEFAULT 0,
                    current_bid REAL DEFAULT 0,
                    unrealized_pnl REAL DEFAULT 0,
                    unrealized_pnl_pct REAL DEFAULT 0,
                    next_exit_rung INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT,
                    is_paper INTEGER DEFAULT 1
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pos_token
                    ON positions(token_id, is_paper);

                CREATE TABLE IF NOT EXISTS open_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT UNIQUE NOT NULL,
                    token_id TEXT,
                    condition_id TEXT,
                    market_question TEXT,
                    side TEXT,
                    price REAL,
                    size REAL,
                    remaining_size REAL,
                    status TEXT DEFAULT 'OPEN',
                    post_only INTEGER DEFAULT 0,
                    created_at TEXT,
                    is_paper INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    action TEXT,
                    market_question TEXT,
                    outcome TEXT,
                    token_id TEXT,
                    price REAL,
                    size REAL,
                    gross_value REAL DEFAULT 0,
                    fees REAL DEFAULT 0,
                    realized_pnl REAL DEFAULT 0,
                    notes TEXT DEFAULT '',
                    is_paper INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS event_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    level TEXT,
                    message TEXT
                );

                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    total_exposure REAL DEFAULT 0,
                    cash_reserved REAL DEFAULT 0,
                    unrealized_pnl REAL DEFAULT 0,
                    realized_pnl REAL DEFAULT 0,
                    total_pnl REAL DEFAULT 0
                );
            """)
            c.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def save_settings(self, settings: Settings):
        d = settings.to_dict()
        with self._lock:
            for k, v in d.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (k, str(v)),
                )
            self._conn.commit()

    def load_settings(self) -> Optional[Settings]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        if not rows:
            return None
        d: Dict[str, Any] = {r["key"]: r["value"] for r in rows}
        return Settings.from_dict(d)

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------
    def upsert_position(self, pos: Position) -> int:
        with self._lock:
            now = _now()
            existing = self._conn.execute(
                "SELECT id FROM positions WHERE token_id=? AND is_paper=?",
                (pos.token_id, int(pos.is_paper)),
            ).fetchone()
            if existing:
                self._conn.execute("""
                    UPDATE positions SET
                        condition_id=?, market_question=?, outcome=?,
                        shares=?, avg_entry=?, cost_basis=?,
                        current_mark=?, current_bid=?,
                        unrealized_pnl=?, unrealized_pnl_pct=?,
                        next_exit_rung=?, updated_at=?
                    WHERE id=?
                """, (
                    pos.condition_id, pos.market_question, pos.outcome,
                    pos.shares, pos.avg_entry, pos.cost_basis,
                    pos.current_mark, pos.current_bid,
                    pos.unrealized_pnl, pos.unrealized_pnl_pct,
                    pos.next_exit_rung, now, existing["id"],
                ))
                self._conn.commit()
                return existing["id"]
            else:
                cur = self._conn.execute("""
                    INSERT INTO positions
                        (token_id, condition_id, market_question, outcome,
                         shares, avg_entry, cost_basis,
                         current_mark, current_bid,
                         unrealized_pnl, unrealized_pnl_pct,
                         next_exit_rung, created_at, updated_at, is_paper)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    pos.token_id, pos.condition_id, pos.market_question, pos.outcome,
                    pos.shares, pos.avg_entry, pos.cost_basis,
                    pos.current_mark, pos.current_bid,
                    pos.unrealized_pnl, pos.unrealized_pnl_pct,
                    pos.next_exit_rung, now, now, int(pos.is_paper),
                ))
                self._conn.commit()
                return cur.lastrowid

    def get_positions(self, is_paper: bool = True) -> List[Position]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM positions WHERE is_paper=? AND shares > 0 ORDER BY created_at DESC",
                (int(is_paper),),
            ).fetchall()
        return [self._row_to_position(r) for r in rows]

    def get_position_by_token(self, token_id: str, is_paper: bool = True) -> Optional[Position]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM positions WHERE token_id=? AND is_paper=?",
                (token_id, int(is_paper)),
            ).fetchone()
        return self._row_to_position(row) if row else None

    def delete_position(self, token_id: str, is_paper: bool = True):
        with self._lock:
            self._conn.execute(
                "DELETE FROM positions WHERE token_id=? AND is_paper=?",
                (token_id, int(is_paper)),
            )
            self._conn.commit()

    def remove_empty_positions(self, is_paper: bool = True):
        with self._lock:
            self._conn.execute(
                "DELETE FROM positions WHERE shares <= 0 AND is_paper=?",
                (int(is_paper),),
            )
            self._conn.commit()

    @staticmethod
    def _row_to_position(r) -> Position:
        return Position(
            id=r["id"], token_id=r["token_id"],
            condition_id=r["condition_id"], market_question=r["market_question"],
            outcome=r["outcome"], shares=r["shares"],
            avg_entry=r["avg_entry"], cost_basis=r["cost_basis"],
            current_mark=r["current_mark"], current_bid=r["current_bid"],
            unrealized_pnl=r["unrealized_pnl"],
            unrealized_pnl_pct=r["unrealized_pnl_pct"],
            next_exit_rung=r["next_exit_rung"],
            created_at=r["created_at"], updated_at=r["updated_at"],
            is_paper=bool(r["is_paper"]),
        )

    # ------------------------------------------------------------------
    # Open orders
    # ------------------------------------------------------------------
    def insert_order(self, order: OpenOrder) -> int:
        with self._lock:
            cur = self._conn.execute("""
                INSERT OR REPLACE INTO open_orders
                    (order_id, token_id, condition_id, market_question,
                     side, price, size, remaining_size, status,
                     post_only, created_at, is_paper)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                order.order_id, order.token_id, order.condition_id,
                order.market_question, order.side, order.price,
                order.size, order.remaining_size, order.status,
                int(order.post_only), order.created_at or _now(),
                int(order.is_paper),
            ))
            self._conn.commit()
            return cur.lastrowid

    def get_open_orders(self, is_paper: bool = True) -> List[OpenOrder]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM open_orders WHERE is_paper=? AND status='OPEN' ORDER BY created_at DESC",
                (int(is_paper),),
            ).fetchall()
        return [self._row_to_order(r) for r in rows]

    def get_order_by_id(self, order_id: str) -> Optional[OpenOrder]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM open_orders WHERE order_id=?", (order_id,)
            ).fetchone()
        return self._row_to_order(row) if row else None

    def update_order_status(self, order_id: str, status: str, remaining: Optional[float] = None):
        with self._lock:
            if remaining is not None:
                self._conn.execute(
                    "UPDATE open_orders SET status=?, remaining_size=? WHERE order_id=?",
                    (status, remaining, order_id),
                )
            else:
                self._conn.execute(
                    "UPDATE open_orders SET status=? WHERE order_id=?",
                    (status, order_id),
                )
            self._conn.commit()

    def cancel_all_open_orders(self, is_paper: bool = True):
        with self._lock:
            self._conn.execute(
                "UPDATE open_orders SET status='CANCELLED' WHERE status='OPEN' AND is_paper=?",
                (int(is_paper),),
            )
            self._conn.commit()

    def get_stale_orders(self, timeout_sec: int, is_paper: bool = True) -> List[OpenOrder]:
        with self._lock:
            rows = self._conn.execute("""
                SELECT * FROM open_orders
                WHERE status='OPEN' AND is_paper=?
                AND datetime(created_at) < datetime('now', ? || ' seconds')
            """, (int(is_paper), str(-timeout_sec))).fetchall()
        return [self._row_to_order(r) for r in rows]

    def has_open_order_for_token(self, token_id: str, side: str, is_paper: bool = True) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM open_orders WHERE token_id=? AND side=? AND status='OPEN' AND is_paper=?",
                (token_id, side, int(is_paper)),
            ).fetchone()
        return row is not None

    @staticmethod
    def _row_to_order(r) -> OpenOrder:
        return OpenOrder(
            id=r["id"], order_id=r["order_id"],
            token_id=r["token_id"], condition_id=r["condition_id"],
            market_question=r["market_question"], side=r["side"],
            price=r["price"], size=r["size"],
            remaining_size=r["remaining_size"], status=r["status"],
            post_only=bool(r["post_only"]),
            created_at=r["created_at"], is_paper=bool(r["is_paper"]),
        )

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------
    def insert_trade(self, trade: TradeRecord) -> int:
        with self._lock:
            cur = self._conn.execute("""
                INSERT INTO trades
                    (timestamp, action, market_question, outcome, token_id,
                     price, size, gross_value, fees, realized_pnl, notes, is_paper)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade.timestamp or _now(), trade.action,
                trade.market_question, trade.outcome, trade.token_id,
                trade.price, trade.size, trade.gross_value,
                trade.fees, trade.realized_pnl, trade.notes,
                int(trade.is_paper),
            ))
            self._conn.commit()
            return cur.lastrowid

    def get_trades(self, is_paper: bool = True, limit: int = 500) -> List[TradeRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trades WHERE is_paper=? ORDER BY id DESC LIMIT ?",
                (int(is_paper), limit),
            ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def count_trades_today(self, action: str, is_paper: bool = True) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) as cnt FROM trades WHERE action=? AND is_paper=? AND date(timestamp)=date('now')",
                (action, int(is_paper)),
            ).fetchone()
        return row["cnt"] if row else 0

    def total_realized_pnl(self, is_paper: bool = True) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(realized_pnl),0) as total FROM trades WHERE is_paper=?",
                (int(is_paper),),
            ).fetchone()
        return row["total"] if row else 0.0

    def win_loss_counts(self, is_paper: bool = True) -> tuple:
        with self._lock:
            row = self._conn.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END), 0) as wins,
                    COALESCE(SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END), 0) as losses
                FROM trades WHERE action='SELL' AND is_paper=?
            """, (int(is_paper),)).fetchone()
        return (row["wins"], row["losses"]) if row else (0, 0)

    @staticmethod
    def _row_to_trade(r) -> TradeRecord:
        return TradeRecord(
            id=r["id"], timestamp=r["timestamp"],
            action=r["action"], market_question=r["market_question"],
            outcome=r["outcome"], token_id=r["token_id"],
            price=r["price"], size=r["size"],
            gross_value=r["gross_value"], fees=r["fees"],
            realized_pnl=r["realized_pnl"], notes=r["notes"],
            is_paper=bool(r["is_paper"]),
        )

    # ------------------------------------------------------------------
    # Event log
    # ------------------------------------------------------------------
    def insert_event(self, level: str, message: str):
        with self._lock:
            self._conn.execute(
                "INSERT INTO event_log (timestamp, level, message) VALUES (?,?,?)",
                (_now(), level, message),
            )
            self._conn.commit()

    def get_events(self, limit: int = 500) -> List[EventLogEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM event_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            EventLogEntry(id=r["id"], timestamp=r["timestamp"],
                          level=r["level"], message=r["message"])
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Equity snapshots
    # ------------------------------------------------------------------
    def insert_equity_snapshot(self, snap: EquitySnapshot):
        with self._lock:
            self._conn.execute("""
                INSERT INTO equity_snapshots
                    (timestamp, total_exposure, cash_reserved,
                     unrealized_pnl, realized_pnl, total_pnl)
                VALUES (?,?,?,?,?,?)
            """, (
                snap.timestamp or _now(), snap.total_exposure,
                snap.cash_reserved, snap.unrealized_pnl,
                snap.realized_pnl, snap.total_pnl,
            ))
            self._conn.commit()

    # ------------------------------------------------------------------
    # Aggregates for PnL summary
    # ------------------------------------------------------------------
    def build_pnl_summary(self, is_paper: bool = True) -> PnLSummary:
        positions = self.get_positions(is_paper)
        orders = self.get_open_orders(is_paper)
        total_exposure = sum(p.cost_basis for p in positions)
        unrealized = sum(p.unrealized_pnl for p in positions)
        realized = self.total_realized_pnl(is_paper)
        wins, losses = self.win_loss_counts(is_paper)
        buys_today = self.count_trades_today("BUY", is_paper)
        sells_today = self.count_trades_today("SELL", is_paper)

        order_exposure = sum(o.remaining_size * o.price for o in orders if o.side == "BUY")

        return PnLSummary(
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            total_pnl=realized + unrealized,
            total_exposure=total_exposure,
            cash_reserved=order_exposure,
            open_positions=len(positions),
            open_orders=len(orders),
            win_count=wins,
            loss_count=losses,
            buys_today=buys_today,
            sells_today=sells_today,
        )
