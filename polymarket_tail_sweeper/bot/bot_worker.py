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
from typing import Optional, Dict

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


def _parse_dt(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


class BotWorker(QThread):
    """Background worker thread for the scanning/trading loop."""

    # Signals for GUI updates
    state_changed = Signal(str)
    pnl_updated = Signal(object)
    positions_updated = Signal(list)
    orders_updated = Signal(list)
    trades_updated = Signal(list)
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
        self._running = False

    def kill_switch(self):
        self._kill_switch = True
        self._running = False
        logger.warning("KILL SWITCH activated")

    def reload_markets(self):
        self._markets_cache = []
        self._markets_last_refresh = 0.0
        logger.info("Markets cache cleared; will reload on next cycle")

    def cancel_all_orders(self):
        if self.is_paper:
            self._db.cancel_all_open_orders(is_paper=True)
            logger.info("Cancelled all paper orders")
        else:
            if self._live_adapter and self._live_adapter.is_ready:
                self._live_adapter.cancel_all_orders()
            self._db.cancel_all_open_orders(is_paper=False)
            logger.info("Cancelled all live orders")
        self.orders_updated.emit(self._db.get_open_orders(self.is_paper))

    # ==================================================================
    # Sell All at Market
    # ==================================================================
    def sell_all_at_market(self):
        """
        Emergency liquidation: sell all held positions at executable best bid.
        Works in both paper and live mode. Can be called when bot is stopped.
        """
        is_paper = self.is_paper
        positions = self._db.get_positions(is_paper)
        if not positions:
            logger.info("Sell-all: no positions to liquidate")
            return

        logger.warning("SELL ALL AT MARKET: liquidating %d positions", len(positions))

        # Cancel any existing open SELL orders first
        for pos in positions:
            self._db.cancel_open_sells_for_token(pos.token_id, is_paper)
            if not is_paper and self._live_adapter and self._live_adapter.is_ready:
                for o in self._db.get_open_orders(is_paper):
                    if o.token_id == pos.token_id and o.side == "SELL":
                        self._live_adapter.cancel_order(o.order_id)

        token_ids = [p.token_id for p in positions]
        books = fetch_multiple_order_books(token_ids)

        for pos in positions:
            try:
                book = books.get(pos.token_id)
                if not book or book.best_bid is None or book.best_bid <= 0:
                    logger.warning("Sell-all skipped %s: no executable bid",
                                   pos.token_id[:12])
                    continue

                sell_price = book.best_bid
                sell_shares = pos.shares
                if sell_shares <= 0:
                    continue

                if is_paper:
                    order_id = self._paper_adapter.generate_order_id()
                    filled, fp, fq = self._paper_adapter.simulate_fill(
                        "SELL", sell_price, sell_shares,
                        book.best_bid, book.best_ask,
                        book.best_bid_size, book.best_ask_size,
                    )
                    actual_qty = fq if filled and fq > 0 else sell_shares
                    actual_price = fp if filled else sell_price
                    realized = self._pnl.record_sell(
                        pos.token_id, actual_price, actual_qty, is_paper=True,
                    )
                    self._db.insert_trade(TradeRecord(
                        timestamp=_now(), action="SELL",
                        market_question=pos.market_question,
                        outcome=pos.outcome, token_id=pos.token_id,
                        price=actual_price, size=actual_qty,
                        gross_value=actual_price * actual_qty,
                        realized_pnl=realized,
                        notes="Sell-all market liquidation",
                        is_paper=True,
                    ))
                    self._db.insert_order(OpenOrder(
                        order_id=order_id, token_id=pos.token_id,
                        condition_id=pos.condition_id,
                        market_question=pos.market_question,
                        side="SELL", price=actual_price, size=actual_qty,
                        remaining_size=0, status="FILLED",
                        is_paper=True, order_tag="SELL_ALL",
                    ))
                    logger.info("Sell-all paper: %s @ %.4f x %.2f, PnL=%.4f",
                                pos.token_id[:12], actual_price, actual_qty, realized)
                else:
                    if self._live_adapter and self._live_adapter.is_ready:
                        oid = self._live_adapter.place_limit_order(
                            pos.token_id, "SELL", sell_price, sell_shares,
                            post_only=False, book=book,
                        )
                        if oid:
                            self._db.insert_order(OpenOrder(
                                order_id=oid, token_id=pos.token_id,
                                condition_id=pos.condition_id,
                                market_question=pos.market_question,
                                side="SELL", price=sell_price, size=sell_shares,
                                remaining_size=sell_shares, status="OPEN",
                                is_paper=False, order_tag="SELL_ALL",
                            ))
                            self._db.insert_trade(TradeRecord(
                                timestamp=_now(), action="SELL",
                                market_question=pos.market_question,
                                outcome=pos.outcome, token_id=pos.token_id,
                                price=sell_price, size=sell_shares,
                                gross_value=sell_price * sell_shares,
                                notes="Sell-all live (resting)",
                                is_paper=False,
                            ))
                            logger.info("Sell-all live: %s @ %.4f x %.2f => %s",
                                        pos.token_id[:12], sell_price, sell_shares, oid)
                    else:
                        logger.error("Sell-all: live adapter not ready for %s",
                                     pos.token_id[:12])
            except Exception as exc:
                logger.error("Sell-all error for %s: %s", pos.token_id[:12], exc)

    # ==================================================================
    # Main run loop
    # ==================================================================
    def run(self):
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
            if self._settings.live_sync_on_start:
                self._sync_live_account_state()

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
        for _ in range(seconds * 2):
            if not self._running or self._kill_switch:
                break
            time.sleep(0.5)

    def _init_live_trading(self) -> bool:
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
    # Live account state sync (Objective B)
    # ==================================================================
    def _sync_live_account_state(self):
        """
        Reconcile local DB against actual exchange/wallet state.
        Run on startup and optionally during idle.
        """
        if not self._live_adapter or not self._live_adapter.is_ready:
            return

        logger.info("=== Live account sync starting ===")

        # 1. Sync open orders
        self._reconcile_live_orders_full()

        # 2. Sync wallet positions
        wallet = self._live_adapter.get_wallet_positions()
        if not wallet:
            logger.info("Live sync: wallet positions fetch returned empty (may be unsupported)")
        else:
            self._sync_positions_to_wallet(wallet)

        self._emit_state()
        logger.info("=== Live account sync complete ===")

    def _reconcile_live_orders_full(self):
        """Enhanced reconciliation that handles missing orders more carefully."""
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
                # Order not found — could be filled offline or cancelled
                # Try individual lookup before deciding
                raw = self._live_adapter.get_order(local.order_id)
                if raw:
                    from adapters.polymarket_trade import LiveTradingAdapter as LTA
                    ex = LTA._normalize_exchange_order(raw)
                else:
                    # Truly gone: mark as terminal but log as ambiguous
                    logger.warning(
                        "Order %s (%s) not found on exchange after restart. "
                        "Marking FILLED (assumed offline fill). "
                        "Wallet sync will correct position if wrong.",
                        local.order_id, local.side,
                    )
                    fill_delta = local.remaining_size
                    if fill_delta > 0.001:
                        self._process_fill(local, local.price, fill_delta)
                    self._db.update_order_status(local.order_id, "FILLED", 0)
                    continue

            # Normal reconciliation
            local_remaining = local.remaining_size
            ex_remaining = ex.remaining_size
            fill_delta = local_remaining - ex_remaining

            if fill_delta > 0.001:
                logger.info(
                    "Reconciled fill: %s %s delta=%.4f (local=%.4f, ex=%.4f)",
                    local.side, local.order_id, fill_delta, local_remaining, ex_remaining,
                )
                self._process_fill(local, local.price, fill_delta)

            new_status = ex.status
            if new_status in ("FILLED", "CANCELLED", "EXPIRED"):
                self._db.update_order_status(local.order_id, new_status, ex_remaining)
            elif new_status in ("PARTIAL", "OPEN"):
                self._db.update_order_status(local.order_id, "OPEN", ex_remaining)

    def _sync_positions_to_wallet(self, wallet: Dict[str, float]):
        """
        Sync local positions to match actual wallet holdings.
        Adjusts shares to reality; logs adjustments clearly.
        """
        local_positions = self._db.get_positions(is_paper=False)
        local_map = {p.token_id: p for p in local_positions}
        all_tokens = set(wallet.keys()) | set(local_map.keys())

        for token_id in all_tokens:
            local = local_map.get(token_id)
            wallet_shares = wallet.get(token_id, 0.0)
            local_shares = local.shares if local else 0.0

            if abs(wallet_shares - local_shares) < 0.01:
                continue

            if wallet_shares <= 0 and local_shares > 0:
                logger.warning(
                    "Live sync: removing phantom position %s (DB=%.2f, wallet=0)",
                    token_id[:12], local_shares,
                )
                self._db.delete_position(token_id, is_paper=False)
                self._db.insert_event("WARNING",
                    f"Position {token_id[:12]} removed during live sync "
                    f"(was {local_shares:.2f} shares, wallet shows 0)")

            elif wallet_shares > 0 and local_shares <= 0:
                logger.warning(
                    "Live sync: importing external position %s (wallet=%.2f)",
                    token_id[:12], wallet_shares,
                )
                pos = Position(
                    token_id=token_id,
                    condition_id="",
                    market_question="Imported from wallet sync",
                    outcome="",
                    shares=wallet_shares,
                    avg_entry=0.0,
                    cost_basis=0.0,
                    is_paper=False,
                )
                self._db.upsert_position(pos)
                self._db.insert_event("INFO",
                    f"Position {token_id[:12]} imported during live sync "
                    f"({wallet_shares:.2f} shares, avg_entry unknown)")

            else:
                logger.warning(
                    "Live sync: adjusting %s shares from %.2f to %.2f",
                    token_id[:12], local_shares, wallet_shares,
                )
                local.shares = wallet_shares
                if local.avg_entry > 0:
                    local.cost_basis = local.avg_entry * wallet_shares
                self._db.upsert_position(local)
                self._db.insert_event("INFO",
                    f"Position {token_id[:12]} adjusted during live sync "
                    f"({local_shares:.2f} -> {wallet_shares:.2f})")

    # ==================================================================
    # Main cycle
    # ==================================================================
    def _run_cycle(self):
        logger.info("=== Scan cycle start ===")
        clear_book_cache()

        api_ok = check_api_connectivity()
        self.api_status.emit(api_ok)
        if not api_ok:
            logger.warning("API not reachable — skipping cycle")
            return

        # 1. Refresh markets
        self._maybe_refresh_markets()
        if not self._markets_cache:
            logger.warning("No markets available")
            return

        # 2. Reconcile orders
        if self.is_paper:
            self._check_paper_fills()
        else:
            self._reconcile_live_orders_full()

        # 3. Cancel stale orders
        self._cancel_stale_orders()

        # 4. Inventory management — time-based position aging
        self._manage_aging_inventory()

        # 5. Monitor positions — update marks and check exit triggers
        positions = self._db.get_positions(self.is_paper)
        self._monitor_positions(positions)

        # 6. Populate strategy memory
        self._update_strategy_memory()

        # 7. Build committed-capital snapshot for entry gating
        summary = self._db.build_pnl_summary(self.is_paper)
        committed = summary.total_exposure + summary.cash_reserved

        # 8. Filter and scan for entries
        filtered = self._strategy.filter_markets(self._markets_cache)

        open_orders = self._db.get_open_orders(self.is_paper)
        held_tokens = {p.token_id for p in self._db.get_positions(self.is_paper)}
        open_buy_tokens = {o.token_id for o in open_orders if o.side == "BUY"}

        buys_this_cycle = 0
        cycle_reserved = 0.0

        for market in filtered:
            if not self._running:
                break
            if not self._strategy.should_enter(
                committed + cycle_reserved, len(held_tokens), buys_this_cycle,
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
                    committed + cycle_reserved, len(held_tokens), buys_this_cycle,
                ):
                    break
                if not self._strategy.price_guard(cand.ask_price):
                    continue

                shares = self._strategy.compute_order_size(cand.ask_price)
                if shares <= 0:
                    continue

                order_cost = cand.ask_price * shares
                if committed + cycle_reserved + order_cost > self._settings.max_total_exposure:
                    continue

                filled_shares = self._place_buy(cand, shares)
                if filled_shares is not None:
                    buys_this_cycle += 1
                    open_buy_tokens.add(cand.token.token_id)
                    if filled_shares > 0:
                        held_tokens.add(cand.token.token_id)
                        committed += cand.ask_price * filled_shares
                        resting = shares - filled_shares
                        if resting > 0:
                            cycle_reserved += cand.ask_price * resting
                    else:
                        cycle_reserved += order_cost

        # 9. Entry repricing
        if self._settings.entry_reprice_enabled:
            self._reprice_stale_entries()

        # 10. Emit state
        self._emit_state()
        self.last_scan_time.emit(_now())
        logger.info("=== Scan cycle end === (buys=%d)", buys_this_cycle)

    # ==================================================================
    # Strategy memory
    # ==================================================================
    def _update_strategy_memory(self):
        """Feed the strategy with recent winner data and position counts."""
        hours = self._settings.recent_winner_boost_hours
        winners = set(self._db.get_recent_profitable_tokens(hours, self.is_paper))
        self._strategy.set_recent_winners(winners)

        positions = self._db.get_positions(self.is_paper)
        counts: Dict[str, int] = {}
        for p in positions:
            cid = p.condition_id
            if cid:
                counts[cid] = counts.get(cid, 0) + 1
        self._strategy.set_position_condition_counts(counts)

    # ==================================================================
    # Inventory management
    # ==================================================================
    def _manage_aging_inventory(self):
        """Time-based position management: tighten exits on old positions."""
        positions = self._db.get_positions(self.is_paper)
        now_dt = datetime.now(timezone.utc)

        for pos in positions:
            if not self._running:
                break

            created = _parse_dt(pos.created_at)
            if not created:
                continue
            age_min = (now_dt - created).total_seconds() / 60.0

            # Skip if there's already an open sell
            if self._db.has_open_order_for_token(pos.token_id, "SELL", self.is_paper):
                continue

            # Breakeven unwind: position held beyond threshold, try to exit near breakeven
            if age_min >= self._settings.breakeven_unwind_minutes:
                book = fetch_order_book(pos.token_id)
                if book.best_bid and book.best_bid > 0:
                    min_price = pos.avg_entry
                    if self._settings.allow_small_forced_unwind_loss:
                        min_price = pos.avg_entry * 0.95
                    if book.best_bid >= min_price:
                        logger.info(
                            "Inventory mgmt: unwinding aged position %s (%.0f min, bid=%.4f, avg=%.4f)",
                            pos.token_id[:12], age_min, book.best_bid, pos.avg_entry,
                        )
                        self._place_sell(pos, book.best_bid, pos.shares, -1, book,
                                         order_tag="BREAKEVEN_UNWIND")
                    else:
                        logger.debug(
                            "Inventory mgmt: aged position %s bid too low for unwind (%.4f < %.4f)",
                            pos.token_id[:12], book.best_bid, min_price,
                        )

            # No-progress: if position hasn't hit any rung after threshold, convert passive to aggressive
            elif age_min >= self._settings.no_progress_minutes and pos.next_exit_rung == 0:
                logger.debug(
                    "Inventory mgmt: no-progress position %s (%.0f min)",
                    pos.token_id[:12], age_min,
                )

    # ==================================================================
    # Entry repricing
    # ==================================================================
    def _reprice_stale_entries(self):
        """Cancel and replace resting buy orders that have become uncompetitive."""
        if not self._settings.entry_reprice_enabled:
            return

        open_buys = [o for o in self._db.get_open_orders(self.is_paper) if o.side == "BUY"]
        interval = self._settings.entry_reprice_interval_sec

        for order in open_buys:
            if not self._running:
                break
            created = _parse_dt(order.created_at)
            if not created:
                continue
            age_sec = (datetime.now(timezone.utc) - created).total_seconds()
            if age_sec < interval:
                continue

            # Check if we've already repriced too many times (use notes as counter)
            # Simple approach: just cancel stale entries older than reprice_interval * (max_reprices + 1)
            max_age = interval * (self._settings.entry_max_reprices + 1)
            if age_sec > max_age:
                logger.info("Entry reprice: cancelling exhausted order %s (age=%ds)",
                            order.order_id, int(age_sec))
                self._db.update_order_status(order.order_id, "CANCELLED")
                if not self.is_paper and self._live_adapter:
                    self._live_adapter.cancel_order(order.order_id)
                continue

            # Could reprice here — for now just cancel old ones
            logger.debug("Entry reprice: order %s aged %ds, eligible for reprice",
                         order.order_id, int(age_sec))

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
    # Paper fills for resting orders
    # ==================================================================
    def _check_paper_fills(self):
        open_orders = self._db.get_open_orders(is_paper=True)
        if not open_orders:
            return

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
        if not positions:
            return

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

                if self._db.has_open_order_for_token(pos.token_id, "SELL", self.is_paper):
                    logger.debug("Exit skipped: existing open sell for %s", pos.token_id[:12])
                    continue

                best_bid = book.best_bid
                best_ask = book.best_ask
                mid = book.midpoint

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
                    rung_idx, rung_multiple, target_price, trigger_price, min_exit,
                )

                if best_bid is None or best_bid < min_exit:
                    logger.info(
                        "Exit blocked: bid (%.4f) below breakeven+buffer (%.4f) for %s",
                        best_bid or 0.0, min_exit, pos.token_id[:12],
                    )
                    continue

                if self._settings.exit_order_mode == "passive":
                    sell_price = mid if (mid is not None and mid > best_bid) else best_bid
                    if sell_price < min_exit:
                        sell_price = min_exit
                    logger.info("Exit deferred: passive at %.4f for %s",
                                sell_price, pos.token_id[:12])
                else:
                    sell_price = best_bid
                    logger.info("Exit placed: aggressive at %.4f for %s",
                                sell_price, pos.token_id[:12])

                if sell_price > 0 and sell_shares > 0:
                    self._place_sell(refreshed, sell_price, sell_shares, rung_idx, book,
                                     order_tag=f"EXIT_RUNG:{rung_idx}")

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
            is_paper=True, order_tag="ENTRY",
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
            is_paper=False, order_tag="ENTRY",
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
        return 0.0

    # ==================================================================
    # Sell placement
    # ==================================================================
    def _place_sell(self, pos: Position, price: float, shares: float,
                    rung_idx: int, book=None, order_tag: str = ""):
        """Place a sell order. order_tag persists intent for later rung resolution."""
        token_id = pos.token_id
        tag = order_tag or (f"EXIT_RUNG:{rung_idx}" if rung_idx >= 0 else "MANUAL")

        if self.is_paper:
            self._place_sell_paper(pos, price, shares, rung_idx, book, tag)
        else:
            self._place_sell_live(pos, price, shares, rung_idx, book, tag)

    def _place_sell_paper(self, pos, price, shares, rung_idx, book, tag):
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
                notes=f"Exit rung {rung_idx + 1}" if rung_idx >= 0 else tag,
                is_paper=True,
            ))
            self._db.insert_order(OpenOrder(
                order_id=order_id, token_id=token_id,
                condition_id=pos.condition_id,
                market_question=pos.market_question,
                side="SELL", price=fill_price, size=fill_qty,
                remaining_size=0, status="FILLED",
                is_paper=True, order_tag=tag,
            ))
            if fill_qty >= shares and rung_idx >= 0 and tag.startswith("EXIT_RUNG"):
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
                is_paper=True, order_tag=tag,
            ))
            logger.info("Paper SELL resting: %s @ %.4f x %.2f (tag=%s)",
                         pos.market_question[:40], price, shares, tag)

    def _place_sell_live(self, pos, price, shares, rung_idx, book, tag):
        token_id = pos.token_id
        if not self._live_adapter or not self._live_adapter.is_ready:
            return

        order_id = self._live_adapter.place_limit_order(
            token_id, "SELL", price, shares,
            post_only=False, book=book,
        )
        if order_id:
            self._db.insert_order(OpenOrder(
                order_id=order_id, token_id=token_id,
                condition_id=pos.condition_id,
                market_question=pos.market_question,
                side="SELL", price=price, size=shares,
                remaining_size=shares, status="OPEN",
                is_paper=False, order_tag=tag,
            ))
            self._db.insert_trade(TradeRecord(
                timestamp=_now(), action="SELL",
                market_question=pos.market_question,
                outcome=pos.outcome, token_id=pos.token_id,
                price=price, size=shares,
                gross_value=price * shares,
                notes=f"Live exit (tag={tag}, resting)",
                is_paper=False,
            ))
            logger.info("Live SELL placed: %s @ %.4f x %.2f => %s (tag=%s)",
                         token_id[:12], price, shares, order_id, tag)

    # ==================================================================
    # Fill processing
    # ==================================================================
    def _process_fill(self, order: OpenOrder, fill_price: float, fill_qty: float):
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
            # Only advance exit rung if this was an EXIT_RUNG order
            tag = order.order_tag or self._db.get_order_tag(order.order_id)
            if tag.startswith("EXIT_RUNG:"):
                try:
                    intended_rung = int(tag.split(":")[1])
                    new_remaining = max(0, order.remaining_size - fill_qty)
                    if new_remaining <= 0.01:
                        self._pnl.advance_exit_rung(
                            order.token_id, intended_rung + 1, self.is_paper,
                        )
                        logger.info("Exit rung %d advanced for %s (full fill)",
                                    intended_rung, order.token_id[:12])
                    else:
                        logger.info("Exit rung %d partial fill for %s (%.2f remaining)",
                                    intended_rung, order.token_id[:12], new_remaining)
                except (ValueError, IndexError):
                    pass

        new_remaining = max(0, order.remaining_size - fill_qty)
        new_status = "FILLED" if new_remaining <= 0 else "OPEN"
        self._db.update_order_status(order.order_id, new_status, new_remaining)
        logger.info("Fill processed: %s %s @ %.4f x %.2f",
                     order.side, order.order_id, fill_price, fill_qty)
        self.last_order_time.emit(_now())

    # ==================================================================
    # Kill switch + state emission
    # ==================================================================
    def _execute_kill_switch(self):
        logger.warning("Executing kill switch — cancelling all orders")
        self.cancel_all_orders()
        self.state_changed.emit(BotState.STOPPED)

    def _emit_state(self):
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
