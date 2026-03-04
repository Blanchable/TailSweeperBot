"""
Polymarket Tail Sweeper — application entry point.
"""
from __future__ import annotations

import sys
import os

# Ensure the project root is on the path so all imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from config import Settings, LOG_PATH_DEFAULT
from storage.database import Database
from utils.logging_utils import setup_logging, get_logger


def main():
    setup_logging(LOG_PATH_DEFAULT)
    logger = get_logger()
    logger.info("=== Polymarket Tail Sweeper starting ===")

    app = QApplication(sys.argv)
    app.setApplicationName("Polymarket Tail Sweeper")
    app.setOrganizationName("TailSweeper")

    # Load settings: DB first, fall back to defaults, overlay .env creds
    settings = Settings()
    db = Database(settings.db_path)

    persisted = db.load_settings()
    if persisted:
        settings = persisted
        logger.info("Settings loaded from database")
    else:
        logger.info("Using default settings (first run)")

    settings.load_env_credentials()
    db.save_settings(settings)

    from gui.main_window import MainWindow

    window = MainWindow(settings, db)
    window.show()

    logger.info("GUI launched — ready")
    exit_code = app.exec()

    db.close()
    logger.info("=== Polymarket Tail Sweeper exiting ===")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
