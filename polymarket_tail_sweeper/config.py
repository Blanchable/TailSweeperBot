"""
Application configuration management.
Loads settings from .env, provides defaults, and exposes a typed Settings object.
"""
from __future__ import annotations

import os
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
ENV_PATH = APP_DIR / ".env"
load_dotenv(ENV_PATH)

DB_PATH_DEFAULT = str(APP_DIR / "tailsweeper.db")
LOG_PATH_DEFAULT = str(APP_DIR / "tailsweeper.log")

POLYMARKET_CLOB_BASE = "https://clob.polymarket.com"
POLYMARKET_GAMMA_BASE = "https://gamma-api.polymarket.com"
POLYMARKET_GEOBLOCK_URL = "https://clob.polymarket.com/auth/nonce"


@dataclass
class Settings:
    """All user-tunable knobs live here."""

    # mode
    paper_mode: bool = True

    # scan
    scan_interval_sec: int = 60
    max_entry_price: float = 0.035
    min_spread: float = 0.001
    per_order_usd: float = 1.0
    max_total_exposure: float = 50.0
    max_positions: int = 50
    max_buys_per_cycle: int = 3

    # exit ladder
    exit_multiples: List[float] = field(default_factory=lambda: [3.0, 5.0, 10.0])
    exit_fractions: List[float] = field(default_factory=lambda: [0.25, 0.25, 0.25])

    # exit safety
    exit_trigger_mode: str = "best_bid"
    exit_order_mode: str = "aggressive"
    min_exit_profit_buffer: float = 0.0005

    # filters
    only_fee_free: bool = False
    skip_neg_risk: bool = True
    use_post_only: bool = True

    # order management
    stale_order_timeout_sec: int = 600
    auto_cancel_on_stop: bool = True

    # market refresh
    market_refresh_interval_sec: int = 300

    # live account sync
    live_sync_on_start: bool = True
    live_sync_when_idle: bool = True

    # strategy / liquidity filters
    min_best_bid_size: float = 25.0
    min_best_ask_size: float = 25.0
    max_spread_ratio: float = 0.50

    # market memory
    recent_winner_boost_hours: int = 12
    same_market_exposure_cap: int = 2

    # inventory management
    max_hold_minutes: int = 180
    no_progress_minutes: int = 20
    breakeven_unwind_minutes: int = 45
    allow_small_forced_unwind_loss: bool = False

    # order size
    min_marketable_order_usd: float = 1.0

    # scan / farm mode
    scan_burst_duration_sec: int = 90
    scan_burst_max_new_orders: int = 6
    farm_phase_max_minutes: int = 25
    rescan_every_minutes: int = 45
    rescan_if_farm_size_below: int = 6
    rescan_fill_window_minutes: int = 10
    rescan_if_fill_rate_below: int = 1
    farm_token_ttl_minutes: int = 240
    farm_prune_after_bad_cycles: int = 10
    farm_boost_hours: int = 12
    farm_score_boost: float = 1000.0

    # entry maintenance
    entry_reprice_enabled: bool = True
    entry_reprice_interval_sec: int = 15
    entry_max_reprices: int = 6

    # credentials (live mode)
    private_key: str = ""
    funder_address: str = ""
    signature_type: int = 0

    # paths
    db_path: str = DB_PATH_DEFAULT
    log_path: str = LOG_PATH_DEFAULT

    def to_dict(self) -> dict:
        d = asdict(self)
        d["exit_multiples"] = json.dumps(d["exit_multiples"])
        d["exit_fractions"] = json.dumps(d["exit_fractions"])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        if "exit_multiples" in d and isinstance(d["exit_multiples"], str):
            d["exit_multiples"] = json.loads(d["exit_multiples"])
        if "exit_fractions" in d and isinstance(d["exit_fractions"], str):
            d["exit_fractions"] = json.loads(d["exit_fractions"])
        bool_fields = [
            "paper_mode", "only_fee_free", "skip_neg_risk",
            "use_post_only", "auto_cancel_on_stop",
            "live_sync_on_start", "live_sync_when_idle",
            "allow_small_forced_unwind_loss", "entry_reprice_enabled",
        ]
        int_fields = [
            "scan_interval_sec", "max_positions", "max_buys_per_cycle",
            "stale_order_timeout_sec", "signature_type",
            "market_refresh_interval_sec",
            "recent_winner_boost_hours", "same_market_exposure_cap",
            "max_hold_minutes", "no_progress_minutes",
            "breakeven_unwind_minutes",
            "scan_burst_duration_sec", "scan_burst_max_new_orders",
            "farm_phase_max_minutes", "rescan_every_minutes",
            "rescan_if_farm_size_below", "rescan_fill_window_minutes",
            "rescan_if_fill_rate_below", "farm_token_ttl_minutes",
            "farm_prune_after_bad_cycles", "farm_boost_hours",
            "entry_reprice_interval_sec", "entry_max_reprices",
        ]
        float_fields = [
            "max_entry_price", "min_spread", "per_order_usd",
            "max_total_exposure", "min_exit_profit_buffer",
            "min_best_bid_size", "min_best_ask_size", "max_spread_ratio",
            "min_marketable_order_usd", "farm_score_boost",
        ]
        for k in bool_fields:
            if k in d and not isinstance(d[k], bool):
                d[k] = str(d[k]).lower() in ("1", "true", "yes")
        for k in int_fields:
            if k in d and not isinstance(d[k], int):
                try:
                    d[k] = int(d[k])
                except (ValueError, TypeError):
                    pass
        for k in float_fields:
            if k in d and not isinstance(d[k], float):
                try:
                    d[k] = float(d[k])
                except (ValueError, TypeError):
                    pass
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**filtered)

    def load_env_credentials(self):
        """Override credential fields from environment variables if set."""
        self.private_key = os.getenv("POLYMARKET_PRIVATE_KEY", self.private_key)
        self.funder_address = os.getenv("POLYMARKET_FUNDER_ADDRESS", self.funder_address)
        sig = os.getenv("POLYMARKET_SIGNATURE_TYPE", "")
        if sig.isdigit():
            self.signature_type = int(sig)

    def validate_live_mode(self) -> List[str]:
        """Return list of validation errors for live mode. Empty = OK."""
        errors = []
        if not self.private_key:
            errors.append("Private key is required for live trading")
        if not self.funder_address:
            errors.append("Funder address is required for live trading")
        if self.max_total_exposure <= 0:
            errors.append("Max total exposure must be > 0")
        return errors
