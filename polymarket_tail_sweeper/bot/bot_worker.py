"""
Background bot worker.
Runs the scan-filter-order-monitor loop in a QThread,
emits signals so the GUI can update without freezing.
"""
from __future__ import annotations

import time
import traceback
import logging
from datetime import datetime, timezone
from typing import Optional

from PySide6.QtCore import QThread, Signal

from config import Settings
from storage.database import Database
from models.data_models import (
    BotState, Position, OpenOrder, TradeRecord,
    EquitySnapshot, PnLSummary, OrderSide,
)
from adapters.polymarket_public import (
    fetch_markets, fetch_order_book, fetch_multiple_order_books,
    check_geoblock, check_api_connectivity, clear_book_cache,
)
from adapters.polymarket_trade import PaperTradingAdapter, LiveTradingAdapter
from bot.strategy import Strategy
from bot.pnl import PnLEngine
from utils.pricing import normalize_price, infer_tick_size, DEFAULT_TICK

logger = logging.getLogger("tailsweeper.worker")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class BotWorker(QThread):
    """Background worker thread for the scanning/trading loop."""

    # Signals for GUI updates
    state_changed = Signal(str)          # BotState value
    pnl_updated = Signal(object)         # PnLSummary
    positions_updated = Signal(list)     # List[Position]
    orders_updated = Signal(list)        # List[OpenOrder]
    trades_updated = Signal(list)        # List[TradeRecord]
    last_scan_time = Signal(str)
    last_order_time = Signal(str)
    geoblock_status = Signal(bool)
    api_status = Signal(bool)
    error_signal = Signal(str)
    log_message = Signal(str)

    def __init__(self, settings: Settings, db: Database, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._db = db
        self._strategy = Strategy(settings)
        self._pnl = PnLEngine(db)
        self._paper_adapter = PaperTradingAdapter()
        self._live_adapter: Optional[LiveTradingAdapter] = None
        self._running = False
        self._kill_switch = False
        self._markets_cache = []
        self._markets_last_refresh: float = 0.0
        self._consecutive_errors = 0
        self._max_consecutive_errors = 5

    @property
    def is_paper(self) -> bool:
        return self._settings.paper_mode

    def update_settings(self, settings: Settings):
        self._settings = settings
        self._strategy = Strategy(settings)

    def request_stop(self):
        """Gracefully stop the bot after the current cycle."""
        self._running = False

    def kill_switch(self):
        """Emergency stop: cancel all orders and halt immediately."""
        self._kill_switch = True
        self._running = False
        logger.warning("KILL SWITCH activated")

    def reload_markets(self):
        """Force a market reload on next cycle."""
        self._markets_cache = []
        self._markets_last_refresh = 0.0
        logger.info("Markets cache cleared; will reload on next cycle")

    def cancel_all_orders(self):
        """Cancel all open orders."""
        if self.is_paper:
            self._db.cancel_all_open_orders(is_paper=True)
            logger.info("Cancelled all paper orders")
        else:
            if self._live_adapter and self._live_adapter.is_ready:
                self._live_adapter.cancel_all_orders()
            self._db.cancel_all_open_orders(is_paper=False)
            logger.info("Cancelled all live orders")
        self.orders_updated.emit(self._db.get_open_orders(self.is_paper))

    def run(self):
        """Main thread entry point."""
        self._running = True
        self._kill_switch = False
        self._consecutive_errors = 0

        mode = "PAPER" if self.is_paper else "LIVE"
        logger.info("Bot starting in %s mode", mode)
        self.state_changed.emit(BotState.PAPER if self.is_paper else BotState.LIVE)

        if not self.is_paper:
            if not self._init_live_trading():
                self.state_changed.emit(BotState.ERROR)
                return

        while self._running and not self._kill_switch:
            try:
                self._run_cycle()
                self._consecutive_errors = 0
            except Exception as exc:
                self._consecutive_errors += 1
                logger.error("Cycle error (%d/%d): %s\n%s",
                             self._consecutive_errors,
                             self._max_consecutive_errors,
                             exc, traceback.format_exc())
                self.error_signal.emit(str(exc))
                if self._consecutive_errors >= self._max_consecutive_errors:
                    logger.error("Too many consecutive errors — stopping bot")
                    self._running = False
                    self.state_changed.emit(BotState.ERROR)
                    break

            if self._running and not self._kill_switch:
                self._interruptible_sleep(self._settings.scan_interval_sec)

        if self._kill_switch:
            self._execute_kill_switch()

        if self._settings.auto_cancel_on_stop:
            self.cancel_all_orders()

        self.state_changed.emit(BotState.STOPPED)
        logger.info("Bot stopped")

    def _interruptible_sleep(self, seconds: int):
        """Sleep that can be interrupted by stop requests."""
        for _ in range(seconds * 2):
            if not self._running or self._kill_switch:
                break
            time.sleep(0.5)

    def _init_live_trading(self) -> bool:
        """Initialize the live trading adapter."""
        errors = self._settings.validate_live_mode()
        if errors:
            for e in errors:
                logger.error("Live validation: %s", e)
                self.error_signal.emit(e)
            return False

        geo = check_geoblock()
        self.geoblock_status.emit(geo)
        if geo:
            logger.error("Geoblock detected — refusing to start live mode")
            self.error_signal.emit("Geoblocked: cannot trade from this location")
            return False

        self._live_adapter = LiveTradingAdapter(self._settings)
        if not self._live_adapter.initialize():
            self.error_signal.emit("Failed to initialize live trading client")
            return False

        return True

    # ==================================================================
    # Main cycle
    # ==================================================================
    def _run_cycle(self):
        """One full scan -> reconcile -> monitor -> enter cycle."""
        logger.info("=== Scan cycle start ===")
        clear_book_cache()

        api_ok = check_api_connectivity()
        self.api_status.emit(api_ok)
        if not api_ok:
            logger.warning("API not reachable — skipping cycle")
            return

        # 1. Refresh markets on timer or if empty
        self._maybe_refresh_markets()
        if not self._markets_cache:
            logger.warning("No markets available")
            return

        # 2. Reconcile order state (live fills, paper resting fills)
        if self.is_paper:
            self._check_paper_fills()
        else:
            self._reconcile_live_orders()

        # 3. Cancel stale orders
        self._cancel_stale_orders()

        # 4. Monitor positions — update marks and check exit triggers
        positions = self._db.get_positions(self.is_paper)
        self._monitor_positions(positions)

        # 5. Build committed-capital snapshot for entry gating
        summary = self._db.build_pnl_summary(self.is_paper)
        committed = summary.total_exposure + summary.cash_reserved

        # 6. Filter markets for entry candidates
        filtered = self._strategy.filter_markets(self._markets_cache)

        open_orders = self._db.get_open_orders(self.is_paper)
        held_tokens = {p.token_id for p in self._db.get_positions(self.is_paper)}
        open_buy_tokens = {o.token_id for o in open_orders if o.side == "BUY"}

        # 7. Scan and place new entries
        buys_this_cycle = 0
        cycle_reserved = 0.0  # additional notional reserved this cycle

        for market in filtered:
            if not self._running:
                break
            if not self._strategy.should_enter(
                committed + cycle_reserved,
                len(held_tokens),
                buys_this_cycle,
            ):
                break

            token_ids = [t.token_id for t in market.tokens]
            books = fetch_multiple_order_books(token_ids)

            candidates = self._strategy.find_candidates(
                market, books, held_tokens, open_buy_tokens,
            )
            ranked = self._strategy.rank_candidates(candidates)

            for cand in ranked:
                if not self._running:
                    break
                if not self._strategy.should_enter(
                    committed + cycle_reserved,
                    len(held_tokens),
                    buys_this_cycle,
                ):
                    break
                if not self._strategy.price_guard(cand.ask_price):
                    continue

                shares = self._strategy.compute_order_size(cand.ask_price)
                if shares <= 0:
                    continue

                order_cost = cand.ask_price * shares
                if committed + cycle_reserved + order_cost > self._settings.max_total_exposure:
                    logger.debug("Would exceed exposure cap, skipping")
                    continue

                filled_shares = self._place_buy(cand, shares)
                if filled_shares is not None:
                    buys_this_cycle += 1
                    open_buy_tokens.add(cand.token.token_id)
                    if filled_shares > 0:
                        # Immediately filled: counts as position exposure
                        held_tokens.add(cand.token.token_id)
                        committed += cand.ask_price * filled_shares
                        resting = shares - filled_shares
                        if resting > 0:
                            cycle_reserved += cand.ask_price * resting
                    else:
                        # Fully resting: counts as reserved cash
                        cycle_reserved += order_cost

        # 8. Emit updated state
        self._emit_state()
        self.last_scan_time.emit(_now())
        logger.info("=== Scan cycle end === (buys=%d)", buys_this_cycle)

    # ==================================================================
    # Market refresh
    # ==================================================================
    def _maybe_refresh_markets(self):
        now = time.monotonic()
        interval = self._settings.market_refresh_interval_sec
        if self._markets_cache and (now - self._markets_last_refresh) < interval:
            return
        self._markets_cache = fetch_markets(limit=1000)
        self._markets_last_refresh = now

    # ==================================================================
    # Live order reconciliation
    # ==================================================================
    def _reconcile_live_orders(self):
        """
        Reconcile local order records against exchange state.
        Detects new fills by comparing remaining_size deltas.
        """
        if not self._live_adapter or not self._live_adapter.is_ready:
            return

        local_open = self._db.get_open_orders(is_paper=False)
        if not local_open:
            return

        order_ids = [o.order_id for o in local_open]
        exchange_states = self._live_adapter.fetch_exchange_order_states(order_ids)

        for local in local_open:
            ex = exchange_states.get(local.order_id)
            if ex is None:
                # Exchange doesn't know this order — treat as cancelled/expired
                logger.warning(
                    "Order %s not found on exchange — marking CANCELLED",
                    local.order_id,
                )
                self._db.update_order_status(local.order_id, "CANCELLED", local.remaining_size)
                continue

            # Detect fill delta
            local_remaining = local.remaining_size
            ex_remaining = ex.remaining_size
            fill_delta = local_remaining - ex_remaining

            if fill_delta > 0.001:
                # New fill happened
                logger.info(
                    "Reconciled fill: %s %s delta=%.4f (local_rem=%.4f, ex_rem=%.4f)",
                    local.side, local.order_id, fill_delta, local_remaining, ex_remaining,
                )
                self._process_fill(local, local.price, fill_delta)

            # Sync status
            new_status = ex.status
            if new_status in ("FILLED", "CANCELLED", "EXPIRED"):
                self._db.update_order_status(local.order_id, new_status, ex_remaining)
            elif new_status == "PARTIAL":
                self._db.update_order_status(local.order_id, "OPEN", ex_remaining)
            elif new_status == "OPEN":
                if abs(ex_remaining - local_remaining) > 0.001:
                    self._db.update_order_status(local.order_id, "OPEN", ex_remaining)

    # ==================================================================
    # Paper fills for resting orders
    # ==================================================================
    def _check_paper_fills(self):
        """Check if any resting paper orders can be filled against the current book."""
        open_orders = self._db.get_open_orders(is_paper=True)
        if not open_orders:
            return

        # Batch-fetch books for all tokens with open orders
        token_ids = list({o.token_id for o in open_orders})
        books = fetch_multiple_order_books(token_ids)

        for order in open_orders:
            if not self._running:
                break
            if order.status != "OPEN":
                continue
            book = books.get(order.token_id)
            if not book:
                continue
            try:
                filled, fill_price, fill_qty = self._paper_adapter.check_resting_order_fill(
                    order.side, order.price, order.remaining_size,
                    book.best_bid, book.best_ask,
                    book.best_bid_size, book.best_ask_size,
                )
                if filled and fill_qty > 0:
                    self._process_fill(order, fill_price, fill_qty)
            except Exception as exc:
                logger.error("Error checking paper fill %s: %s", order.order_id, exc)

    # ==================================================================
    # Position monitoring + exit ladder
    # ==================================================================
    def _monitor_positions(self, positions):
        """Update mark prices and trigger exit ladder sells."""
        if not positions:
            return

        # Batch-fetch books for all held tokens
        token_ids = [p.token_id for p in positions]
        books = fetch_multiple_order_books(token_ids)

        for pos in positions:
            if not self._running:
                break
            try:
                book = books.get(pos.token_id)
                if not book:
                    continue
                self._pnl.update_mark_prices(pos.token_id, book, self.is_paper)

                refreshed = self._db.get_position_by_token(pos.token_id, self.is_paper)
                if not refreshed or refreshed.shares <= 0:
                    continue

                # Guard: skip exit if there's already an open SELL for this token
                if self._db.has_open_order_for_token(pos.token_id, "SELL", self.is_paper):
                    logger.debug(
                        "Exit skipped: existing open sell order for %s",
                        pos.token_id[:12],
                    )
                    continue

                best_bid = book.best_bid
                best_ask = book.best_ask
                mid = book.midpoint

                # Choose the trigger price based on configured mode
                if self._settings.exit_trigger_mode == "midpoint" and mid is not None:
                    trigger_price = mid
                else:
                    trigger_price = best_bid

                if trigger_price is None or trigger_price <= 0:
                    continue

                triggers = self._pnl.check_exit_rungs(
                    refreshed,
                    self._settings.exit_multiples,
                    self._settings.exit_fractions,
                    trigger_price=trigger_price,
                )

                if not triggers:
                    continue

                rung_idx, fraction, sell_shares = triggers[0]
                rung_multiple = self._settings.exit_multiples[rung_idx]
                target_price = refreshed.avg_entry * rung_multiple
                min_exit = refreshed.avg_entry + self._settings.min_exit_profit_buffer

                logger.info(
                    "Exit eval: token=%s mkt=%s avg=%.4f bid=%s ask=%s mid=%s "
                    "rung=%d multiple=%.2f target=%.4f trigger=%.4f min_exit=%.4f",
                    pos.token_id[:12],
                    (refreshed.market_question or "")[:40],
                    refreshed.avg_entry,
                    f"{best_bid:.4f}" if best_bid else "None",
                    f"{best_ask:.4f}" if best_ask else "None",
                    f"{mid:.4f}" if mid else "None",
                    rung_idx,
                    rung_multiple,
                    target_price,
                    trigger_price,
                    min_exit,
                )

                # No-loss guard: never auto-sell below breakeven + buffer
                if best_bid is None or best_bid < min_exit:
                    logger.info(
                        "Exit blocked: executable bid (%.4f) below breakeven+buffer "
                        "(%.4f) for %s (avg_entry=%.4f, buffer=%.4f)",
                        best_bid or 0.0,
                        min_exit,
                        pos.token_id[:12],
                        refreshed.avg_entry,
                        self._settings.min_exit_profit_buffer,
                    )
                    continue

                # Choose order price based on exit_order_mode
                if self._settings.exit_order_mode == "passive":
                    if mid is not None and mid > best_bid:
                        sell_price = mid
                    else:
                        sell_price = best_bid
                    # Even in passive mode, never price below breakeven
                    if sell_price < min_exit:
                        sell_price = min_exit
                    logger.info(
                        "Exit deferred: passive sell posted at %.4f (bid=%.4f) for %s",
                        sell_price, best_bid, pos.token_id[:12],
                    )
                else:
                    sell_price = best_bid
                    logger.info(
                        "Exit placed: aggressive sell at bid %.4f for %s",
                        sell_price, pos.token_id[:12],
                    )

                if sell_price > 0 and sell_shares > 0:
                    self._place_sell(refreshed, sell_price, sell_shares, rung_idx, book)

            except Exception as exc:
                logger.error("Error monitoring %s: %s", pos.token_id[:12], exc)

    # ==================================================================
    # Stale orders
    # ==================================================================
    def _cancel_stale_orders(self):
        stale = self._db.get_stale_orders(
            self._settings.stale_order_timeout_sec, self.is_paper
        )
        for order in stale:
            logger.info("Cancelling stale order %s", order.order_id)
            self._db.update_order_status(order.order_id, "CANCELLED")
            if not self.is_paper and self._live_adapter:
                self._live_adapter.cancel_order(order.order_id)
            self._db.insert_trade(TradeRecord(
                timestamp=_now(), action="CANCEL",
                market_question=order.market_question,
                token_id=order.token_id,
                price=order.price, size=order.remaining_size,
                notes="Stale order timeout", is_paper=self.is_paper,
            ))

    # ==================================================================
    # Buy placement
    # ==================================================================
    def _place_buy(self, cand, shares: float) -> Optional[float]:
        """
        Place a buy order (paper or live).
        Returns:
          - filled_shares (float >= 0) on success (0 means resting)
          - None if the order was not placed at all
        """
        price = cand.ask_price
        token_id = cand.token.token_id

        if self._db.has_open_order_for_token(token_id, "BUY", self.is_paper):
            logger.debug("Duplicate buy order guard for %s", token_id[:12])
            return None

        if self.is_paper:
            return self._place_buy_paper(cand, price, shares, token_id)
        else:
            return self._place_buy_live(cand, price, shares, token_id)

    def _place_buy_paper(self, cand, price, shares, token_id) -> Optional[float]:
        order_id = self._paper_adapter.generate_order_id()
        filled, fill_price, fill_qty = self._paper_adapter.simulate_fill(
            "BUY", price, shares,
            cand.book.best_bid, cand.book.best_ask,
            cand.book.best_bid_size, cand.book.best_ask_size,
        )

        remaining = shares if not filled else max(0, shares - fill_qty)
        status = "FILLED" if (filled and fill_qty >= shares) else "OPEN"

        self._db.insert_order(OpenOrder(
            order_id=order_id, token_id=token_id,
            condition_id=cand.market.condition_id,
            market_question=cand.market.question,
            side="BUY", price=price, size=shares,
            remaining_size=remaining, status=status,
            post_only=self._settings.use_post_only,
            is_paper=True,
        ))

        actual_fill = 0.0
        if filled and fill_qty > 0:
            actual_fill = fill_qty
            self._pnl.record_buy(
                token_id, cand.market.condition_id,
                cand.market.question, cand.token.outcome,
                fill_price, fill_qty, is_paper=True,
            )
            self._db.insert_trade(TradeRecord(
                timestamp=_now(), action="BUY",
                market_question=cand.market.question,
                outcome=cand.token.outcome, token_id=token_id,
                price=fill_price, size=fill_qty,
                gross_value=fill_price * fill_qty,
                notes=f"Paper fill (score={cand.score:.1f})",
                is_paper=True,
            ))
            self.last_order_time.emit(_now())

        logger.info(
            "Paper BUY %s | %.4f x %.2f | fill=%s (%.2f) | score=%.1f",
            cand.market.question[:40], price, shares,
            "YES" if filled else "resting", actual_fill, cand.score,
        )
        return actual_fill

    def _place_buy_live(self, cand, price, shares, token_id) -> Optional[float]:
        if not self._live_adapter or not self._live_adapter.is_ready:
            logger.error("Live adapter not ready")
            return None

        order_id = self._live_adapter.place_limit_order(
            token_id, "BUY", price, shares,
            post_only=self._settings.use_post_only,
            book=cand.book,
        )
        if not order_id:
            return None

        self._db.insert_order(OpenOrder(
            order_id=order_id, token_id=token_id,
            condition_id=cand.market.condition_id,
            market_question=cand.market.question,
            side="BUY", price=price, size=shares,
            remaining_size=shares, status="OPEN",
            post_only=self._settings.use_post_only,
            is_paper=False,
        ))
        self._db.insert_trade(TradeRecord(
            timestamp=_now(), action="BUY",
            market_question=cand.market.question,
            outcome=cand.token.outcome, token_id=token_id,
            price=price, size=shares,
            gross_value=price * shares,
            notes="Live order placed (resting)",
            is_paper=False,
        ))
        self.last_order_time.emit(_now())
        logger.info("Live BUY placed: %s @ %.4f x %.2f => %s",
                     cand.market.question[:40], price, shares, order_id)
        # Live orders are always treated as resting until reconciliation
        return 0.0

    # ==================================================================
    # Sell placement
    # ==================================================================
    def _place_sell(self, pos: Position, price: float, shares: float,
                    rung_idx: int, book: Optional[object] = None):
        """
        Place a sell order for exit ladder.
        Does NOT advance exit rung — that happens only on actual fill.
        """
        token_id = pos.token_id

        if self.is_paper:
            self._place_sell_paper(pos, price, shares, rung_idx, book)
        else:
            self._place_sell_live(pos, price, shares, rung_idx, book)

    def _place_sell_paper(self, pos, price, shares, rung_idx, book):
        token_id = pos.token_id
        order_id = self._paper_adapter.generate_order_id()

        if book is None:
            book = fetch_order_book(token_id)

        filled, fill_price, fill_qty = self._paper_adapter.simulate_fill(
            "SELL", price, shares,
            book.best_bid, book.best_ask,
            book.best_bid_size, book.best_ask_size,
        )

        if filled and fill_qty > 0:
            realized = self._pnl.record_sell(token_id, fill_price, fill_qty, is_paper=True)
            self._db.insert_trade(TradeRecord(
                timestamp=_now(), action="SELL",
                market_question=pos.market_question,
                outcome=pos.outcome, token_id=token_id,
                price=fill_price, size=fill_qty,
                gross_value=fill_price * fill_qty,
                realized_pnl=realized,
                notes=f"Exit rung {rung_idx + 1}",
                is_paper=True,
            ))
            self._db.insert_order(OpenOrder(
                order_id=order_id, token_id=token_id,
                condition_id=pos.condition_id,
                market_question=pos.market_question,
                side="SELL", price=fill_price, size=fill_qty,
                remaining_size=0, status="FILLED",
                is_paper=True,
            ))
            # Advance rung only on actual fill
            if fill_qty >= shares:
                self._pnl.advance_exit_rung(token_id, rung_idx + 1, is_paper=True)
            logger.info("Paper SELL filled: %s @ %.4f x %.2f, PnL=%.4f",
                         pos.market_question[:40], fill_price, fill_qty, realized)
        else:
            self._db.insert_order(OpenOrder(
                order_id=order_id, token_id=token_id,
                condition_id=pos.condition_id,
                market_question=pos.market_question,
                side="SELL", price=price, size=shares,
                remaining_size=shares, status="OPEN",
                is_paper=True,
            ))
            logger.info("Paper SELL resting: %s @ %.4f x %.2f (rung %d pending)",
                         pos.market_question[:40], price, shares, rung_idx + 1)

    def _place_sell_live(self, pos, price, shares, rung_idx, book):
        token_id = pos.token_id
        if not self._live_adapter or not self._live_adapter.is_ready:
            return

        order_id = self._live_adapter.place_limit_order(
            token_id, "SELL", price, shares,
            post_only=False,
            book=book,
        )
        if order_id:
            self._db.insert_order(OpenOrder(
                order_id=order_id, token_id=token_id,
                condition_id=pos.condition_id,
                market_question=pos.market_question,
                side="SELL", price=price, size=shares,
                remaining_size=shares, status="OPEN",
                is_paper=False,
            ))
            self._db.insert_trade(TradeRecord(
                timestamp=_now(), action="SELL",
                market_question=pos.market_question,
                outcome=pos.outcome, token_id=token_id,
                price=price, size=shares,
                gross_value=price * shares,
                notes=f"Live exit rung {rung_idx + 1} (resting)",
                is_paper=False,
            ))
            logger.info("Live SELL placed: %s @ %.4f x %.2f => %s",
                         token_id[:12], price, shares, order_id)

    # ==================================================================
    # Fill processing (shared by paper and live reconciliation)
    # ==================================================================
    def _process_fill(self, order: OpenOrder, fill_price: float, fill_qty: float):
        """Process a fill on a resting order."""
        if order.side == "BUY":
            self._pnl.record_buy(
                order.token_id, order.condition_id,
                order.market_question, "",
                fill_price, fill_qty, is_paper=self.is_paper,
            )
            self._db.insert_trade(TradeRecord(
                timestamp=_now(), action="BUY",
                market_question=order.market_question,
                token_id=order.token_id,
                price=fill_price, size=fill_qty,
                gross_value=fill_price * fill_qty,
                notes="Resting order filled",
                is_paper=self.is_paper,
            ))
        elif order.side == "SELL":
            realized = self._pnl.record_sell(
                order.token_id, fill_price, fill_qty, is_paper=self.is_paper,
            )
            self._db.insert_trade(TradeRecord(
                timestamp=_now(), action="SELL",
                market_question=order.market_question,
                token_id=order.token_id,
                price=fill_price, size=fill_qty,
                gross_value=fill_price * fill_qty,
                realized_pnl=realized,
                notes="Resting order filled",
                is_paper=self.is_paper,
            ))
            # Advance exit rung now that the sell actually filled
            pos = self._db.get_position_by_token(order.token_id, self.is_paper)
            if pos:
                self._pnl.advance_exit_rung(
                    order.token_id, pos.next_exit_rung + 1, self.is_paper,
                )

        new_remaining = max(0, order.remaining_size - fill_qty)
        new_status = "FILLED" if new_remaining <= 0 else "OPEN"
        self._db.update_order_status(order.order_id, new_status, new_remaining)
        logger.info("Fill processed: %s %s @ %.4f x %.2f",
                     order.side, order.order_id, fill_price, fill_qty)
        self.last_order_time.emit(_now())

    # ==================================================================
    # Kill switch
    # ==================================================================
    def _execute_kill_switch(self):
        """Emergency: cancel everything and stop."""
        logger.warning("Executing kill switch — cancelling all orders")
        self.cancel_all_orders()
        self.state_changed.emit(BotState.STOPPED)

    # ==================================================================
    # State emission
    # ==================================================================
    def _emit_state(self):
        """Push current state to the GUI via signals."""
        try:
            summary = self._db.build_pnl_summary(self.is_paper)
            self.pnl_updated.emit(summary)
            self.positions_updated.emit(self._db.get_positions(self.is_paper))
            self.orders_updated.emit(self._db.get_open_orders(self.is_paper))
            self.trades_updated.emit(self._db.get_trades(self.is_paper, limit=200))

            self._db.insert_equity_snapshot(EquitySnapshot(
                timestamp=_now(),
                total_exposure=summary.total_exposure,
                cash_reserved=summary.cash_reserved,
                unrealized_pnl=summary.unrealized_pnl,
                realized_pnl=summary.realized_pnl,
                total_pnl=summary.total_pnl,
            ))
        except Exception as exc:
            logger.error("Error emitting state: %s", exc)
