"""
PnL engine.
Handles lot accounting (FIFO), cost basis, realized/unrealized PnL.
"""
from __future__ import annotations

import logging
from typing import Optional

from models.data_models import Position, OrderBook
from storage.database import Database

logger = logging.getLogger("tailsweeper.pnl")


class PnLEngine:
    """Computes and updates PnL for all positions."""

    def __init__(self, db: Database):
        self._db = db

    def record_buy(
        self,
        token_id: str,
        condition_id: str,
        market_question: str,
        outcome: str,
        price: float,
        shares: float,
        is_paper: bool = True,
    ) -> Position:
        """
        Record a buy fill.  Updates average entry and cost basis (FIFO-compatible).
        """
        existing = self._db.get_position_by_token(token_id, is_paper)
        if existing and existing.shares > 0:
            total_cost = existing.cost_basis + (price * shares)
            total_shares = existing.shares + shares
            avg = total_cost / total_shares if total_shares > 0 else price
            existing.shares = total_shares
            existing.avg_entry = avg
            existing.cost_basis = total_cost
            self._db.upsert_position(existing)
            logger.info(
                "Position updated: %s +%.2f shares @ %.4f, avg=%.4f, total=%.2f",
                token_id[:12], shares, price, avg, total_shares,
            )
            return existing
        else:
            pos = Position(
                token_id=token_id,
                condition_id=condition_id,
                market_question=market_question,
                outcome=outcome,
                shares=shares,
                avg_entry=price,
                cost_basis=price * shares,
                is_paper=is_paper,
            )
            self._db.upsert_position(pos)
            logger.info(
                "New position: %s %.2f shares @ %.4f",
                token_id[:12], shares, price,
            )
            return pos

    def record_sell(
        self,
        token_id: str,
        price: float,
        shares: float,
        is_paper: bool = True,
    ) -> float:
        """
        Record a sell fill.  FIFO lot accounting.
        Returns realized PnL for this sell.
        """
        pos = self._db.get_position_by_token(token_id, is_paper)
        if not pos or pos.shares <= 0:
            logger.warning("Sell on non-existent position %s", token_id[:12])
            return 0.0

        sell_shares = min(shares, pos.shares)
        cost_per_share = pos.avg_entry
        realized = (price - cost_per_share) * sell_shares

        pos.shares -= sell_shares
        if pos.shares > 0:
            pos.cost_basis = pos.avg_entry * pos.shares
        else:
            pos.cost_basis = 0.0

        self._db.upsert_position(pos)

        if pos.shares <= 0:
            self._db.remove_empty_positions(is_paper)

        logger.info(
            "Sold %.2f of %s @ %.4f, realized PnL=%.4f, remaining=%.2f",
            sell_shares, token_id[:12], price, realized, pos.shares,
        )
        return realized

    def update_mark_prices(
        self,
        token_id: str,
        book: OrderBook,
        is_paper: bool = True,
    ):
        """Update a position's mark price and unrealized PnL from latest book."""
        pos = self._db.get_position_by_token(token_id, is_paper)
        if not pos or pos.shares <= 0:
            return

        mid = book.midpoint
        bid = book.best_bid

        mark = mid if mid is not None else (bid if bid is not None else pos.avg_entry)
        pos.current_mark = mark
        pos.current_bid = bid if bid is not None else 0.0

        market_value = pos.shares * mark
        pos.unrealized_pnl = market_value - pos.cost_basis
        if pos.cost_basis > 0:
            pos.unrealized_pnl_pct = (pos.unrealized_pnl / pos.cost_basis) * 100
        else:
            pos.unrealized_pnl_pct = 0.0

        self._db.upsert_position(pos)

    def check_exit_rungs(
        self,
        pos: Position,
        exit_multiples: list,
        exit_fractions: list,
        trigger_price: Optional[float] = None,
    ) -> list:
        """
        Check which exit rungs have been reached.
        Returns list of (rung_index, fraction, shares_to_sell) tuples.

        trigger_price: the executable price to evaluate against.
        When None, falls back to pos.current_bid (then pos.current_mark).
        Callers should pass best_bid so exits are based on executable price.
        """
        triggers = []
        if pos.avg_entry <= 0 or pos.shares <= 0:
            return triggers

        if trigger_price is not None and trigger_price > 0:
            price = trigger_price
        elif pos.current_bid > 0:
            price = pos.current_bid
        elif pos.current_mark > 0:
            price = pos.current_mark
        else:
            price = pos.avg_entry

        multiple = price / pos.avg_entry

        for i in range(pos.next_exit_rung, len(exit_multiples)):
            if multiple >= exit_multiples[i]:
                fraction = exit_fractions[i] if i < len(exit_fractions) else 0.25
                original_shares = pos.cost_basis / pos.avg_entry if pos.avg_entry > 0 else pos.shares
                shares_to_sell = original_shares * fraction
                shares_to_sell = min(shares_to_sell, pos.shares)
                if shares_to_sell > 0:
                    triggers.append((i, fraction, shares_to_sell))
            else:
                break

        return triggers

    def advance_exit_rung(self, token_id: str, new_rung: int, is_paper: bool = True):
        """Advance the next exit rung pointer for a position."""
        pos = self._db.get_position_by_token(token_id, is_paper)
        if pos:
            pos.next_exit_rung = new_rung
            self._db.upsert_position(pos)
