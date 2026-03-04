"""
Authenticated Polymarket trading adapter.
Wraps the official py-clob-client SDK for live order placement/cancellation.
Provides a clean adapter boundary so the rest of the app doesn't depend
directly on SDK internals.
"""
from __future__ import annotations

import logging
from typing import Optional, List, Dict, Any

from config import Settings, POLYMARKET_CLOB_BASE

logger = logging.getLogger("tailsweeper.trade")


class LiveTradingAdapter:
    """
    Adapter for live Polymarket CLOB trading.
    Wraps py-clob-client. Only instantiated when live mode is activated.
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = None
        self._initialized = False

    def initialize(self) -> bool:
        """Initialize the CLOB client with credentials. Returns True on success."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            host = POLYMARKET_CLOB_BASE
            key = self._settings.private_key
            chain_id = 137  # Polygon mainnet

            self._client = ClobClient(
                host,
                key=key,
                chain_id=chain_id,
                funder=self._settings.funder_address or None,
                signature_type=self._settings.signature_type,
            )

            # Derive API credentials
            self._client.set_api_creds(self._client.create_or_derive_api_creds())
            self._initialized = True
            logger.info("Live trading adapter initialized successfully")
            return True
        except ImportError:
            logger.error(
                "py-clob-client not installed. Install with: pip install py-clob-client"
            )
            return False
        except Exception as exc:
            logger.error("Failed to initialize live trading adapter: %s", exc)
            return False

    @property
    def is_ready(self) -> bool:
        return self._initialized and self._client is not None

    def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        post_only: bool = False,
    ) -> Optional[str]:
        """
        Place a limit order on the CLOB.
        Returns the order ID string on success, None on failure.
        """
        if not self.is_ready:
            logger.error("Trading adapter not initialized")
            return None
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            order_side = BUY if side.upper() == "BUY" else SELL

            order_args = OrderArgs(
                price=price,
                size=size,
                side=order_side,
                token_id=token_id,
            )

            signed_order = self._client.create_order(order_args)
            resp = self._client.post_order(signed_order, order_type=OrderType.GTC)

            order_id = None
            if isinstance(resp, dict):
                order_id = resp.get("orderID") or resp.get("id")
            elif hasattr(resp, "orderID"):
                order_id = resp.orderID

            if order_id:
                logger.info(
                    "Live order placed: %s %s @ %.4f x %.2f => %s",
                    side, token_id[:12], price, size, order_id,
                )
            else:
                logger.warning("Order response didn't contain order ID: %s", resp)

            return order_id
        except Exception as exc:
            logger.error("Failed to place live order: %s", exc)
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order by ID. Returns True on success."""
        if not self.is_ready:
            return False
        try:
            self._client.cancel(order_id)
            logger.info("Cancelled live order: %s", order_id)
            return True
        except Exception as exc:
            logger.error("Failed to cancel order %s: %s", order_id, exc)
            return False

    def cancel_all_orders(self) -> bool:
        """Cancel all open orders. Returns True on success."""
        if not self.is_ready:
            return False
        try:
            self._client.cancel_all()
            logger.info("Cancelled all live orders")
            return True
        except Exception as exc:
            logger.error("Failed to cancel all orders: %s", exc)
            return False

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """Fetch all open orders from the exchange."""
        if not self.is_ready:
            return []
        try:
            orders = self._client.get_orders()
            if isinstance(orders, list):
                return orders
            return []
        except Exception as exc:
            logger.error("Failed to fetch open orders: %s", exc)
            return []

    def get_balances(self) -> Dict[str, Any]:
        """Fetch token balances / allowances."""
        if not self.is_ready:
            return {}
        try:
            # The SDK doesn't have a direct balance call; this is a placeholder
            # for when the user integrates with their wallet provider.
            return {"status": "connected"}
        except Exception as exc:
            logger.error("Failed to fetch balances: %s", exc)
            return {}


class PaperTradingAdapter:
    """
    Paper trading adapter that simulates order placement using real market data.
    Tracks simulated orders and fills conservatively.
    """

    def __init__(self):
        self._next_order_id = 1

    def generate_order_id(self) -> str:
        oid = f"PAPER-{self._next_order_id:06d}"
        self._next_order_id += 1
        return oid

    def simulate_fill(
        self,
        side: str,
        price: float,
        size: float,
        best_bid: Optional[float],
        best_ask: Optional[float],
        best_bid_size: float = 0.0,
        best_ask_size: float = 0.0,
    ) -> tuple:
        """
        Conservative fill simulation.
        Returns (filled_immediately: bool, fill_price: float, fill_size: float).

        BUY: fills immediately only if price >= best_ask and adequate size.
        SELL: fills immediately only if price <= best_bid and adequate size.
        Otherwise order rests.
        """
        if side.upper() == "BUY":
            if best_ask is not None and price >= best_ask and best_ask_size >= size:
                return (True, best_ask, size)
            elif best_ask is not None and price >= best_ask and best_ask_size > 0:
                return (True, best_ask, min(size, best_ask_size))
            return (False, price, 0.0)

        elif side.upper() == "SELL":
            if best_bid is not None and price <= best_bid and best_bid_size >= size:
                return (True, best_bid, size)
            elif best_bid is not None and price <= best_bid and best_bid_size > 0:
                return (True, best_bid, min(size, best_bid_size))
            return (False, price, 0.0)

        return (False, price, 0.0)

    def check_resting_order_fill(
        self,
        side: str,
        order_price: float,
        remaining_size: float,
        best_bid: Optional[float],
        best_ask: Optional[float],
        best_bid_size: float = 0.0,
        best_ask_size: float = 0.0,
    ) -> tuple:
        """
        Check if a resting simulated order would now fill.
        Returns (filled: bool, fill_price: float, fill_size: float).
        """
        if side.upper() == "BUY":
            if best_ask is not None and order_price >= best_ask:
                fill_qty = min(remaining_size, best_ask_size) if best_ask_size > 0 else 0.0
                if fill_qty > 0:
                    return (True, best_ask, fill_qty)
        elif side.upper() == "SELL":
            if best_bid is not None and order_price <= best_bid:
                fill_qty = min(remaining_size, best_bid_size) if best_bid_size > 0 else 0.0
                if fill_qty > 0:
                    return (True, best_bid, fill_qty)
        return (False, order_price, 0.0)
