"""Dark theme stylesheet for the application."""

DARK_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #1a1a2e;
    color: #e0e0e0;
    font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
}

QTabWidget::pane {
    border: 1px solid #2d2d44;
    background-color: #16213e;
    border-radius: 4px;
}

QTabBar::tab {
    background-color: #1a1a2e;
    color: #8888aa;
    padding: 8px 20px;
    margin-right: 2px;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    border: 1px solid #2d2d44;
    border-bottom: none;
}

QTabBar::tab:selected {
    background-color: #16213e;
    color: #00d4ff;
    border-bottom: 2px solid #00d4ff;
}

QTabBar::tab:hover {
    color: #ffffff;
    background-color: #1f2b47;
}

QPushButton {
    background-color: #2d2d44;
    color: #e0e0e0;
    border: 1px solid #3d3d5c;
    border-radius: 4px;
    padding: 6px 16px;
    font-weight: 500;
    min-height: 28px;
}

QPushButton:hover {
    background-color: #3d3d5c;
    border-color: #00d4ff;
}

QPushButton:pressed {
    background-color: #00d4ff;
    color: #1a1a2e;
}

QPushButton:disabled {
    background-color: #1a1a2e;
    color: #555566;
    border-color: #2d2d44;
}

QPushButton#startBtn {
    background-color: #0a6e3a;
    border-color: #0d8a4a;
    color: #ffffff;
}
QPushButton#startBtn:hover {
    background-color: #0d8a4a;
}

QPushButton#stopBtn {
    background-color: #6e0a0a;
    border-color: #8a1a1a;
    color: #ffffff;
}
QPushButton#stopBtn:hover {
    background-color: #8a1a1a;
}

QPushButton#killBtn {
    background-color: #8b0000;
    border-color: #aa0000;
    color: #ffffff;
    font-weight: bold;
}
QPushButton#killBtn:hover {
    background-color: #aa0000;
}

QTableWidget {
    background-color: #0f1629;
    alternate-background-color: #141b30;
    gridline-color: #2d2d44;
    border: 1px solid #2d2d44;
    border-radius: 4px;
    selection-background-color: #1e3a5f;
    selection-color: #ffffff;
}

QTableWidget::item {
    padding: 4px 8px;
    border-bottom: 1px solid #1a1a2e;
}

QHeaderView::section {
    background-color: #1a1a2e;
    color: #8888aa;
    padding: 6px 8px;
    border: none;
    border-bottom: 2px solid #2d2d44;
    font-weight: 600;
    font-size: 12px;
    text-transform: uppercase;
}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background-color: #0f1629;
    color: #e0e0e0;
    border: 1px solid #2d2d44;
    border-radius: 4px;
    padding: 4px 8px;
    min-height: 24px;
}

QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border-color: #00d4ff;
}

QCheckBox {
    spacing: 8px;
    color: #e0e0e0;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #3d3d5c;
    border-radius: 3px;
    background-color: #0f1629;
}

QCheckBox::indicator:checked {
    background-color: #00d4ff;
    border-color: #00d4ff;
}

QLabel {
    color: #e0e0e0;
}

QLabel#sectionLabel {
    font-size: 14px;
    font-weight: bold;
    color: #00d4ff;
    padding: 4px 0;
}

QGroupBox {
    border: 1px solid #2d2d44;
    border-radius: 4px;
    margin-top: 12px;
    padding-top: 16px;
    font-weight: bold;
    color: #8888aa;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
}

QScrollBar:vertical {
    background-color: #0f1629;
    width: 10px;
    border: none;
}

QScrollBar::handle:vertical {
    background-color: #2d2d44;
    min-height: 20px;
    border-radius: 5px;
}

QScrollBar::handle:vertical:hover {
    background-color: #3d3d5c;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}

QTextEdit {
    background-color: #0f1629;
    color: #e0e0e0;
    border: 1px solid #2d2d44;
    border-radius: 4px;
    font-family: "Cascadia Mono", "Consolas", "Courier New", monospace;
    font-size: 12px;
}

QStatusBar {
    background-color: #0f1629;
    color: #8888aa;
    border-top: 1px solid #2d2d44;
}
"""
