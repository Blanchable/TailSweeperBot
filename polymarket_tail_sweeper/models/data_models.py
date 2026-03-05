"""
Core data models used across the application.
Plain dataclasses — no ORM dependency.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List


class BotState(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    ERROR = "error"
    PAPER = "paper"
    LIVE = "live"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class TradeAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    CANCEL = "CANCEL"


@dataclass
class Market:
    """Represents a Polymarket event market."""
    condition_id: str
    question: str
    tokens: List[Token] = field(default_factory=list)
    active: bool = True
    closed: bool = False
    neg_risk: bool = False
    fee: float = 0.0
    volume: float = 0.0
    end_date: Optional[str] = None


@dataclass
class Token:
    """A single outcome token within a market."""
    token_id: str
    outcome: str
    price: float = 0.0


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid_size(self) -> float:
        return self.bids[0].size if self.bids else 0.0

    @property
    def best_ask_size(self) -> float:
        return self.asks[0].size if self.asks else 0.0

    @property
    def midpoint(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None


@dataclass
class Candidate:
    """A scored buy candidate."""
    market: Market
    token: Token
    book: OrderBook
    score: float = 0.0
    ask_price: float = 0.0
    ask_size: float = 0.0
    bid_price: float = 0.0
    spread: float = 0.0


@dataclass
class Position:
    id: Optional[int] = None
    token_id: str = ""
    condition_id: str = ""
    market_question: str = ""
    outcome: str = ""
    shares: float = 0.0
    avg_entry: float = 0.0
    cost_basis: float = 0.0
    current_mark: float = 0.0
    current_bid: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    next_exit_rung: int = 0
    created_at: str = ""
    updated_at: str = ""
    is_paper: bool = True


@dataclass
class OpenOrder:
    id: Optional[int] = None
    order_id: str = ""
    token_id: str = ""
    condition_id: str = ""
    market_question: str = ""
    side: str = "BUY"
    price: float = 0.0
    size: float = 0.0
    remaining_size: float = 0.0
    status: str = "OPEN"
    post_only: bool = False
    created_at: str = ""
    is_paper: bool = True
    order_tag: str = ""  # EXIT_RUNG:N, SELL_ALL, ENTRY, etc.


@dataclass
class TradeRecord:
    id: Optional[int] = None
    timestamp: str = ""
    action: str = "BUY"
    market_question: str = ""
    outcome: str = ""
    token_id: str = ""
    price: float = 0.0
    size: float = 0.0
    gross_value: float = 0.0
    fees: float = 0.0
    realized_pnl: float = 0.0
    notes: str = ""
    is_paper: bool = True


@dataclass
class EventLogEntry:
    id: Optional[int] = None
    timestamp: str = ""
    level: str = "INFO"
    message: str = ""


@dataclass
class EquitySnapshot:
    id: Optional[int] = None
    timestamp: str = ""
    total_exposure: float = 0.0
    cash_reserved: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    total_pnl: float = 0.0


@dataclass
class PnLSummary:
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_pnl: float = 0.0
    total_exposure: float = 0.0
    cash_reserved: float = 0.0
    open_positions: int = 0
    open_orders: int = 0
    win_count: int = 0
    loss_count: int = 0
    buys_today: int = 0
    sells_today: int = 0

    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        return (self.win_count / total * 100) if total > 0 else 0.0
