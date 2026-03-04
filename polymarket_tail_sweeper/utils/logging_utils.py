"""
Centralized logging configuration.
Provides a logger that writes to file and emits signals the GUI can consume.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, List

_gui_callbacks: List[Callable[[str, str, str], None]] = []


def register_gui_log_callback(cb: Callable[[str, str, str], None]):
    """Register a callback(timestamp, level, message) for GUI event log."""
    _gui_callbacks.append(cb)


def unregister_gui_log_callback(cb: Callable[[str, str, str], None]):
    _gui_callbacks.discard(cb) if hasattr(_gui_callbacks, "discard") else None
    try:
        _gui_callbacks.remove(cb)
    except ValueError:
        pass


class GUIHandler(logging.Handler):
    """Logging handler that forwards records to registered GUI callbacks."""

    def emit(self, record: logging.LogRecord):
        try:
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            msg = self.format(record)
            level = record.levelname
            for cb in _gui_callbacks:
                try:
                    cb(ts, level, msg)
                except Exception:
                    pass
        except Exception:
            pass


def setup_logging(log_path: str, level: int = logging.DEBUG) -> logging.Logger:
    """Set up the application logger with file + GUI handlers."""
    logger = logging.getLogger("tailsweeper")
    if logger.handlers:
        return logger
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    gh = GUIHandler()
    gh.setLevel(logging.DEBUG)
    gh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(gh)

    return logger


def get_logger(name: str = "tailsweeper") -> logging.Logger:
    return logging.getLogger(name)
