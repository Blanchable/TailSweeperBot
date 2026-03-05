"""
Settings tab widget.
Provides controls for all user-tunable settings and credential fields.
"""
from __future__ import annotations

import json
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox, QPushButton,
    QGroupBox, QScrollArea, QComboBox, QFileDialog, QMessageBox,
)
from PySide6.QtCore import Qt, Signal

from config import Settings


class SettingsTab(QWidget):
    """Settings editor tab."""

    settings_saved = Signal(object)  # emits Settings

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._build_ui()
        self._load_from_settings(settings)

    def _build_ui(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        container = QWidget()
        main_layout = QVBoxLayout(container)
        main_layout.setSpacing(12)
        main_layout.setContentsMargins(16, 16, 16, 16)

        # --- Mode ---
        mode_group = QGroupBox("Trading Mode")
        mode_layout = QHBoxLayout(mode_group)
        self.chk_paper = QCheckBox("Paper Mode (simulated)")
        self.chk_paper.setChecked(True)
        mode_layout.addWidget(self.chk_paper)
        mode_layout.addStretch()
        main_layout.addWidget(mode_group)

        # --- Scan Settings ---
        scan_group = QGroupBox("Scan & Entry Settings")
        scan_layout = QGridLayout(scan_group)
        scan_layout.setSpacing(8)

        row = 0
        scan_layout.addWidget(QLabel("Scan interval (sec):"), row, 0)
        self.spin_scan_interval = QSpinBox()
        self.spin_scan_interval.setRange(10, 3600)
        scan_layout.addWidget(self.spin_scan_interval, row, 1)

        row += 1
        scan_layout.addWidget(QLabel("Max entry price ($):"), row, 0)
        self.spin_max_entry = QDoubleSpinBox()
        self.spin_max_entry.setRange(0.0001, 0.10)
        self.spin_max_entry.setDecimals(4)
        self.spin_max_entry.setSingleStep(0.001)
        scan_layout.addWidget(self.spin_max_entry, row, 1)

        row += 1
        scan_layout.addWidget(QLabel("Min spread ($):"), row, 0)
        self.spin_min_spread = QDoubleSpinBox()
        self.spin_min_spread.setRange(0.0001, 0.10)
        self.spin_min_spread.setDecimals(4)
        self.spin_min_spread.setSingleStep(0.0005)
        scan_layout.addWidget(self.spin_min_spread, row, 1)

        row += 1
        scan_layout.addWidget(QLabel("Per-order USD size:"), row, 0)
        self.spin_order_size = QDoubleSpinBox()
        self.spin_order_size.setRange(0.10, 100.0)
        self.spin_order_size.setDecimals(2)
        self.spin_order_size.setSingleStep(0.5)
        scan_layout.addWidget(self.spin_order_size, row, 1)

        row += 1
        scan_layout.addWidget(QLabel("Max total exposure ($):"), row, 0)
        self.spin_max_exposure = QDoubleSpinBox()
        self.spin_max_exposure.setRange(1.0, 10000.0)
        self.spin_max_exposure.setDecimals(2)
        scan_layout.addWidget(self.spin_max_exposure, row, 1)

        row += 1
        scan_layout.addWidget(QLabel("Max positions:"), row, 0)
        self.spin_max_positions = QSpinBox()
        self.spin_max_positions.setRange(1, 500)
        scan_layout.addWidget(self.spin_max_positions, row, 1)

        row += 1
        scan_layout.addWidget(QLabel("Max new buys per cycle:"), row, 0)
        self.spin_max_buys = QSpinBox()
        self.spin_max_buys.setRange(1, 50)
        scan_layout.addWidget(self.spin_max_buys, row, 1)

        main_layout.addWidget(scan_group)

        # --- Exit Ladder ---
        exit_group = QGroupBox("Exit Ladder")
        exit_layout = QGridLayout(exit_group)
        exit_layout.setSpacing(8)

        exit_layout.addWidget(QLabel("Exit multiples (JSON list):"), 0, 0)
        self.edit_exit_multiples = QLineEdit()
        exit_layout.addWidget(self.edit_exit_multiples, 0, 1)

        exit_layout.addWidget(QLabel("Exit fractions (JSON list):"), 1, 0)
        self.edit_exit_fractions = QLineEdit()
        exit_layout.addWidget(self.edit_exit_fractions, 1, 1)

        exit_layout.addWidget(QLabel("Exit trigger mode:"), 2, 0)
        self.combo_exit_trigger = QComboBox()
        self.combo_exit_trigger.addItems(["best_bid", "midpoint"])
        exit_layout.addWidget(self.combo_exit_trigger, 2, 1)

        exit_layout.addWidget(QLabel("Exit order mode:"), 3, 0)
        self.combo_exit_order = QComboBox()
        self.combo_exit_order.addItems(["aggressive", "passive"])
        exit_layout.addWidget(self.combo_exit_order, 3, 1)

        exit_layout.addWidget(QLabel("Min exit profit buffer ($):"), 4, 0)
        self.spin_exit_buffer = QDoubleSpinBox()
        self.spin_exit_buffer.setRange(0.0, 0.10)
        self.spin_exit_buffer.setDecimals(4)
        self.spin_exit_buffer.setSingleStep(0.0001)
        exit_layout.addWidget(self.spin_exit_buffer, 4, 1)

        main_layout.addWidget(exit_group)

        # --- Filters ---
        filter_group = QGroupBox("Market Filters")
        filter_layout = QVBoxLayout(filter_group)
        self.chk_fee_free = QCheckBox("Only fee-free markets")
        self.chk_neg_risk = QCheckBox("Skip neg-risk markets")
        self.chk_post_only = QCheckBox("Use post-only for entries")
        filter_layout.addWidget(self.chk_fee_free)
        filter_layout.addWidget(self.chk_neg_risk)
        filter_layout.addWidget(self.chk_post_only)
        main_layout.addWidget(filter_group)

        # --- Order Management ---
        order_group = QGroupBox("Order Management")
        order_layout = QGridLayout(order_group)

        order_layout.addWidget(QLabel("Stale order timeout (sec):"), 0, 0)
        self.spin_stale_timeout = QSpinBox()
        self.spin_stale_timeout.setRange(60, 86400)
        order_layout.addWidget(self.spin_stale_timeout, 0, 1)

        self.chk_auto_cancel = QCheckBox("Auto-cancel open orders on stop")
        order_layout.addWidget(self.chk_auto_cancel, 1, 0, 1, 2)

        order_layout.addWidget(QLabel("Market refresh interval (sec):"), 2, 0)
        self.spin_market_refresh = QSpinBox()
        self.spin_market_refresh.setRange(60, 3600)
        order_layout.addWidget(self.spin_market_refresh, 2, 1)

        main_layout.addWidget(order_group)

        # --- Credentials ---
        cred_group = QGroupBox("Credentials (Live Mode)")
        cred_layout = QGridLayout(cred_group)
        cred_layout.setSpacing(8)

        cred_layout.addWidget(QLabel("Private key:"), 0, 0)
        self.edit_private_key = QLineEdit()
        self.edit_private_key.setEchoMode(QLineEdit.Password)
        self.edit_private_key.setPlaceholderText("0x... (from .env or paste here)")
        cred_layout.addWidget(self.edit_private_key, 0, 1)

        cred_layout.addWidget(QLabel("Funder address:"), 1, 0)
        self.edit_funder = QLineEdit()
        self.edit_funder.setPlaceholderText("0x...")
        cred_layout.addWidget(self.edit_funder, 1, 1)

        cred_layout.addWidget(QLabel("Signature type:"), 2, 0)
        self.combo_sig_type = QComboBox()
        self.combo_sig_type.addItems(["0 - EOA", "1 - POLY_GNOSIS_SAFE", "2 - POLY_PROXY"])
        cred_layout.addWidget(self.combo_sig_type, 2, 1)

        note = QLabel("Credentials are stored in memory only. Use .env for persistent storage.")
        note.setStyleSheet("color: #ffab00; font-size: 11px;")
        note.setWordWrap(True)
        cred_layout.addWidget(note, 3, 0, 1, 2)

        main_layout.addWidget(cred_group)

        # --- Paths ---
        path_group = QGroupBox("Paths")
        path_layout = QGridLayout(path_group)
        path_layout.addWidget(QLabel("Database path:"), 0, 0)
        self.edit_db_path = QLineEdit()
        self.edit_db_path.setReadOnly(True)
        path_layout.addWidget(self.edit_db_path, 0, 1)

        self.btn_export_logs = QPushButton("Export Logs...")
        path_layout.addWidget(self.btn_export_logs, 1, 0, 1, 2)
        self.btn_export_logs.clicked.connect(self._export_logs)

        main_layout.addWidget(path_group)

        main_layout.addStretch()

        scroll.setWidget(container)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(scroll)

    def _load_from_settings(self, s: Settings):
        self.chk_paper.setChecked(s.paper_mode)
        self.spin_scan_interval.setValue(s.scan_interval_sec)
        self.spin_max_entry.setValue(s.max_entry_price)
        self.spin_min_spread.setValue(s.min_spread)
        self.spin_order_size.setValue(s.per_order_usd)
        self.spin_max_exposure.setValue(s.max_total_exposure)
        self.spin_max_positions.setValue(s.max_positions)
        self.spin_max_buys.setValue(s.max_buys_per_cycle)
        self.edit_exit_multiples.setText(json.dumps(s.exit_multiples))
        self.edit_exit_fractions.setText(json.dumps(s.exit_fractions))
        idx_trigger = self.combo_exit_trigger.findText(s.exit_trigger_mode)
        self.combo_exit_trigger.setCurrentIndex(max(0, idx_trigger))
        idx_order = self.combo_exit_order.findText(s.exit_order_mode)
        self.combo_exit_order.setCurrentIndex(max(0, idx_order))
        self.spin_exit_buffer.setValue(s.min_exit_profit_buffer)
        self.chk_fee_free.setChecked(s.only_fee_free)
        self.chk_neg_risk.setChecked(s.skip_neg_risk)
        self.chk_post_only.setChecked(s.use_post_only)
        self.spin_stale_timeout.setValue(s.stale_order_timeout_sec)
        self.chk_auto_cancel.setChecked(s.auto_cancel_on_stop)
        self.spin_market_refresh.setValue(s.market_refresh_interval_sec)
        self.edit_private_key.setText(s.private_key)
        self.edit_funder.setText(s.funder_address)
        self.combo_sig_type.setCurrentIndex(s.signature_type)
        self.edit_db_path.setText(s.db_path)

    def collect_settings(self) -> Settings:
        """Read all GUI values into a Settings object."""
        s = Settings()
        s.paper_mode = self.chk_paper.isChecked()
        s.scan_interval_sec = self.spin_scan_interval.value()
        s.max_entry_price = self.spin_max_entry.value()
        s.min_spread = self.spin_min_spread.value()
        s.per_order_usd = self.spin_order_size.value()
        s.max_total_exposure = self.spin_max_exposure.value()
        s.max_positions = self.spin_max_positions.value()
        s.max_buys_per_cycle = self.spin_max_buys.value()

        try:
            s.exit_multiples = json.loads(self.edit_exit_multiples.text())
        except (json.JSONDecodeError, TypeError):
            s.exit_multiples = [3.0, 5.0, 10.0]
        try:
            s.exit_fractions = json.loads(self.edit_exit_fractions.text())
        except (json.JSONDecodeError, TypeError):
            s.exit_fractions = [0.25, 0.25, 0.25]

        s.exit_trigger_mode = self.combo_exit_trigger.currentText()
        s.exit_order_mode = self.combo_exit_order.currentText()
        s.min_exit_profit_buffer = self.spin_exit_buffer.value()
        s.only_fee_free = self.chk_fee_free.isChecked()
        s.skip_neg_risk = self.chk_neg_risk.isChecked()
        s.use_post_only = self.chk_post_only.isChecked()
        s.stale_order_timeout_sec = self.spin_stale_timeout.value()
        s.auto_cancel_on_stop = self.chk_auto_cancel.isChecked()
        s.market_refresh_interval_sec = self.spin_market_refresh.value()
        s.private_key = self.edit_private_key.text().strip()
        s.funder_address = self.edit_funder.text().strip()
        s.signature_type = self.combo_sig_type.currentIndex()
        s.db_path = self.edit_db_path.text()

        return s

    def _export_logs(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Logs", "tailsweeper_logs.txt",
            "Text Files (*.txt);;All Files (*)"
        )
        if path:
            try:
                import shutil
                from config import LOG_PATH_DEFAULT
                shutil.copy2(LOG_PATH_DEFAULT, path)
                QMessageBox.information(self, "Export", f"Logs exported to {path}")
            except Exception as exc:
                QMessageBox.warning(self, "Error", f"Failed to export: {exc}")
