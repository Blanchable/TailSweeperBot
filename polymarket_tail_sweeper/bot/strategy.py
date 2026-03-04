"""
Strategy: filters markets, scores candidates, decides entries and exits.
"""
from __future__ import annotations

import logging
from typing import List

from config import Settings
from models.data_models import Market, Token, OrderBook, Candidate

logger = logging.getLogger("tailsweeper.strategy")


class Strategy:
    """Tail-sweep entry/exit strategy logic."""

    def __init__(self, settings: Settings):
        self.s = settings

    def filter_markets(self, markets: List[Market]) -> List[Market]:
        """Apply market-level filters. Returns passing markets."""
        out = []
        for m in markets:
            if not m.active or m.closed:
                continue
            if self.s.only_fee_free and m.fee != 0:
                logger.debug("Reject (fee): %s", m.question[:60])
                continue
            if self.s.skip_neg_risk and m.neg_risk:
                logger.debug("Reject (neg-risk): %s", m.question[:60])
                continue
            if not m.tokens:
                logger.debug("Reject (no tokens): %s", m.question[:60])
                continue
            out.append(m)
        logger.info("Market filter: %d -> %d markets passed", len(markets), len(out))
        return out

    def find_candidates(
        self,
        market: Market,
        books: dict,
        held_tokens: set,
        open_buy_tokens: set,
    ) -> List[Candidate]:
        """
        For a given market, find tokens that qualify as buy candidates.
        """
        candidates = []
        for token in market.tokens:
            book: OrderBook = books.get(token.token_id)
            if not book:
                continue

            if token.token_id in held_tokens:
                logger.debug("Skip %s (already held)", token.token_id[:12])
                continue
            if token.token_id in open_buy_tokens:
                logger.debug("Skip %s (open buy order)", token.token_id[:12])
                continue

            best_ask = book.best_ask
            best_bid = book.best_bid

            if best_ask is None:
                logger.debug("Skip %s (no asks)", token.token_id[:12])
                continue

            if best_ask > self.s.max_entry_price:
                logger.debug(
                    "Skip %s ask=%.4f > max_entry=%.4f",
                    token.token_id[:12], best_ask, self.s.max_entry_price,
                )
                continue

            if best_ask <= 0:
                continue

            spread = book.spread
            if spread is None or spread < self.s.min_spread:
                logger.debug(
                    "Skip %s spread=%.4f < min=%.4f",
                    token.token_id[:12], spread or 0, self.s.min_spread,
                )
                continue

            ask_size = book.best_ask_size
            if ask_size < 1.0:
                logger.debug("Skip %s (ask size too small: %.2f)", token.token_id[:12], ask_size)
                continue

            score = self._score_candidate(book, best_ask, spread)

            candidates.append(Candidate(
                market=market,
                token=token,
                book=book,
                score=score,
                ask_price=best_ask,
                ask_size=ask_size,
                bid_price=best_bid or 0.0,
                spread=spread or 0.0,
            ))

        return candidates

    def _score_candidate(self, book: OrderBook, ask: float, spread: float) -> float:
        """
        Simple inefficiency score.
        Higher = more attractive.
        Factors:
         - wider spread / ask ratio
         - lower ask price
         - lower displayed depth (easier to be first in queue)
        """
        spread_ratio = spread / ask if ask > 0 else 0
        price_score = max(0, 1.0 - (ask / 0.01))  # cheaper is better, scaled to 1c
        depth_penalty = min(1.0, 100.0 / max(book.best_ask_size, 1.0))

        return (spread_ratio * 40) + (price_score * 40) + (depth_penalty * 20)

    def rank_candidates(self, candidates: List[Candidate]) -> List[Candidate]:
        """Sort candidates by score descending."""
        return sorted(candidates, key=lambda c: c.score, reverse=True)

    def compute_order_size(self, ask_price: float) -> float:
        """How many shares to buy given per_order_usd and the ask price."""
        if ask_price <= 0:
            return 0.0
        shares = self.s.per_order_usd / ask_price
        return round(shares, 2)

    def should_enter(
        self,
        current_exposure: float,
        current_positions: int,
        buys_this_cycle: int,
    ) -> bool:
        """Pre-flight checks before placing a buy."""
        if current_exposure >= self.s.max_total_exposure:
            logger.debug("Exposure cap reached: %.2f >= %.2f", current_exposure, self.s.max_total_exposure)
            return False
        if current_positions >= self.s.max_positions:
            logger.debug("Position cap reached: %d >= %d", current_positions, self.s.max_positions)
            return False
        if buys_this_cycle >= self.s.max_buys_per_cycle:
            logger.debug("Cycle buy cap reached: %d >= %d", buys_this_cycle, self.s.max_buys_per_cycle)
            return False
        return True

    def price_guard(self, price: float) -> bool:
        """Reject obviously bad prices."""
        if price <= 0:
            return False
        if price > 0.05:
            logger.warning("Price guard: %.4f exceeds hard cap $0.05", price)
            return False
        return True
