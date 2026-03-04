"""
Public (unauthenticated) Polymarket data adapter.
Fetches markets, order books, and prices using the Gamma API and CLOB API.
"""
from __future__ import annotations

import time
import logging
from typing import List, Optional, Dict, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import POLYMARKET_CLOB_BASE, POLYMARKET_GAMMA_BASE, POLYMARKET_GEOBLOCK_URL
from models.data_models import (
    Market, Token, OrderBook, OrderBookLevel,
)

logger = logging.getLogger("tailsweeper.public")


def _build_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"Accept": "application/json"})
    return s


_session = _build_session()


def check_geoblock() -> bool:
    """Return True if we appear to be geo-blocked (cannot reach the API)."""
    try:
        resp = _session.get(POLYMARKET_GEOBLOCK_URL, timeout=10)
        if resp.status_code == 403:
            return True
        if resp.status_code == 200:
            return False
        return False
    except Exception as exc:
        logger.warning("Geoblock check failed: %s", exc)
        return True


def check_api_connectivity() -> bool:
    """Return True if we can reach the Polymarket CLOB API."""
    try:
        resp = _session.get(f"{POLYMARKET_CLOB_BASE}/time", timeout=10)
        return resp.status_code == 200
    except Exception:
        return False


def fetch_markets(limit: int = 500, active_only: bool = True) -> List[Market]:
    """
    Fetch markets from the Gamma API.
    Returns a list of Market objects.
    Paginates through results until exhausted.
    """
    all_markets: List[Market] = []
    offset = 0
    page_size = min(limit, 100)

    while len(all_markets) < limit:
        params: Dict[str, Any] = {
            "limit": page_size,
            "offset": offset,
            "closed": "false",
            "order": "volume",
            "ascending": "false",
        }
        if active_only:
            params["active"] = "true"

        try:
            resp = _session.get(
                f"{POLYMARKET_GAMMA_BASE}/markets",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Failed fetching markets at offset %d: %s", offset, exc)
            break

        if not data:
            break

        for item in data:
            try:
                m = _parse_market(item)
                if m:
                    all_markets.append(m)
            except Exception as exc:
                logger.debug("Skipping market parse error: %s", exc)

        if len(data) < page_size:
            break
        offset += page_size
        time.sleep(0.2)

    logger.info("Fetched %d markets from Gamma API", len(all_markets))
    return all_markets


def _parse_market(item: dict) -> Optional[Market]:
    """Parse a single Gamma API market response into our Market model."""
    condition_id = item.get("conditionId") or item.get("condition_id", "")
    question = item.get("question", "Unknown")
    active = item.get("active", True)
    if isinstance(active, str):
        active = active.lower() == "true"
    closed = item.get("closed", False)
    if isinstance(closed, str):
        closed = closed.lower() == "true"
    neg_risk = item.get("negRisk", False)
    if isinstance(neg_risk, str):
        neg_risk = neg_risk.lower() == "true"

    fee_raw = item.get("makerRewardBps") or item.get("fee", 0)
    try:
        fee = float(fee_raw) if fee_raw else 0.0
    except (ValueError, TypeError):
        fee = 0.0

    volume = 0.0
    try:
        volume = float(item.get("volume", 0) or 0)
    except (ValueError, TypeError):
        pass

    tokens: List[Token] = []
    clob_token_ids = item.get("clobTokenIds")
    outcomes_raw = item.get("outcomes")
    outcome_prices = item.get("outcomePrices")

    if clob_token_ids and outcomes_raw:
        if isinstance(clob_token_ids, str):
            import json
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except Exception:
                clob_token_ids = []
        if isinstance(outcomes_raw, str):
            import json
            try:
                outcomes_raw = json.loads(outcomes_raw)
            except Exception:
                outcomes_raw = []
        if isinstance(outcome_prices, str):
            import json
            try:
                outcome_prices = json.loads(outcome_prices)
            except Exception:
                outcome_prices = []

        for i, tid in enumerate(clob_token_ids):
            outcome_name = outcomes_raw[i] if i < len(outcomes_raw) else f"Outcome {i}"
            price = 0.0
            if outcome_prices and i < len(outcome_prices):
                try:
                    price = float(outcome_prices[i])
                except (ValueError, TypeError):
                    pass
            tokens.append(Token(token_id=str(tid), outcome=str(outcome_name), price=price))

    if not condition_id:
        return None

    return Market(
        condition_id=condition_id,
        question=question,
        tokens=tokens,
        active=active,
        closed=closed,
        neg_risk=neg_risk,
        fee=fee,
        volume=volume,
        end_date=item.get("endDate"),
    )


def fetch_order_book(token_id: str) -> OrderBook:
    """Fetch order book for a single token from the CLOB API."""
    try:
        resp = _session.get(
            f"{POLYMARKET_CLOB_BASE}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("Order book fetch failed for %s: %s", token_id, exc)
        return OrderBook(token_id=token_id)

    bids = []
    asks = []

    for b in data.get("bids", []):
        try:
            bids.append(OrderBookLevel(price=float(b["price"]), size=float(b["size"])))
        except (KeyError, ValueError, TypeError):
            pass

    for a in data.get("asks", []):
        try:
            asks.append(OrderBookLevel(price=float(a["price"]), size=float(a["size"])))
        except (KeyError, ValueError, TypeError):
            pass

    bids.sort(key=lambda x: x.price, reverse=True)
    asks.sort(key=lambda x: x.price)

    return OrderBook(token_id=token_id, bids=bids, asks=asks)


def fetch_price(token_id: str) -> Optional[float]:
    """Fetch the current midpoint price for a token."""
    book = fetch_order_book(token_id)
    return book.midpoint


def fetch_best_bid(token_id: str) -> Optional[float]:
    book = fetch_order_book(token_id)
    return book.best_bid


def fetch_multiple_order_books(token_ids: List[str]) -> Dict[str, OrderBook]:
    """Fetch books for multiple tokens (sequential with rate limiting)."""
    books: Dict[str, OrderBook] = {}
    for tid in token_ids:
        books[tid] = fetch_order_book(tid)
        time.sleep(0.1)
    return books
