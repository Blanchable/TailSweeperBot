"""
Main application window.
Assembles the toolbar, status bar, dashboard, tabs, and manages the bot worker.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QTabWidget, QStatusBar, QMessageBox, QFrame,
)
from PySide6.QtCore import Qt, Slot, QTimer
from PySide6.QtGui import QIcon, QCloseEvent

from config import Settings
from storage.database import Database
from models.data_models import BotState, PnLSummary, Position, OpenOrder, TradeRecord
from bot.bot_worker import BotWorker
from gui.dashboard import DashboardWidget
from gui.tables import PositionsTable, OrdersTable, TradesTable, EventLogTable
from gui.settings_tab import SettingsTab
from gui.styles import DARK_STYLESHEET
from utils.logging_utils import register_gui_log_callback, unregister_gui_log_callback

logger = logging.getLogger("tailsweeper.gui")


class StatusIndicator(QLabel):
    """Colored status dot + text."""

    COLORS = {
        "green": "#00e676",
        "red": "#ff5252",
        "yellow": "#ffab00",
        "blue": "#00d4ff",
        "gray": "#666688",
    }

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self._label = label
        self.set_status("—", "gray")

    def set_status(self, text: str, color: str = "gray"):
        c = self.COLORS.get(color, color)
        self.setText(f'<span style="color:{c};">&#9679;</span> {self._label}: {text}')


class MainWindow(QMainWindow):
    """Application main window."""

    def __init__(self, settings: Settings, db: Database):
        super().__init__()
        self._settings = settings
        self._db = db
        self._worker: BotWorker | None = None

        self.setWindowTitle("Polymarket Tail Sweeper")
        self.setMinimumSize(1200, 800)
        self.resize(1400, 900)

        self.setStyleSheet(DARK_STYLESHEET)

        self._build_ui()
        self._connect_signals()
        self._load_persisted_state()

        register_gui_log_callback(self._on_log_message)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._periodic_refresh)
        self._refresh_timer.start(5000)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(8)
        root.setContentsMargins(12, 8, 12, 8)

        # --- Top toolbar ---
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self.btn_start = QPushButton("Start Bot")
        self.btn_start.setObjectName("startBtn")
        self.btn_stop = QPushButton("Stop Bot")
        self.btn_stop.setObjectName("stopBtn")
        self.btn_stop.setEnabled(False)
        self.btn_save = QPushButton("Save Settings")
        self.btn_reload = QPushButton("Reload Markets")
        self.btn_reload.setEnabled(False)
        self.btn_cancel_all = QPushButton("Cancel All Orders")
        self.btn_sell_all = QPushButton("Sell All at Market")
        self.btn_sell_all.setObjectName("sellAllBtn")
        self.btn_sell_all.setStyleSheet("""
            QPushButton#sellAllBtn {
                background-color: #6e3a0a;
                border-color: #8a4a1a;
                color: #ffffff;
                font-weight: bold;
            }
            QPushButton#sellAllBtn:hover {
                background-color: #8a4a1a;
            }
        """)
        self.btn_sync = QPushButton("Sync Portfolio")
        self.btn_kill = QPushButton("KILL SWITCH")
        self.btn_kill.setObjectName("killBtn")
        self.btn_kill.setEnabled(False)

        for btn in [self.btn_start, self.btn_stop, self.btn_save,
                     self.btn_reload, self.btn_cancel_all,
                     self.btn_sell_all, self.btn_sync, self.btn_kill]:
            toolbar.addWidget(btn)
        toolbar.addStretch()

        root.addLayout(toolbar)

        # --- Status indicators ---
        status_row = QHBoxLayout()
        status_row.setSpacing(24)

        self.ind_bot = StatusIndicator("Bot")
        self.ind_geo = StatusIndicator("Geo")
        self.ind_scan = StatusIndicator("Last Scan")
        self.ind_order = StatusIndicator("Last Order")
        self.ind_api = StatusIndicator("API")
        self.ind_wallet = StatusIndicator("Wallet")

        for ind in [self.ind_bot, self.ind_geo, self.ind_scan,
                     self.ind_order, self.ind_api, self.ind_wallet]:
            status_row.addWidget(ind)
        status_row.addStretch()

        root.addLayout(status_row)

        # --- Dashboard ---
        self.dashboard = DashboardWidget()
        root.addWidget(self.dashboard)

        # --- Tabs ---
        self.tabs = QTabWidget()

        self.positions_table = PositionsTable()
        self.orders_table = OrdersTable()
        self.trades_table = TradesTable()
        self.event_log_table = EventLogTable()
        self.settings_tab = SettingsTab(self._settings)

        self.tabs.addTab(self.positions_table, "Active Positions")
        self.tabs.addTab(self.orders_table, "Open Orders")
        self.tabs.addTab(self.trades_table, "Trade Log")
        self.tabs.addTab(self.event_log_table, "Event Log")
        self.tabs.addTab(self.settings_tab, "Settings")

        root.addWidget(self.tabs, stretch=1)

        # --- Status bar ---
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Ready — Paper mode")

    def _connect_signals(self):
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_save.clicked.connect(self._on_save_settings)
        self.btn_reload.clicked.connect(self._on_reload_markets)
        self.btn_cancel_all.clicked.connect(self._on_cancel_all)
        self.btn_sell_all.clicked.connect(self._on_sell_all_market)
        self.btn_sync.clicked.connect(self._on_sync_portfolio)
        self.btn_kill.clicked.connect(self._on_kill_switch)
        self.settings_tab.settings_saved.connect(self._on_settings_changed)

    @staticmethod
    def _mask_address(addr: str) -> str:
        if not addr or len(addr) < 10:
            return ""
        return f"{addr[:6]}...{addr[-4:]}"

    def _refresh_wallet_indicator(self):
        has_key = bool(self._settings.private_key)
        has_funder = bool(self._settings.funder_address)

        if has_key and has_funder:
            masked = self._mask_address(self._settings.funder_address)
            self.ind_wallet.set_status(f"Loaded ({masked})", "green")
            logger.info("Live credentials loaded: private key and funder address present")
        elif has_key or has_funder:
            missing = "funder address" if not has_funder else "private key"
            self.ind_wallet.set_status("Incomplete", "yellow")
            logger.info("Live credentials incomplete: missing %s", missing)
        else:
            self.ind_wallet.set_status("Not set", "gray")
            logger.info("Live credentials not set")

    # ------------------------------------------------------------------
    # Centralized DB refresh
    # ------------------------------------------------------------------
    def _refresh_from_db(self):
        """Reload all tables and dashboard from the current DB state."""
        is_paper = self._settings.paper_mode
        try:
            self.positions_table.load_data(self._db.get_positions(is_paper))
            self.orders_table.load_data(self._db.get_open_orders(is_paper))
            self.trades_table.load_data(self._db.get_trades(is_paper, 200))
            summary = self._db.build_pnl_summary(is_paper)
            self.dashboard.update_summary(summary)
        except Exception:
            pass

    def _load_persisted_state(self):
        """Load data from DB on startup."""
        self._refresh_from_db()

        events = self._db.get_events(limit=200)
        self.event_log_table.load_data(events)

        self.ind_bot.set_status("Stopped", "gray")
        self._refresh_wallet_indicator()
        mode_text = "Paper" if self._settings.paper_mode else "Live"
        self._status_bar.showMessage(f"Ready — {mode_text} mode")

    # ------------------------------------------------------------------
    # Bot lifecycle
    # ------------------------------------------------------------------
    @Slot()
    def _on_start(self):
        if self._worker and self._worker.isRunning():
            return

        self._settings = self.settings_tab.collect_settings()

        if not self._settings.paper_mode:
            errors = self._settings.validate_live_mode()
            if errors:
                QMessageBox.warning(
                    self, "Live Mode Validation",
                    "Cannot start live mode:\n\n" + "\n".join(errors),
                )
                return
            confirm = QMessageBox.question(
                self, "Live Mode",
                "You are about to start LIVE trading with real funds.\n\n"
                "Are you sure?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return

        self._worker = BotWorker(self._settings, self._db)
        self._worker.state_changed.connect(self._on_state_changed)
        self._worker.pnl_updated.connect(self._on_pnl_updated)
        self._worker.positions_updated.connect(self._on_positions_updated)
        self._worker.orders_updated.connect(self._on_orders_updated)
        self._worker.trades_updated.connect(self._on_trades_updated)
        self._worker.last_scan_time.connect(self._on_last_scan)
        self._worker.last_order_time.connect(self._on_last_order)
        self._worker.geoblock_status.connect(self._on_geoblock)
        self._worker.api_status.connect(self._on_api_status)
        self._worker.error_signal.connect(self._on_error)
        self._worker.finished.connect(self._on_worker_finished)

        self._worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_kill.setEnabled(True)
        self.btn_reload.setEnabled(True)

    @Slot()
    def _on_stop(self):
        if self._worker:
            self._worker.request_stop()
            self.ind_bot.set_status("Stopping...", "yellow")
            self._status_bar.showMessage("Stopping bot...")

    @Slot()
    def _on_kill_switch(self):
        confirm = QMessageBox.warning(
            self, "Kill Switch",
            "This will immediately cancel ALL open orders and stop the bot.\n\nProceed?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm == QMessageBox.Yes and self._worker:
            self._worker.kill_switch()
            self.ind_bot.set_status("KILLED", "red")

    @Slot()
    def _on_reload_markets(self):
        if self._worker:
            self._worker.reload_markets()
            self._status_bar.showMessage("Markets cache cleared")

    @Slot()
    def _on_cancel_all(self):
        if self._worker and self._worker.isRunning():
            self._worker.cancel_all_orders()
        else:
            self._db.cancel_all_open_orders(self._settings.paper_mode)
        self._refresh_from_db()
        self._status_bar.showMessage("All open orders cancelled")

    @Slot()
    def _on_sell_all_market(self):
        """Emergency liquidation of all held positions at market price."""
        positions = self._db.get_positions(self._settings.paper_mode)
        if not positions:
            QMessageBox.information(self, "Sell All", "No open positions to liquidate.")
            return

        confirm = QMessageBox.warning(
            self, "Sell All at Market",
            f"This will attempt to liquidate ALL {len(positions)} held positions "
            f"at the current best executable bid.\n\n"
            f"Mode: {'PAPER' if self._settings.paper_mode else 'LIVE'}\n\n"
            f"Proceed?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        self._status_bar.showMessage("Selling all positions at market...")
        logger.warning("User triggered Sell All at Market")

        if self._worker and self._worker.isRunning():
            self._worker.sell_all_at_market()
        else:
            temp_worker = BotWorker(self._settings, self._db)
            if not self._settings.paper_mode:
                if not temp_worker._init_live_trading():
                    QMessageBox.warning(self, "Error",
                                        "Failed to initialize live adapter for sell-all.")
                    return
            temp_worker._running = True
            temp_worker.sell_all_at_market()

        self._refresh_from_db()
        self._status_bar.showMessage("Sell-all complete — check positions and trades")

    @Slot()
    def _on_sync_portfolio(self):
        """Run live account sync to reconcile DB against actual wallet/exchange state."""
        if self._settings.paper_mode:
            QMessageBox.information(self, "Sync", "Sync is only available in live mode.")
            return

        errors = self._settings.validate_live_mode()
        if errors:
            QMessageBox.warning(self, "Sync", "Cannot sync:\n\n" + "\n".join(errors))
            return

        self._status_bar.showMessage("Syncing portfolio with exchange...")
        logger.info("User triggered Sync Portfolio")

        if self._worker and self._worker.isRunning():
            self._worker._sync_live_account_state()
        else:
            temp_worker = BotWorker(self._settings, self._db)
            if not temp_worker._init_live_trading():
                QMessageBox.warning(self, "Error",
                                    "Failed to initialize live adapter for sync.")
                return
            temp_worker._running = True
            temp_worker._sync_live_account_state()

        self._refresh_from_db()
        self._status_bar.showMessage("Portfolio sync complete")

    @Slot()
    def _on_save_settings(self):
        self._settings = self.settings_tab.collect_settings()
        self._db.save_settings(self._settings)
        if self._worker and self._worker.isRunning():
            self._worker.update_settings(self._settings)
        self._refresh_wallet_indicator()
        self._status_bar.showMessage("Settings saved")
        logger.info("Settings saved to database")

    @Slot(object)
    def _on_settings_changed(self, settings):
        self._settings = settings

    # ------------------------------------------------------------------
    # Worker signal handlers
    # ------------------------------------------------------------------
    @Slot(str)
    def _on_state_changed(self, state: str):
        colors = {
            BotState.STOPPED: ("Stopped", "gray"),
            BotState.RUNNING: ("Running", "green"),
            BotState.ERROR: ("Error", "red"),
            BotState.PAPER: ("Paper Mode", "blue"),
            BotState.LIVE: ("LIVE", "yellow"),
        }
        text, color = colors.get(state, ("Unknown", "gray"))
        self.ind_bot.set_status(text, color)
        self._status_bar.showMessage(f"Bot: {text}")

    @Slot()
    def _on_worker_finished(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_kill.setEnabled(False)
        self.btn_reload.setEnabled(False)
        self.ind_bot.set_status("Stopped", "gray")
        self._refresh_from_db()
        self._status_bar.showMessage("Bot stopped")

    @Slot(object)
    def _on_pnl_updated(self, summary: PnLSummary):
        self.dashboard.update_summary(summary)

    @Slot(list)
    def _on_positions_updated(self, positions: list):
        self.positions_table.load_data(positions)

    @Slot(list)
    def _on_orders_updated(self, orders: list):
        self.orders_table.load_data(orders)

    @Slot(list)
    def _on_trades_updated(self, trades: list):
        self.trades_table.load_data(trades)

    @Slot(str)
    def _on_last_scan(self, ts: str):
        self.ind_scan.set_status(ts, "green")

    @Slot(str)
    def _on_last_order(self, ts: str):
        self.ind_order.set_status(ts, "green")

    @Slot(bool)
    def _on_geoblock(self, blocked: bool):
        if blocked:
            self.ind_geo.set_status("BLOCKED", "red")
        else:
            self.ind_geo.set_status("OK", "green")

    @Slot(bool)
    def _on_api_status(self, ok: bool):
        if ok:
            self.ind_api.set_status("Connected", "green")
        else:
            self.ind_api.set_status("Unreachable", "red")

    @Slot(str)
    def _on_error(self, msg: str):
        self._status_bar.showMessage(f"Error: {msg}")

    def _on_log_message(self, timestamp: str, level: str, message: str):
        self.event_log_table.add_entry(timestamp, level, message)
        self._db.insert_event(level, message)

    def _periodic_refresh(self):
        """Refresh all tables and dashboard when the bot is idle."""
        if self._worker and self._worker.isRunning():
            return
        self._refresh_from_db()

    # ------------------------------------------------------------------
    # Window close
    # ------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent):
        if self._worker and self._worker.isRunning():
            self._worker.request_stop()
            self._worker.wait(5000)
        self._db.save_settings(self._settings)
        unregister_gui_log_callback(self._on_log_message)
        self._refresh_timer.stop()
        event.accept()
