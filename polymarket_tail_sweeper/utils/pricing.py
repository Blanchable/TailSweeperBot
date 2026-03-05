"""
Exchange-safe price normalization and tick alignment.
All live order prices must be routed through this module before submission.
"""
from __future__ import annotations

import math
import logging
from typing import Optional, List

from models.data_models import OrderBook, OrderBookLevel

logger = logging.getLogger("tailsweeper.pricing")

# Polymarket CLOB prices are probabilities in (0, 1).
# The exchange uses a minimum tick size of 0.001 for most ranges, but
# it can vary. We infer from the visible book when possible.
MIN_PRICE = 0.001
MAX_PRICE = 0.999
DEFAULT_TICK = 0.001


def clamp_price(price: float) -> float:
    """Keep a price within the valid probability range."""
    return max(MIN_PRICE, min(MAX_PRICE, price))


def infer_tick_size(book: OrderBook) -> float:
    """
    Attempt to infer the tick size from visible book levels.
    Looks at price increments between adjacent levels on both sides.
    Falls back to DEFAULT_TICK if no clear increment can be determined.
    """
    increments: List[float] = []

    for levels in (book.bids, book.asks):
        if len(levels) < 2:
            continue
        for i in range(min(len(levels) - 1, 5)):
            diff = abs(levels[i].price - levels[i + 1].price)
            if diff > 1e-9:
                increments.append(round(diff, 6))

    if not increments:
        return DEFAULT_TICK

    # The most common increment is the best guess for tick size
    from collections import Counter
    counts = Counter(increments)
    best_tick = counts.most_common(1)[0][0]

    if best_tick < 0.0001 or best_tick > 0.1:
        return DEFAULT_TICK

    return best_tick


def round_to_tick(price: float, tick: float) -> float:
    """Round a price to the nearest tick increment."""
    if tick <= 0:
        tick = DEFAULT_TICK
    return round(round(price / tick) * tick, 6)


def round_down_to_tick(price: float, tick: float) -> float:
    """Round a price down (floor) to the nearest tick. Useful for BUY limits."""
    if tick <= 0:
        tick = DEFAULT_TICK
    return round(math.floor(price / tick) * tick, 6)


def round_up_to_tick(price: float, tick: float) -> float:
    """Round a price up (ceil) to the nearest tick. Useful for SELL limits."""
    if tick <= 0:
        tick = DEFAULT_TICK
    return round(math.ceil(price / tick) * tick, 6)


def normalize_price(
    price: float,
    tick: float,
    side: str,
    post_only: bool,
    best_bid: Optional[float],
    best_ask: Optional[float],
) -> Optional[float]:
    """
    Normalize a price to a valid exchange tick and apply post-only safety.

    Returns None if no valid price exists (e.g., post-only BUY can't avoid
    crossing the ask), meaning the order should be skipped.
    """
    price = clamp_price(price)

    if side.upper() == "BUY":
        # Round down so we don't accidentally overpay
        normalized = round_down_to_tick(price, tick)

        if post_only and best_ask is not None:
            if normalized >= best_ask:
                # Pull back one tick below the ask
                safe = round_down_to_tick(best_ask - tick, tick)
                if safe <= 0 or safe < MIN_PRICE:
                    logger.warning(
                        "Post-only BUY: no valid price below ask %.4f (tick=%.4f)",
                        best_ask, tick,
                    )
                    return None
                logger.info(
                    "Post-only BUY: repriced %.4f -> %.4f (ask=%.4f)",
                    normalized, safe, best_ask,
                )
                normalized = safe

    elif side.upper() == "SELL":
        # Round up so we don't accidentally undersell
        normalized = round_up_to_tick(price, tick)

        if post_only and best_bid is not None:
            if normalized <= best_bid:
                safe = round_up_to_tick(best_bid + tick, tick)
                if safe > MAX_PRICE:
                    logger.warning(
                        "Post-only SELL: no valid price above bid %.4f (tick=%.4f)",
                        best_bid, tick,
                    )
                    return None
                logger.info(
                    "Post-only SELL: repriced %.4f -> %.4f (bid=%.4f)",
                    normalized, safe, best_bid,
                )
                normalized = safe
    else:
        normalized = round_to_tick(price, tick)

    normalized = clamp_price(normalized)
    return normalized
