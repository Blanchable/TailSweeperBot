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
    fetch_markets, fetch_order_book, check_geoblock, check_api_connectivity,
)
from adapters.polymarket_trade import PaperTradingAdapter, LiveTradingAdapter
from bot.strategy import Strategy
from bot.pnl import PnLEngine

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

    def _run_cycle(self):
        """One full scan → filter → order → monitor cycle."""
        logger.info("=== Scan cycle start ===")

        api_ok = check_api_connectivity()
        self.api_status.emit(api_ok)
        if not api_ok:
            logger.warning("API not reachable — skipping cycle")
            return

        # 1. Load or refresh markets
        if not self._markets_cache:
            self._markets_cache = fetch_markets(limit=1000)
            if not self._markets_cache:
                logger.warning("No markets fetched")
                return

        # 2. Filter markets
        filtered = self._strategy.filter_markets(self._markets_cache)

        # 3. Check existing positions and orders
        positions = self._db.get_positions(self.is_paper)
        open_orders = self._db.get_open_orders(self.is_paper)
        held_tokens = {p.token_id for p in positions}
        open_buy_tokens = {o.token_id for o in open_orders if o.side == "BUY"}

        summary = self._db.build_pnl_summary(self.is_paper)

        # 4. Monitor positions — update marks and check exit triggers
        self._monitor_positions(positions)

        # 5. Check resting paper orders for fills
        if self.is_paper:
            self._check_paper_fills(open_orders)

        # 6. Cancel stale orders
        self._cancel_stale_orders()

        # 7. Find and rank new entry candidates
        buys_this_cycle = 0
        for market in filtered:
            if not self._running:
                break
            if not self._strategy.should_enter(
                summary.total_exposure, summary.open_positions, buys_this_cycle
            ):
                break

            token_ids = [t.token_id for t in market.tokens]
            books = {}
            for tid in token_ids:
                books[tid] = fetch_order_book(tid)
                time.sleep(0.05)

            candidates = self._strategy.find_candidates(
                market, books, held_tokens, open_buy_tokens
            )
            ranked = self._strategy.rank_candidates(candidates)

            for cand in ranked:
                if not self._running:
                    break
                if not self._strategy.should_enter(
                    summary.total_exposure, summary.open_positions, buys_this_cycle
                ):
                    break
                if not self._strategy.price_guard(cand.ask_price):
                    continue

                shares = self._strategy.compute_order_size(cand.ask_price)
                if shares <= 0:
                    continue

                order_cost = cand.ask_price * shares
                if summary.total_exposure + order_cost > self._settings.max_total_exposure:
                    logger.debug("Would exceed exposure cap, skipping")
                    continue

                success = self._place_buy(cand, shares)
                if success:
                    buys_this_cycle += 1
                    summary.total_exposure += order_cost
                    summary.open_positions += 1
                    held_tokens.add(cand.token.token_id)

        # 8. Emit updated state
        self._emit_state()
        self.last_scan_time.emit(_now())
        logger.info("=== Scan cycle end === (buys=%d)", buys_this_cycle)

    def _monitor_positions(self, positions):
        """Update mark prices and trigger exit ladder sells."""
        for pos in positions:
            if not self._running:
                break
            try:
                book = fetch_order_book(pos.token_id)
                self._pnl.update_mark_prices(pos.token_id, book, self.is_paper)

                refreshed = self._db.get_position_by_token(pos.token_id, self.is_paper)
                if not refreshed or refreshed.shares <= 0:
                    continue

                triggers = self._pnl.check_exit_rungs(
                    refreshed,
                    self._settings.exit_multiples,
                    self._settings.exit_fractions,
                )
                for rung_idx, fraction, sell_shares in triggers:
                    sell_price = book.best_bid if book.best_bid else refreshed.current_mark
                    if sell_price and sell_price > 0 and sell_shares > 0:
                        self._place_sell(refreshed, sell_price, sell_shares, rung_idx)
                        self._pnl.advance_exit_rung(
                            pos.token_id, rung_idx + 1, self.is_paper
                        )

                time.sleep(0.05)
            except Exception as exc:
                logger.error("Error monitoring %s: %s", pos.token_id[:12], exc)

    def _check_paper_fills(self, open_orders):
        """Check if any resting paper orders can be filled against the current book."""
        for order in open_orders:
            if not self._running:
                break
            if order.status != "OPEN":
                continue
            try:
                book = fetch_order_book(order.token_id)
                filled, fill_price, fill_qty = self._paper_adapter.check_resting_order_fill(
                    order.side, order.price, order.remaining_size,
                    book.best_bid, book.best_ask,
                    book.best_bid_size, book.best_ask_size,
                )
                if filled and fill_qty > 0:
                    self._process_fill(order, fill_price, fill_qty)
                time.sleep(0.05)
            except Exception as exc:
                logger.error("Error checking paper fill %s: %s", order.order_id, exc)

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

    def _place_buy(self, cand, shares: float) -> bool:
        """Place a buy order (paper or live)."""
        price = cand.ask_price
        token_id = cand.token.token_id

        if self._db.has_open_order_for_token(token_id, "BUY", self.is_paper):
            logger.debug("Duplicate buy order guard for %s", token_id[:12])
            return False

        if self.is_paper:
            order_id = self._paper_adapter.generate_order_id()
            filled, fill_price, fill_qty = self._paper_adapter.simulate_fill(
                "BUY", price, shares,
                cand.book.best_bid, cand.book.best_ask,
                cand.book.best_bid_size, cand.book.best_ask_size,
            )

            order = OpenOrder(
                order_id=order_id, token_id=token_id,
                condition_id=cand.market.condition_id,
                market_question=cand.market.question,
                side="BUY", price=price, size=shares,
                remaining_size=shares if not filled else max(0, shares - fill_qty),
                status="FILLED" if (filled and fill_qty >= shares) else "OPEN",
                post_only=self._settings.use_post_only,
                is_paper=True,
            )
            self._db.insert_order(order)

            if filled and fill_qty > 0:
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
                if fill_qty >= shares:
                    self._db.update_order_status(order_id, "FILLED", 0)
                else:
                    self._db.update_order_status(order_id, "OPEN", shares - fill_qty)
                self.last_order_time.emit(_now())

            logger.info(
                "Paper BUY %s | %.4f x %.2f | fill=%s | score=%.1f",
                cand.market.question[:40], price, shares,
                "YES" if filled else "resting", cand.score,
            )
            return True

        else:
            if not self._live_adapter or not self._live_adapter.is_ready:
                logger.error("Live adapter not ready")
                return False
            order_id = self._live_adapter.place_limit_order(
                token_id, "BUY", price, shares,
                post_only=self._settings.use_post_only,
            )
            if order_id:
                order = OpenOrder(
                    order_id=order_id, token_id=token_id,
                    condition_id=cand.market.condition_id,
                    market_question=cand.market.question,
                    side="BUY", price=price, size=shares,
                    remaining_size=shares, status="OPEN",
                    post_only=self._settings.use_post_only,
                    is_paper=False,
                )
                self._db.insert_order(order)
                self._db.insert_trade(TradeRecord(
                    timestamp=_now(), action="BUY",
                    market_question=cand.market.question,
                    outcome=cand.token.outcome, token_id=token_id,
                    price=price, size=shares,
                    gross_value=price * shares,
                    notes="Live order placed",
                    is_paper=False,
                ))
                self.last_order_time.emit(_now())
                logger.info("Live BUY placed: %s @ %.4f x %.2f => %s",
                            cand.market.question[:40], price, shares, order_id)
                return True
            return False

    def _place_sell(self, pos: Position, price: float, shares: float, rung_idx: int):
        """Place a sell order for exit ladder."""
        token_id = pos.token_id

        if self.is_paper:
            order_id = self._paper_adapter.generate_order_id()
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
                logger.info("Paper SELL resting: %s @ %.4f x %.2f",
                            pos.market_question[:40], price, shares)
        else:
            if self._live_adapter and self._live_adapter.is_ready:
                order_id = self._live_adapter.place_limit_order(
                    token_id, "SELL", price, shares,
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
                        notes=f"Live exit rung {rung_idx + 1}",
                        is_paper=False,
                    ))
                    logger.info("Live SELL placed: %s @ %.4f x %.2f", token_id[:12], price, shares)

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

        new_remaining = max(0, order.remaining_size - fill_qty)
        new_status = "FILLED" if new_remaining <= 0 else "OPEN"
        self._db.update_order_status(order.order_id, new_status, new_remaining)
        logger.info("Fill processed: %s %s @ %.4f x %.2f",
                     order.side, order.order_id, fill_price, fill_qty)
        self.last_order_time.emit(_now())

    def _execute_kill_switch(self):
        """Emergency: cancel everything and stop."""
        logger.warning("Executing kill switch — cancelling all orders")
        self.cancel_all_orders()
        self.state_changed.emit(BotState.STOPPED)

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
