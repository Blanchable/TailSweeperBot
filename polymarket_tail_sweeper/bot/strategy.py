"""
Strategy: filters markets, scores candidates, decides entries and exits.
Includes liquidity quality filters and market-memory scoring.
"""
from __future__ import annotations

import logging
from typing import List, Set

from config import Settings
from models.data_models import Market, Token, OrderBook, Candidate

logger = logging.getLogger("tailsweeper.strategy")


class Strategy:
    """Tail-sweep / micro-scalp entry/exit strategy logic."""

    def __init__(self, settings: Settings):
        self.s = settings
        self._recent_winners: Set[str] = set()
        self._position_condition_counts: dict = {}
        self._farm_tokens: Set[str] = set()

    def set_recent_winners(self, token_ids: Set[str]):
        """Update the set of recently profitable token_ids for score boosting."""
        self._recent_winners = token_ids

    def set_position_condition_counts(self, counts: dict):
        """Update {condition_id: position_count} for exposure capping."""
        self._position_condition_counts = counts

    def set_farm_tokens(self, token_ids: Set[str]):
        """Update the set of tokens currently in the farm list."""
        self._farm_tokens = token_ids

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
        """Find tokens that qualify as buy candidates with liquidity quality checks."""
        # Market-level exposure cap
        cap = self.s.same_market_exposure_cap
        if cap > 0:
            existing = self._position_condition_counts.get(market.condition_id, 0)
            if existing >= cap:
                logger.debug("Skip market %s (exposure cap %d/%d)",
                             market.question[:40], existing, cap)
                return []

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
                logger.debug("Skip %s ask=%.4f > max=%.4f",
                             token.token_id[:12], best_ask, self.s.max_entry_price)
                continue

            if best_ask <= 0:
                continue

            # --- Liquidity quality filters ---
            if best_bid is None:
                logger.debug("Skip %s (no bids — one-sided book)", token.token_id[:12])
                continue

            bid_size = book.best_bid_size
            ask_size = book.best_ask_size

            if bid_size < self.s.min_best_bid_size:
                logger.debug("Skip %s bid_size=%.1f < min=%.1f",
                             token.token_id[:12], bid_size, self.s.min_best_bid_size)
                continue

            if ask_size < self.s.min_best_ask_size:
                logger.debug("Skip %s ask_size=%.1f < min=%.1f",
                             token.token_id[:12], ask_size, self.s.min_best_ask_size)
                continue

            spread = book.spread
            if spread is None or spread < self.s.min_spread:
                logger.debug("Skip %s spread=%.4f < min=%.4f",
                             token.token_id[:12], spread or 0, self.s.min_spread)
                continue

            spread_ratio = spread / best_ask if best_ask > 0 else 1.0
            if spread_ratio > self.s.max_spread_ratio:
                logger.debug("Skip %s spread_ratio=%.2f > max=%.2f",
                             token.token_id[:12], spread_ratio, self.s.max_spread_ratio)
                continue

            # Banded rules: higher-priced entries require stronger liquidity
            if best_ask > 0.01:
                min_depth = max(self.s.min_best_bid_size, 50.0)
                max_ratio = min(self.s.max_spread_ratio, 0.35)
                if bid_size < min_depth or ask_size < min_depth:
                    logger.debug("Skip %s (banded: depth %.0f/%.0f < %.0f for ask>1c)",
                                 token.token_id[:12], bid_size, ask_size, min_depth)
                    continue
                if spread_ratio > max_ratio:
                    logger.debug("Skip %s (banded: ratio %.2f > %.2f for ask>1c)",
                                 token.token_id[:12], spread_ratio, max_ratio)
                    continue

            score = self._score_candidate(book, best_ask, spread, token.token_id)

            candidates.append(Candidate(
                market=market,
                token=token,
                book=book,
                score=score,
                ask_price=best_ask,
                ask_size=ask_size,
                bid_price=best_bid,
                spread=spread or 0.0,
            ))

        return candidates

    def _score_candidate(
        self, book: OrderBook, ask: float, spread: float, token_id: str = ""
    ) -> float:
        """
        Composite inefficiency + liquidity score.
        Higher = more attractive.
        """
        spread_ratio = spread / ask if ask > 0 else 0
        price_score = max(0, 1.0 - (ask / 0.04))
        depth_bonus = min(1.0, min(book.best_bid_size, book.best_ask_size) / 100.0)
        tightness = max(0, 1.0 - spread_ratio)

        base = (tightness * 25) + (price_score * 20) + (depth_bonus * 25)

        # Bid-side quality bonus
        if book.best_bid_size > 0:
            bid_quality = min(1.0, book.best_bid_size / 100.0)
            base += bid_quality * 10

        # Recent winner boost
        if token_id in self._recent_winners:
            base += 20.0

        # Farm token boost (large, pushes farm tokens to top of ranking)
        if token_id in self._farm_tokens:
            base += self.s.farm_score_boost

        return base

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
        committed_capital: float,
        current_positions: int,
        buys_this_cycle: int,
    ) -> bool:
        """Pre-flight checks before placing a buy."""
        if committed_capital >= self.s.max_total_exposure:
            logger.debug("Committed capital cap: %.2f >= %.2f",
                         committed_capital, self.s.max_total_exposure)
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
        if price > 0.10:
            logger.warning("Price guard: %.4f exceeds hard cap $0.10", price)
            return False
        return True
