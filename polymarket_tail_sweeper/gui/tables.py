"""
Table widgets for positions, orders, trades, and event log.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from models.data_models import Position, OpenOrder, TradeRecord, EventLogEntry
from typing import List


class _BaseTable(QTableWidget):
    """Common table setup."""

    def __init__(self, headers: list, parent=None):
        super().__init__(0, len(headers), parent)
        self.setHorizontalHeaderLabels(headers)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.setSortingEnabled(False)
        self.verticalHeader().setDefaultSectionSize(28)

    def _set_row(self, row: int, values: list, colors: list | None = None):
        for col, val in enumerate(values):
            item = QTableWidgetItem(str(val))
            item.setTextAlignment(Qt.AlignCenter)
            if colors and col < len(colors) and colors[col]:
                item.setForeground(QColor(colors[col]))
            self.setItem(row, col, item)


class PositionsTable(_BaseTable):
    HEADERS = [
        "Market", "Outcome", "Token ID", "Shares", "Avg Entry",
        "Mark", "Best Bid", "UPnL $", "UPnL %", "Next Exit", "Created",
    ]

    def __init__(self, parent=None):
        super().__init__(self.HEADERS, parent)
        self.setColumnWidth(0, 250)
        self.setColumnWidth(2, 120)

    def load_data(self, positions: List[Position]):
        self.setRowCount(0)
        self.setRowCount(len(positions))
        for row, p in enumerate(positions):
            pnl_color = "#00e676" if p.unrealized_pnl >= 0 else "#ff5252"
            self._set_row(row, [
                p.market_question[:60],
                p.outcome,
                p.token_id[:16] + "...",
                f"{p.shares:.2f}",
                f"${p.avg_entry:.4f}",
                f"${p.current_mark:.4f}",
                f"${p.current_bid:.4f}",
                f"${p.unrealized_pnl:.4f}",
                f"{p.unrealized_pnl_pct:.1f}%",
                str(p.next_exit_rung),
                p.created_at or "",
            ], colors=[None, None, None, None, None, None, None, pnl_color, pnl_color, None, None])


class OrdersTable(_BaseTable):
    HEADERS = [
        "Order ID", "Market", "Side", "Price", "Size",
        "Remaining", "Status", "Post-Only", "Created",
    ]

    def __init__(self, parent=None):
        super().__init__(self.HEADERS, parent)
        self.setColumnWidth(1, 250)

    def load_data(self, orders: List[OpenOrder]):
        self.setRowCount(0)
        self.setRowCount(len(orders))
        for row, o in enumerate(orders):
            side_color = "#00e676" if o.side == "BUY" else "#ff5252"
            self._set_row(row, [
                o.order_id,
                o.market_question[:60],
                o.side,
                f"${o.price:.4f}",
                f"{o.size:.2f}",
                f"{o.remaining_size:.2f}",
                o.status,
                "Yes" if o.post_only else "No",
                o.created_at or "",
            ], colors=[None, None, side_color, None, None, None, None, None, None])


class TradesTable(_BaseTable):
    HEADERS = [
        "Time", "Action", "Market", "Outcome", "Price",
        "Size", "Gross $", "Fees", "Realized PnL", "Notes",
    ]

    def __init__(self, parent=None):
        super().__init__(self.HEADERS, parent)
        self.setColumnWidth(2, 220)

    def load_data(self, trades: List[TradeRecord]):
        self.setRowCount(0)
        self.setRowCount(len(trades))
        for row, t in enumerate(trades):
            action_color = {
                "BUY": "#00e676", "SELL": "#ff5252", "CANCEL": "#ffab00",
            }.get(t.action, "#e0e0e0")
            pnl_color = "#00e676" if t.realized_pnl >= 0 else "#ff5252" if t.realized_pnl < 0 else None
            self._set_row(row, [
                t.timestamp,
                t.action,
                t.market_question[:50],
                t.outcome,
                f"${t.price:.4f}",
                f"{t.size:.2f}",
                f"${t.gross_value:.4f}",
                f"${t.fees:.4f}",
                f"${t.realized_pnl:.4f}" if t.action == "SELL" else "",
                t.notes[:40],
            ], colors=[None, action_color, None, None, None, None, None, None, pnl_color, None])


class EventLogTable(_BaseTable):
    HEADERS = ["Time", "Level", "Message"]

    def __init__(self, parent=None):
        super().__init__(self.HEADERS, parent)
        self.setColumnWidth(2, 600)

    def add_entry(self, timestamp: str, level: str, message: str):
        row = self.rowCount()
        self.insertRow(row)
        level_color = {
            "ERROR": "#ff5252", "WARNING": "#ffab00", "INFO": "#00d4ff",
            "DEBUG": "#666688",
        }.get(level, "#e0e0e0")
        self._set_row(row, [timestamp, level, message],
                       colors=[None, level_color, None])
        if self.rowCount() > 2000:
            self.removeRow(0)
        self.scrollToBottom()

    def load_data(self, entries: List[EventLogEntry]):
        self.setRowCount(0)
        for e in reversed(entries):
            self.add_entry(e.timestamp, e.level, e.message)
