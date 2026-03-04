"""
Dashboard cards widget — shows PnL summary, counts, exposure.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QWidget, QGridLayout, QLabel, QFrame, QVBoxLayout, QHBoxLayout,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from models.data_models import PnLSummary


class DashboardCard(QFrame):
    """Single metric card."""

    def __init__(self, title: str, value: str = "—", parent=None):
        super().__init__(parent)
        self.setFrameStyle(QFrame.StyledPanel)
        self.setStyleSheet("""
            DashboardCard {
                background-color: #16213e;
                border: 1px solid #2d2d44;
                border-radius: 6px;
                padding: 8px;
            }
        """)
        self.setMinimumWidth(130)
        self.setMinimumHeight(70)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)

        self._title = QLabel(title)
        self._title.setStyleSheet("color: #8888aa; font-size: 11px; font-weight: 600;")
        self._title.setAlignment(Qt.AlignLeft)

        self._value = QLabel(value)
        self._value.setStyleSheet("color: #ffffff; font-size: 18px; font-weight: bold;")
        self._value.setAlignment(Qt.AlignLeft)

        layout.addWidget(self._title)
        layout.addWidget(self._value)

    def set_value(self, text: str, color: str = "#ffffff"):
        self._value.setText(text)
        self._value.setStyleSheet(f"color: {color}; font-size: 18px; font-weight: bold;")


class DashboardWidget(QWidget):
    """Grid of dashboard metric cards."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QGridLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(0, 0, 0, 0)

        self.card_positions = DashboardCard("Open Positions")
        self.card_orders = DashboardCard("Open Orders")
        self.card_exposure = DashboardCard("Total Exposure")
        self.card_cash = DashboardCard("Cash Reserved")
        self.card_realized = DashboardCard("Realized PnL")
        self.card_unrealized = DashboardCard("Unrealized PnL")
        self.card_total_pnl = DashboardCard("Total PnL")
        self.card_win_rate = DashboardCard("Win Rate")
        self.card_buys_today = DashboardCard("Buys Today")
        self.card_sells_today = DashboardCard("Sells Today")

        cards = [
            self.card_positions, self.card_orders, self.card_exposure,
            self.card_cash, self.card_realized, self.card_unrealized,
            self.card_total_pnl, self.card_win_rate, self.card_buys_today,
            self.card_sells_today,
        ]
        for i, card in enumerate(cards):
            row = i // 5
            col = i % 5
            layout.addWidget(card, row, col)

    def update_summary(self, s: PnLSummary):
        self.card_positions.set_value(str(s.open_positions))
        self.card_orders.set_value(str(s.open_orders))
        self.card_exposure.set_value(f"${s.total_exposure:.2f}")
        self.card_cash.set_value(f"${s.cash_reserved:.2f}")

        rpnl_color = "#00e676" if s.realized_pnl >= 0 else "#ff5252"
        self.card_realized.set_value(f"${s.realized_pnl:.4f}", rpnl_color)

        upnl_color = "#00e676" if s.unrealized_pnl >= 0 else "#ff5252"
        self.card_unrealized.set_value(f"${s.unrealized_pnl:.4f}", upnl_color)

        tpnl_color = "#00e676" if s.total_pnl >= 0 else "#ff5252"
        self.card_total_pnl.set_value(f"${s.total_pnl:.4f}", tpnl_color)

        self.card_win_rate.set_value(f"{s.win_rate:.1f}%")
        self.card_buys_today.set_value(str(s.buys_today))
        self.card_sells_today.set_value(str(s.sells_today))
