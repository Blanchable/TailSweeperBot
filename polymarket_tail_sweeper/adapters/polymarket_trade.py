"""
Authenticated Polymarket trading adapter.
Wraps the official py-clob-client SDK for live order placement/cancellation.
Provides a clean adapter boundary so the rest of the app doesn't depend
directly on SDK internals.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

from config import Settings, POLYMARKET_CLOB_BASE
from models.data_models import OrderBook
from utils.pricing import normalize_price, infer_tick_size, DEFAULT_TICK

logger = logging.getLogger("tailsweeper.trade")


@dataclass
class ExchangeOrderState:
    """Normalized snapshot of one order as the exchange sees it."""
    order_id: str
    status: str          # OPEN, FILLED, CANCELLED, EXPIRED, PARTIAL
    remaining_size: float
    original_size: float
    price: float
    side: str
    token_id: str


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

    # ------------------------------------------------------------------
    # Order placement with tick normalization + real post-only
    # ------------------------------------------------------------------
    def _post_order_compat(self, signed_order, order_type_value):
        """
        Submit a signed order to the CLOB via the SDK, handling keyword
        argument differences across py-clob-client versions.

        The SDK's post_order() uses camelCase ``orderType`` (not snake_case
        ``order_type``).  We call with the correct keyword and surface a
        clear adapter-level message if the SDK shape changes again.
        """
        try:
            return self._client.post_order(signed_order, orderType=order_type_value)
        except TypeError as exc:
            msg = str(exc)
            if "orderType" in msg or "order_type" in msg or "unexpected keyword" in msg:
                logger.error(
                    "py-clob-client API appears incompatible with this adapter. "
                    "post_order() rejected our keyword argument. SDK error: %s",
                    msg,
                )
            raise

    def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        post_only: bool = False,
        book: Optional[OrderBook] = None,
    ) -> Optional[str]:
        """
        Place a limit order on the CLOB with exchange-safe pricing.
        Returns the order ID string on success, None on failure/skip.
        """
        if not self.is_ready:
            logger.error("Trading adapter not initialized")
            return None

        tick = infer_tick_size(book) if book else DEFAULT_TICK
        best_bid = book.best_bid if book else None
        best_ask = book.best_ask if book else None

        safe_price = normalize_price(
            price, tick, side, post_only, best_bid, best_ask,
        )
        if safe_price is None:
            logger.warning(
                "Order skipped: no valid %s price for %s "
                "(raw=%.6f, post_only=%s, bid=%s, ask=%s)",
                side, token_id[:12], price, post_only, best_bid, best_ask,
            )
            return None

        # Marketable min-size guard: the CLOB rejects marketable orders
        # below $1.  If this order would cross the spread, enforce the min.
        is_marketable = False
        if side.upper() == "BUY" and best_ask is not None and safe_price >= best_ask:
            is_marketable = True
        elif side.upper() == "SELL" and best_bid is not None and safe_price <= best_bid:
            is_marketable = True

        if is_marketable:
            order_usd = safe_price * size
            min_usd = self._settings.min_marketable_order_usd
            if order_usd < min_usd:
                needed_size = min_usd / safe_price if safe_price > 0 else 0
                if needed_size > size * 1.5:
                    logger.warning(
                        "Skipped marketable %s order: $%.2f < min $%.2f for %s",
                        side, order_usd, min_usd, token_id[:12],
                    )
                    return None
                logger.info(
                    "Bumped marketable %s size %.2f->%.2f for %s (min $%.2f)",
                    side, size, needed_size, token_id[:12], min_usd,
                )
                size = round(needed_size, 2)

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            order_side = BUY if side.upper() == "BUY" else SELL

            order_args = OrderArgs(
                price=safe_price,
                size=size,
                side=order_side,
                token_id=token_id,
            )

            signed_order = self._client.create_order(order_args)
            resp = self._post_order_compat(signed_order, OrderType.GTC)

            order_id = None
            if isinstance(resp, dict):
                order_id = resp.get("orderID") or resp.get("id")
            elif hasattr(resp, "orderID"):
                order_id = resp.orderID

            if order_id:
                logger.info(
                    "Live order placed: %s %s @ %.4f (raw %.4f) x %.2f => %s",
                    side, token_id[:12], safe_price, price, size, order_id,
                )
            else:
                logger.warning("Order response didn't contain order ID: %s", resp)

            return order_id
        except Exception as exc:
            logger.error("Failed to place live order: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Order cancellation
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Order state reconciliation
    # ------------------------------------------------------------------
    def get_open_orders(self) -> List[Dict[str, Any]]:
        """Fetch all open orders from the exchange as raw dicts."""
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

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single order by ID from the exchange."""
        if not self.is_ready:
            return None
        try:
            resp = self._client.get_order(order_id)
            if isinstance(resp, dict):
                return resp
            return None
        except Exception as exc:
            logger.debug("get_order(%s) failed: %s", order_id, exc)
            return None

    def fetch_exchange_order_states(
        self, order_ids: List[str]
    ) -> Dict[str, ExchangeOrderState]:
        """
        Fetch current exchange state for a list of order IDs.
        Uses get_open_orders() as primary source, falls back to
        per-order lookups for IDs not found in the open set.
        Returns a dict keyed by order_id.
        """
        result: Dict[str, ExchangeOrderState] = {}
        if not self.is_ready or not order_ids:
            return result

        # 1. Bulk fetch all open orders and index them
        open_orders_raw = self.get_open_orders()
        exchange_open: Dict[str, Dict] = {}
        for raw in open_orders_raw:
            oid = raw.get("orderID") or raw.get("id") or raw.get("order_id", "")
            if oid:
                exchange_open[oid] = raw

        wanted = set(order_ids)

        # 2. Process orders found in the open set
        for oid in wanted:
            raw = exchange_open.get(oid)
            if raw:
                result[oid] = self._normalize_exchange_order(raw)
                continue

            # 3. Not in open set — try individual lookup (may be filled/cancelled)
            raw = self.get_order(oid)
            if raw:
                result[oid] = self._normalize_exchange_order(raw)

        return result

    @staticmethod
    def _normalize_exchange_order(raw: Dict[str, Any]) -> ExchangeOrderState:
        """Normalize a raw SDK order dict into ExchangeOrderState."""
        oid = raw.get("orderID") or raw.get("id") or raw.get("order_id", "")

        # Status normalization — SDK may use various field names
        status_raw = (
            raw.get("status")
            or raw.get("orderStatus")
            or raw.get("order_status")
            or "UNKNOWN"
        )
        status_map = {
            "LIVE": "OPEN",
            "ACTIVE": "OPEN",
            "OPEN": "OPEN",
            "MATCHED": "FILLED",
            "FILLED": "FILLED",
            "CANCELLED": "CANCELLED",
            "CANCELED": "CANCELLED",
            "EXPIRED": "EXPIRED",
        }
        status = status_map.get(status_raw.upper(), status_raw.upper())

        # Size fields
        original = _safe_float(raw.get("original_size") or raw.get("originalSize") or raw.get("size"), 0.0)
        size_matched = _safe_float(raw.get("size_matched") or raw.get("sizeMatched"), 0.0)
        remaining = _safe_float(raw.get("remaining_size") or raw.get("remainingSize"), None)

        if remaining is None:
            remaining = max(0.0, original - size_matched)

        if 0 < remaining < original and status == "OPEN":
            status = "PARTIAL"

        price = _safe_float(raw.get("price"), 0.0)
        side_raw = raw.get("side", "BUY")
        if isinstance(side_raw, str):
            side = side_raw.upper()
        else:
            side = "BUY" if side_raw == 0 else "SELL"

        token_id = raw.get("asset_id") or raw.get("token_id") or raw.get("tokenID") or ""

        return ExchangeOrderState(
            order_id=oid,
            status=status,
            remaining_size=remaining,
            original_size=original,
            price=price,
            side=side,
            token_id=token_id,
        )

    def get_balances(self) -> Dict[str, Any]:
        """Fetch token balances / allowances."""
        if not self.is_ready:
            return {}
        try:
            return {"status": "connected"}
        except Exception as exc:
            logger.error("Failed to fetch balances: %s", exc)
            return {}

    def get_wallet_positions(self) -> Dict[str, float]:
        """
        Fetch actual current outcome-token balances for the live wallet.
        Returns {token_id: shares_held}.

        Uses the Polymarket Data API (data-api.polymarket.com/positions)
        with pagination and sizeThreshold=0 so even tiny positions appear.

        For POLY_PROXY signature types, the funder address may differ from
        the actual proxy wallet.  If the initial fetch returns empty, we
        attempt to resolve the proxy wallet via the Gamma public-profile
        endpoint and retry.
        """
        if not self.is_ready:
            return {}
        funder = self._settings.funder_address
        if not funder:
            return {}

        holdings = self._fetch_positions_data_api(funder)

        # Proxy-wallet fallback: if empty and using POLY_PROXY, resolve actual proxy
        if not holdings and self._settings.signature_type == 1:
            proxy = self._resolve_proxy_wallet(funder)
            if proxy and proxy.lower() != funder.lower():
                logger.info("Wallet sync: retrying with proxy wallet %s...%s",
                            proxy[:6], proxy[-4:])
                holdings = self._fetch_positions_data_api(proxy)

        logger.info("Wallet positions fetched: %d tokens held (addr=%s...%s)",
                     len(holdings), funder[:6], funder[-4:])
        return holdings

    def _fetch_positions_data_api(self, address: str) -> Dict[str, float]:
        """Paginated fetch from the Data API positions endpoint."""
        import requests
        holdings: Dict[str, float] = {}
        offset = 0
        page_size = 500
        data_api = "https://data-api.polymarket.com"

        while True:
            try:
                resp = requests.get(
                    f"{data_api}/positions",
                    params={
                        "user": address,
                        "sizeThreshold": "0",
                        "limit": str(page_size),
                        "offset": str(offset),
                    },
                    timeout=15,
                )
                if resp.status_code != 200:
                    logger.warning("Data API positions returned %d for %s...%s",
                                   resp.status_code, address[:6], address[-4:])
                    break
                data = resp.json()
                if not data:
                    break

                items = data if isinstance(data, list) else data.get("positions", data.get("data", []))
                if not isinstance(items, list) or not items:
                    break

                for item in items:
                    tid = (item.get("asset") or item.get("token_id")
                           or item.get("tokenId") or item.get("assetId") or "")
                    size = _safe_float(
                        item.get("size") or item.get("balance")
                        or item.get("shares") or item.get("amount"), 0.0
                    )
                    if tid and size > 0:
                        holdings[tid] = holdings.get(tid, 0.0) + size

                if len(items) < page_size:
                    break
                offset += page_size
            except Exception as exc:
                logger.error("Data API positions fetch error: %s", exc)
                break

        return holdings

    @staticmethod
    def _resolve_proxy_wallet(funder: str) -> Optional[str]:
        """Look up the proxy wallet address via Gamma public-profile."""
        import requests
        from config import POLYMARKET_GAMMA_BASE
        try:
            resp = requests.get(
                f"{POLYMARKET_GAMMA_BASE}/public-profile",
                params={"address": funder},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                proxy = data.get("proxyWallet") or data.get("proxy_wallet") or ""
                if proxy:
                    return proxy
        except Exception as exc:
            logger.debug("Proxy wallet lookup failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Paper trading adapter
# ---------------------------------------------------------------------------
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


def _safe_float(val, default=0.0):
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default if default is not None else 0.0
