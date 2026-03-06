"""
Background bot worker.
Runs a SCAN → FARM → rescan cycle in a QThread.
SCAN discovers active markets; FARM repeatedly trades them.
"""
from __future__ import annotations

import time
import traceback
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Dict, Set

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
from utils.pricing import (
    normalize_price, infer_tick_size, DEFAULT_TICK,
    clamp_price, round_down_to_tick,
)

logger = logging.getLogger("tailsweeper.worker")

MODE_SCAN = "SCAN"
MODE_FARM = "FARM"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _mono() -> float:
    return time.monotonic()


# ======================================================================
# Farm token metadata (in-memory only)
# ======================================================================
class _FarmEntry:
    __slots__ = (
        "token_id", "condition_id", "last_seen_ts", "last_fill_ts",
        "last_profit_exit_ts", "consecutive_bad_cycles",
    )

    def __init__(self, token_id: str, condition_id: str = ""):
        self.token_id = token_id
        self.condition_id = condition_id
        self.last_seen_ts = _mono()
        self.last_fill_ts = _mono()
        self.last_profit_exit_ts = 0.0
        self.consecutive_bad_cycles = 0

    def touch(self, reason: str = ""):
        self.last_seen_ts = _mono()
        if "fill" in reason:
            self.last_fill_ts = _mono()
            self.consecutive_bad_cycles = 0
        if "profit" in reason:
            self.last_profit_exit_ts = _mono()

    def age_since_fill_min(self) -> float:
        return (_mono() - self.last_fill_ts) / 60.0


class BotWorker(QThread):
    """Background worker thread for the scanning/trading loop."""

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

        # SCAN/FARM state machine
        self._mode = MODE_SCAN
        self._mode_started_ts: float = 0.0
        self._last_rescan_ts: float = 0.0

        # Farm list (in-memory)
        self._farm: Dict[str, _FarmEntry] = {}

        # Fill tracking for rescan triggers
        self._recent_fills: deque = deque()  # timestamps (monotonic)

        # Repricing tracking: order_id -> (last_reprice_ts, reprice_count)
        self._reprice_state: Dict[str, tuple] = {}

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
    # Farm list management
    # ==================================================================
    def _farm_touch(self, token_id: str, condition_id: str = "", reason: str = ""):
        entry = self._farm.get(token_id)
        if entry:
            entry.touch(reason)
            if condition_id:
                entry.condition_id = condition_id
        else:
            entry = _FarmEntry(token_id, condition_id)
            entry.touch(reason)
            self._farm[token_id] = entry
            logger.info("Farm +%s (reason=%s, farm_size=%d)",
                        token_id[:12], reason, len(self._farm))

    def _farm_prune(self):
        """Remove stale or illiquid farm tokens."""
        ttl = self._settings.farm_token_ttl_minutes
        bad_limit = self._settings.farm_prune_after_bad_cycles
        to_remove = []
        for tid, fe in self._farm.items():
            if fe.age_since_fill_min() > ttl:
                to_remove.append(tid)
            elif fe.consecutive_bad_cycles >= bad_limit:
                to_remove.append(tid)
        for tid in to_remove:
            del self._farm[tid]
            logger.info("Farm -%s (pruned, farm_size=%d)", tid[:12], len(self._farm))

    def _farm_active_count(self) -> int:
        return len(self._farm)

    def _record_fill_event(self):
        self._recent_fills.append(_mono())

    def _fills_in_window(self) -> int:
        cutoff = _mono() - self._settings.rescan_fill_window_minutes * 60
        while self._recent_fills and self._recent_fills[0] < cutoff:
            self._recent_fills.popleft()
        return len(self._recent_fills)

    # ==================================================================
    # Mode transitions
    # ==================================================================
    def _switch_mode(self, new_mode: str, reason: str):
        old = self._mode
        self._mode = new_mode
        self._mode_started_ts = _mono()
        if new_mode == MODE_SCAN:
            self._last_rescan_ts = _mono()
        logger.warning("Mode: %s -> %s (reason=%s, farm_size=%d, fills_window=%d)",
                       old, new_mode, reason,
                       self._farm_active_count(), self._fills_in_window())

    def _should_rescan(self) -> tuple:
        """Return (should_rescan: bool, reason: str)."""
        s = self._settings
        now = _mono()
        farm_age = (now - self._mode_started_ts) / 60.0

        if self._farm_active_count() < s.rescan_if_farm_size_below:
            return True, f"low_farm_size({self._farm_active_count()}<{s.rescan_if_farm_size_below})"

        if self._fills_in_window() < s.rescan_if_fill_rate_below:
            return True, f"low_fills({self._fills_in_window()}<{s.rescan_if_fill_rate_below})"

        if farm_age >= s.farm_phase_max_minutes:
            return True, f"farm_max_time({farm_age:.0f}min)"

        if s.rescan_every_minutes > 0:
            since_last = (now - self._last_rescan_ts) / 60.0
            if since_last >= s.rescan_every_minutes:
                return True, f"periodic({since_last:.0f}min)"

        return False, ""

    def _scan_burst_expired(self) -> bool:
        age = _mono() - self._mode_started_ts
        return age >= self._settings.scan_burst_duration_sec

    # ==================================================================
    # Sell All at Market (unchanged)
    # ==================================================================
    def sell_all_at_market(self):
        is_paper = self.is_paper
        positions = self._db.get_positions(is_paper)
        if not positions:
            logger.info("Sell-all: no positions to liquidate")
            return
        logger.warning("SELL ALL AT MARKET: liquidating %d positions", len(positions))
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
                    logger.warning("Sell-all skipped %s: no bid", pos.token_id[:12])
                    continue
                sell_price = book.best_bid
                sell_shares = pos.shares
                if sell_shares <= 0:
                    continue
                if is_paper:
                    oid = self._paper_adapter.generate_order_id()
                    filled, fp, fq = self._paper_adapter.simulate_fill(
                        "SELL", sell_price, sell_shares,
                        book.best_bid, book.best_ask, book.best_bid_size, book.best_ask_size)
                    aq = fq if filled and fq > 0 else sell_shares
                    ap = fp if filled else sell_price
                    realized = self._pnl.record_sell(pos.token_id, ap, aq, is_paper=True)
                    self._db.insert_trade(TradeRecord(
                        timestamp=_now(), action="SELL", market_question=pos.market_question,
                        outcome=pos.outcome, token_id=pos.token_id, price=ap, size=aq,
                        gross_value=ap * aq, realized_pnl=realized,
                        notes="Sell-all market liquidation", is_paper=True))
                    self._db.insert_order(OpenOrder(
                        order_id=oid, token_id=pos.token_id, condition_id=pos.condition_id,
                        market_question=pos.market_question, side="SELL", price=ap, size=aq,
                        remaining_size=0, status="FILLED", is_paper=True, order_tag="SELL_ALL"))
                    logger.info("Sell-all paper: %s @ %.4f x %.2f PnL=%.4f",
                                pos.token_id[:12], ap, aq, realized)
                else:
                    if self._live_adapter and self._live_adapter.is_ready:
                        oid = self._live_adapter.place_limit_order(
                            pos.token_id, "SELL", sell_price, sell_shares,
                            post_only=False, book=book)
                        if oid:
                            self._db.insert_order(OpenOrder(
                                order_id=oid, token_id=pos.token_id, condition_id=pos.condition_id,
                                market_question=pos.market_question, side="SELL", price=sell_price,
                                size=sell_shares, remaining_size=sell_shares, status="OPEN",
                                is_paper=False, order_tag="SELL_ALL"))
                            self._db.insert_trade(TradeRecord(
                                timestamp=_now(), action="SELL", market_question=pos.market_question,
                                outcome=pos.outcome, token_id=pos.token_id, price=sell_price,
                                size=sell_shares, gross_value=sell_price * sell_shares,
                                notes="Sell-all live (resting)", is_paper=False))
                            logger.info("Sell-all live: %s @ %.4f x %.2f => %s",
                                        pos.token_id[:12], sell_price, sell_shares, oid)
            except Exception as exc:
                logger.error("Sell-all error for %s: %s", pos.token_id[:12], exc)

    # ==================================================================
    # Main run loop
    # ==================================================================
    def run(self):
        self._running = True
        self._kill_switch = False
        self._consecutive_errors = 0
        self._mode = MODE_SCAN
        self._mode_started_ts = _mono()
        self._last_rescan_ts = _mono()

        mode = "PAPER" if self.is_paper else "LIVE"
        logger.info("Bot starting in %s mode", mode)
        self.state_changed.emit(BotState.PAPER if self.is_paper else BotState.LIVE)

        if not self.is_paper:
            if not self._init_live_trading():
                self.state_changed.emit(BotState.ERROR)
                return
            if self._settings.live_sync_on_start:
                self._sync_live_account_state()

        # Seed farm from existing positions
        for p in self._db.get_positions(self.is_paper):
            self._farm_touch(p.token_id, p.condition_id, reason="startup_position")

        while self._running and not self._kill_switch:
            try:
                self._run_cycle()
                self._consecutive_errors = 0
            except Exception as exc:
                self._consecutive_errors += 1
                logger.error("Cycle error (%d/%d): %s\n%s",
                             self._consecutive_errors, self._max_consecutive_errors,
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
            logger.error("Geoblock detected")
            self.error_signal.emit("Geoblocked")
            return False
        self._live_adapter = LiveTradingAdapter(self._settings)
        if not self._live_adapter.initialize():
            self.error_signal.emit("Failed to init live adapter")
            return False
        return True

    # ==================================================================
    # Live account sync (unchanged)
    # ==================================================================
    def _sync_live_account_state(self):
        if not self._live_adapter or not self._live_adapter.is_ready:
            return
        logger.info("=== Live account sync starting ===")
        self._reconcile_live_orders_full()
        wallet = self._live_adapter.get_wallet_positions()
        if wallet:
            self._sync_positions_to_wallet(wallet)
        self._emit_state()
        logger.info("=== Live account sync complete ===")

    def _reconcile_live_orders_full(self):
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
                raw = self._live_adapter.get_order(local.order_id)
                if raw:
                    from adapters.polymarket_trade import LiveTradingAdapter as LTA
                    ex = LTA._normalize_exchange_order(raw)
                else:
                    logger.warning("Order %s not found — marking FILLED (assumed offline)",
                                   local.order_id)
                    if local.remaining_size > 0.001:
                        self._process_fill(local, local.price, local.remaining_size)
                    self._db.update_order_status(local.order_id, "FILLED", 0)
                    continue
            local_rem = local.remaining_size
            ex_rem = ex.remaining_size
            delta = local_rem - ex_rem
            if delta > 0.001:
                self._process_fill(local, local.price, delta)
            st = ex.status
            if st in ("FILLED", "CANCELLED", "EXPIRED"):
                self._db.update_order_status(local.order_id, st, ex_rem)
            elif st in ("PARTIAL", "OPEN"):
                self._db.update_order_status(local.order_id, "OPEN", ex_rem)

    def _sync_positions_to_wallet(self, wallet: Dict[str, float]):
        local_positions = self._db.get_positions(is_paper=False)
        local_map = {p.token_id: p for p in local_positions}
        for token_id in set(wallet.keys()) | set(local_map.keys()):
            local = local_map.get(token_id)
            ws = wallet.get(token_id, 0.0)
            ls = local.shares if local else 0.0
            if abs(ws - ls) < 0.01:
                continue
            if ws <= 0 and ls > 0:
                self._db.delete_position(token_id, is_paper=False)
                self._db.insert_event("WARNING", f"Removed phantom {token_id[:12]}")
            elif ws > 0 and ls <= 0:
                self._db.upsert_position(Position(
                    token_id=token_id, market_question="Imported", shares=ws, is_paper=False))
                self._db.insert_event("INFO", f"Imported {token_id[:12]} ({ws:.2f})")
            else:
                local.shares = ws
                if local.avg_entry > 0:
                    local.cost_basis = local.avg_entry * ws
                self._db.upsert_position(local)

    # ==================================================================
    # Main cycle — dispatches based on mode
    # ==================================================================
    def _run_cycle(self):
        clear_book_cache()
        api_ok = check_api_connectivity()
        self.api_status.emit(api_ok)
        if not api_ok:
            logger.warning("API not reachable — skipping cycle")
            return

        # Always: reconcile, manage inventory, monitor exits
        if self.is_paper:
            self._check_paper_fills()
        else:
            self._reconcile_live_orders_full()
        self._cancel_stale_orders()
        self._manage_aging_inventory()
        positions = self._db.get_positions(self.is_paper)
        self._monitor_positions(positions)
        self._update_strategy_memory()
        self._farm_prune()

        # Mode-specific entry logic
        if self._mode == MODE_SCAN:
            logger.info("=== SCAN cycle (farm=%d) ===", self._farm_active_count())
            self._maybe_refresh_markets()
            self._run_entry_scan(max_new=self._settings.scan_burst_max_new_orders)
            if self._scan_burst_expired():
                self._switch_mode(MODE_FARM, "burst_complete")
        else:
            logger.info("=== FARM cycle (farm=%d, fills_win=%d) ===",
                        self._farm_active_count(), self._fills_in_window())
            self._run_farm_entries()
            rescan, reason = self._should_rescan()
            if rescan:
                self._switch_mode(MODE_SCAN, reason)

        # Always: reprice entry orders
        if self._settings.entry_reprice_enabled:
            self._reprice_entries()

        self._emit_state()
        self.last_scan_time.emit(_now())

    # ==================================================================
    # SCAN mode: full universe entry scan
    # ==================================================================
    def _run_entry_scan(self, max_new: int):
        self._maybe_refresh_markets()
        if not self._markets_cache:
            return

        filtered = self._strategy.filter_markets(self._markets_cache)
        summary = self._db.build_pnl_summary(self.is_paper)
        committed = summary.total_exposure + summary.cash_reserved
        open_orders = self._db.get_open_orders(self.is_paper)
        held = {p.token_id for p in self._db.get_positions(self.is_paper)}
        open_buys = {o.token_id for o in open_orders if o.side == "BUY"}

        buys = 0
        reserved = 0.0
        for market in filtered:
            if not self._running or buys >= max_new:
                break
            if not self._strategy.should_enter(committed + reserved, len(held), buys):
                break
            tids = [t.token_id for t in market.tokens]
            books = fetch_multiple_order_books(tids)
            cands = self._strategy.find_candidates(market, books, held, open_buys)
            for cand in self._strategy.rank_candidates(cands):
                if not self._running or buys >= max_new:
                    break
                if not self._strategy.should_enter(committed + reserved, len(held), buys):
                    break
                if not self._strategy.price_guard(cand.ask_price):
                    continue
                shares = self._strategy.compute_order_size(cand.ask_price)
                if shares <= 0:
                    continue
                cost = cand.ask_price * shares
                if committed + reserved + cost > self._settings.max_total_exposure:
                    continue
                filled = self._place_buy(cand, shares)
                if filled is not None:
                    buys += 1
                    open_buys.add(cand.token.token_id)
                    if filled > 0:
                        held.add(cand.token.token_id)
                        committed += cand.ask_price * filled
                        rest = shares - filled
                        if rest > 0:
                            reserved += cand.ask_price * rest
                    else:
                        reserved += cost
        logger.info("Scan placed %d new orders", buys)

    # ==================================================================
    # FARM mode: focused entries on farm tokens only
    # ==================================================================
    def _run_farm_entries(self):
        if not self._farm:
            return

        summary = self._db.build_pnl_summary(self.is_paper)
        committed = summary.total_exposure + summary.cash_reserved
        held = {p.token_id for p in self._db.get_positions(self.is_paper)}
        open_orders = self._db.get_open_orders(self.is_paper)
        open_buys = {o.token_id for o in open_orders if o.side == "BUY"}

        farm_tids = [tid for tid in self._farm if tid not in held and tid not in open_buys]
        if not farm_tids:
            return

        books = fetch_multiple_order_books(farm_tids)
        buys = 0
        for tid in farm_tids:
            if not self._running or buys >= self._settings.max_buys_per_cycle:
                break
            if not self._strategy.should_enter(committed, len(held), buys):
                break
            book = books.get(tid)
            if not book or book.best_ask is None or book.best_bid is None:
                fe = self._farm.get(tid)
                if fe:
                    fe.consecutive_bad_cycles += 1
                continue
            if not self._strategy.price_guard(book.best_ask):
                continue
            shares = self._strategy.compute_order_size(book.best_ask)
            if shares <= 0:
                continue
            cost = book.best_ask * shares
            if committed + cost > self._settings.max_total_exposure:
                continue

            # Build a minimal Candidate for _place_buy
            from models.data_models import Market, Token, Candidate
            fe = self._farm[tid]
            dummy_market = Market(condition_id=fe.condition_id, question="Farm re-entry")
            dummy_token = Token(token_id=tid, outcome="")
            cand = Candidate(
                market=dummy_market, token=dummy_token, book=book,
                score=0, ask_price=book.best_ask, ask_size=book.best_ask_size,
                bid_price=book.best_bid or 0, spread=book.spread or 0)
            filled = self._place_buy(cand, shares)
            if filled is not None:
                buys += 1
                open_buys.add(tid)
                committed += cost
        logger.info("Farm placed %d re-entries", buys)

    # ==================================================================
    # Real entry repricing (queue fighting)
    # ==================================================================
    def _reprice_entries(self):
        if not self._settings.entry_reprice_enabled:
            return

        open_buys = [o for o in self._db.get_open_orders(self.is_paper) if o.side == "BUY"]
        now = _mono()
        interval = self._settings.entry_reprice_interval_sec
        max_rp = self._settings.entry_max_reprices

        for order in open_buys:
            if not self._running:
                break

            oid = order.order_id
            state = self._reprice_state.get(oid, (0.0, 0))
            last_rp_ts, rp_count = state

            if rp_count >= max_rp:
                # Exhausted reprices — cancel
                created = _parse_dt(order.created_at)
                max_age = interval * (max_rp + 1)
                if created:
                    age = (datetime.now(timezone.utc) - created).total_seconds()
                    if age > max_age:
                        logger.info("Reprice: cancelling exhausted %s (reprices=%d)",
                                    oid, rp_count)
                        self._db.update_order_status(oid, "CANCELLED")
                        if not self.is_paper and self._live_adapter:
                            self._live_adapter.cancel_order(oid)
                        self._reprice_state.pop(oid, None)
                continue

            if now - last_rp_ts < interval:
                continue

            # Fetch book to evaluate competitiveness
            book = fetch_order_book(order.token_id)
            if not book or book.best_bid is None or book.best_ask is None:
                continue

            best_bid = book.best_bid
            best_ask = book.best_ask
            tick = infer_tick_size(book)

            # Already at or above best bid — competitive
            if order.price >= best_bid:
                continue

            # Compute new price: best_bid + 1 tick (but must remain post-only: < ask)
            new_price = round_down_to_tick(best_bid + tick, tick)
            new_price = clamp_price(new_price)

            if new_price >= best_ask:
                new_price = round_down_to_tick(best_ask - tick, tick)
            if new_price <= 0 or new_price >= best_ask:
                continue
            if new_price > self._settings.max_entry_price:
                continue

            # Check spread quality still OK
            spread_ratio = (best_ask - new_price) / best_ask if best_ask > 0 else 1.0
            if spread_ratio > self._settings.max_spread_ratio:
                logger.debug("Reprice skip %s: spread_ratio %.2f too wide", oid, spread_ratio)
                continue

            # Cancel old order
            self._db.update_order_status(oid, "CANCELLED")
            if not self.is_paper and self._live_adapter:
                self._live_adapter.cancel_order(oid)

            # Place replacement at new_price
            shares = order.remaining_size
            if self.is_paper:
                new_oid = self._paper_adapter.generate_order_id()
                self._db.insert_order(OpenOrder(
                    order_id=new_oid, token_id=order.token_id,
                    condition_id=order.condition_id, market_question=order.market_question,
                    side="BUY", price=new_price, size=shares,
                    remaining_size=shares, status="OPEN",
                    post_only=True, is_paper=True, order_tag="ENTRY_REPRICE"))
                logger.info("Reprice paper: %s %.4f->%.4f (bid=%.4f ask=%.4f rp=%d)",
                            order.token_id[:12], order.price, new_price, best_bid, best_ask, rp_count + 1)
            elif self._live_adapter and self._live_adapter.is_ready:
                new_oid = self._live_adapter.place_limit_order(
                    order.token_id, "BUY", new_price, shares,
                    post_only=True, book=book)
                if new_oid:
                    self._db.insert_order(OpenOrder(
                        order_id=new_oid, token_id=order.token_id,
                        condition_id=order.condition_id, market_question=order.market_question,
                        side="BUY", price=new_price, size=shares,
                        remaining_size=shares, status="OPEN",
                        post_only=True, is_paper=False, order_tag="ENTRY_REPRICE"))
                    logger.info("Reprice live: %s %.4f->%.4f => %s (rp=%d)",
                                order.token_id[:12], order.price, new_price, new_oid, rp_count + 1)
                else:
                    logger.warning("Reprice failed for %s", order.token_id[:12])
                    continue

            self._reprice_state.pop(oid, None)
            new_id = new_oid if 'new_oid' in dir() else oid
            self._reprice_state[new_id] = (now, rp_count + 1)

    # ==================================================================
    # Strategy memory
    # ==================================================================
    def _update_strategy_memory(self):
        hours = self._settings.recent_winner_boost_hours
        winners = set(self._db.get_recent_profitable_tokens(hours, self.is_paper))
        self._strategy.set_recent_winners(winners)
        self._strategy.set_farm_tokens(set(self._farm.keys()))

        positions = self._db.get_positions(self.is_paper)
        counts: Dict[str, int] = {}
        for p in positions:
            if p.condition_id:
                counts[p.condition_id] = counts.get(p.condition_id, 0) + 1
        self._strategy.set_position_condition_counts(counts)

    # ==================================================================
    # Inventory management (unchanged)
    # ==================================================================
    def _manage_aging_inventory(self):
        positions = self._db.get_positions(self.is_paper)
        now_dt = datetime.now(timezone.utc)
        for pos in positions:
            if not self._running:
                break
            created = _parse_dt(pos.created_at)
            if not created:
                continue
            age_min = (now_dt - created).total_seconds() / 60.0
            if self._db.has_open_order_for_token(pos.token_id, "SELL", self.is_paper):
                continue
            if age_min >= self._settings.breakeven_unwind_minutes:
                book = fetch_order_book(pos.token_id)
                if book.best_bid and book.best_bid > 0:
                    mp = pos.avg_entry * (0.95 if self._settings.allow_small_forced_unwind_loss else 1.0)
                    if book.best_bid >= mp:
                        logger.info("Inventory: unwinding %s (%.0fmin bid=%.4f avg=%.4f)",
                                    pos.token_id[:12], age_min, book.best_bid, pos.avg_entry)
                        self._place_sell(pos, book.best_bid, pos.shares, -1, book,
                                         order_tag="BREAKEVEN_UNWIND")

    # ==================================================================
    # Market refresh
    # ==================================================================
    def _maybe_refresh_markets(self):
        now = _mono()
        interval = self._settings.market_refresh_interval_sec
        if self._markets_cache and (now - self._markets_last_refresh) < interval:
            return
        self._markets_cache = fetch_markets(limit=1000)
        self._markets_last_refresh = now

    # ==================================================================
    # Paper fills (unchanged)
    # ==================================================================
    def _check_paper_fills(self):
        open_orders = self._db.get_open_orders(is_paper=True)
        if not open_orders:
            return
        tids = list({o.token_id for o in open_orders})
        books = fetch_multiple_order_books(tids)
        for order in open_orders:
            if not self._running or order.status != "OPEN":
                continue
            book = books.get(order.token_id)
            if not book:
                continue
            try:
                filled, fp, fq = self._paper_adapter.check_resting_order_fill(
                    order.side, order.price, order.remaining_size,
                    book.best_bid, book.best_ask, book.best_bid_size, book.best_ask_size)
                if filled and fq > 0:
                    self._process_fill(order, fp, fq)
            except Exception as exc:
                logger.error("Paper fill check error %s: %s", order.order_id, exc)

    # ==================================================================
    # Position monitoring + exit ladder (unchanged)
    # ==================================================================
    def _monitor_positions(self, positions):
        if not positions:
            return
        tids = [p.token_id for p in positions]
        books = fetch_multiple_order_books(tids)
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
                    continue

                best_bid = book.best_bid
                best_ask = book.best_ask
                mid = book.midpoint
                trigger = mid if (self._settings.exit_trigger_mode == "midpoint" and mid) else best_bid
                if not trigger or trigger <= 0:
                    continue

                triggers = self._pnl.check_exit_rungs(
                    refreshed, self._settings.exit_multiples,
                    self._settings.exit_fractions, trigger_price=trigger)
                if not triggers:
                    continue

                rung_idx, frac, sell_shares = triggers[0]
                min_exit = refreshed.avg_entry + self._settings.min_exit_profit_buffer
                if best_bid is None or best_bid < min_exit:
                    continue

                if self._settings.exit_order_mode == "passive":
                    sp = mid if (mid and mid > best_bid) else best_bid
                    if sp < min_exit:
                        sp = min_exit
                else:
                    sp = best_bid

                if sp > 0 and sell_shares > 0:
                    self._place_sell(refreshed, sp, sell_shares, rung_idx, book,
                                     order_tag=f"EXIT_RUNG:{rung_idx}")
            except Exception as exc:
                logger.error("Monitor error %s: %s", pos.token_id[:12], exc)

    # ==================================================================
    # Stale orders
    # ==================================================================
    def _cancel_stale_orders(self):
        stale = self._db.get_stale_orders(self._settings.stale_order_timeout_sec, self.is_paper)
        for order in stale:
            logger.info("Cancelling stale order %s", order.order_id)
            self._db.update_order_status(order.order_id, "CANCELLED")
            if not self.is_paper and self._live_adapter:
                self._live_adapter.cancel_order(order.order_id)
            self._db.insert_trade(TradeRecord(
                timestamp=_now(), action="CANCEL", market_question=order.market_question,
                token_id=order.token_id, price=order.price, size=order.remaining_size,
                notes="Stale order timeout", is_paper=self.is_paper))
            self._reprice_state.pop(order.order_id, None)

    # ==================================================================
    # Buy placement (unchanged except farm_touch on fill)
    # ==================================================================
    def _place_buy(self, cand, shares: float) -> Optional[float]:
        token_id = cand.token.token_id
        if self._db.has_open_order_for_token(token_id, "BUY", self.is_paper):
            return None
        if self.is_paper:
            return self._place_buy_paper(cand, cand.ask_price, shares, token_id)
        else:
            return self._place_buy_live(cand, cand.ask_price, shares, token_id)

    def _place_buy_paper(self, cand, price, shares, token_id) -> Optional[float]:
        oid = self._paper_adapter.generate_order_id()
        filled, fp, fq = self._paper_adapter.simulate_fill(
            "BUY", price, shares, cand.book.best_bid, cand.book.best_ask,
            cand.book.best_bid_size, cand.book.best_ask_size)
        remaining = shares if not filled else max(0, shares - fq)
        status = "FILLED" if (filled and fq >= shares) else "OPEN"
        self._db.insert_order(OpenOrder(
            order_id=oid, token_id=token_id, condition_id=cand.market.condition_id,
            market_question=cand.market.question, side="BUY", price=price, size=shares,
            remaining_size=remaining, status=status, post_only=self._settings.use_post_only,
            is_paper=True, order_tag="ENTRY"))
        actual = 0.0
        if filled and fq > 0:
            actual = fq
            self._pnl.record_buy(token_id, cand.market.condition_id,
                                  cand.market.question, cand.token.outcome,
                                  fp, fq, is_paper=True)
            self._db.insert_trade(TradeRecord(
                timestamp=_now(), action="BUY", market_question=cand.market.question,
                outcome=cand.token.outcome, token_id=token_id, price=fp, size=fq,
                gross_value=fp * fq, notes=f"Paper fill (score={cand.score:.1f})",
                is_paper=True))
            self.last_order_time.emit(_now())
            self._farm_touch(token_id, cand.market.condition_id, "buy_fill")
            self._record_fill_event()
        return actual

    def _place_buy_live(self, cand, price, shares, token_id) -> Optional[float]:
        if not self._live_adapter or not self._live_adapter.is_ready:
            return None
        oid = self._live_adapter.place_limit_order(
            token_id, "BUY", price, shares,
            post_only=self._settings.use_post_only, book=cand.book)
        if not oid:
            return None
        self._db.insert_order(OpenOrder(
            order_id=oid, token_id=token_id, condition_id=cand.market.condition_id,
            market_question=cand.market.question, side="BUY", price=price, size=shares,
            remaining_size=shares, status="OPEN", post_only=self._settings.use_post_only,
            is_paper=False, order_tag="ENTRY"))
        self._db.insert_trade(TradeRecord(
            timestamp=_now(), action="BUY", market_question=cand.market.question,
            outcome=cand.token.outcome, token_id=token_id, price=price, size=shares,
            gross_value=price * shares, notes="Live order placed", is_paper=False))
        self.last_order_time.emit(_now())
        return 0.0

    # ==================================================================
    # Sell placement (unchanged)
    # ==================================================================
    def _place_sell(self, pos, price, shares, rung_idx, book=None, order_tag=""):
        tag = order_tag or (f"EXIT_RUNG:{rung_idx}" if rung_idx >= 0 else "MANUAL")
        if self.is_paper:
            self._place_sell_paper(pos, price, shares, rung_idx, book, tag)
        else:
            self._place_sell_live(pos, price, shares, rung_idx, book, tag)

    def _place_sell_paper(self, pos, price, shares, rung_idx, book, tag):
        tid = pos.token_id
        oid = self._paper_adapter.generate_order_id()
        if book is None:
            book = fetch_order_book(tid)
        filled, fp, fq = self._paper_adapter.simulate_fill(
            "SELL", price, shares, book.best_bid, book.best_ask,
            book.best_bid_size, book.best_ask_size)
        if filled and fq > 0:
            realized = self._pnl.record_sell(tid, fp, fq, is_paper=True)
            self._db.insert_trade(TradeRecord(
                timestamp=_now(), action="SELL", market_question=pos.market_question,
                outcome=pos.outcome, token_id=tid, price=fp, size=fq,
                gross_value=fp * fq, realized_pnl=realized,
                notes=f"Exit rung {rung_idx+1}" if rung_idx >= 0 else tag, is_paper=True))
            self._db.insert_order(OpenOrder(
                order_id=oid, token_id=tid, condition_id=pos.condition_id,
                market_question=pos.market_question, side="SELL", price=fp, size=fq,
                remaining_size=0, status="FILLED", is_paper=True, order_tag=tag))
            if fq >= shares and rung_idx >= 0 and tag.startswith("EXIT_RUNG"):
                self._pnl.advance_exit_rung(tid, rung_idx + 1, is_paper=True)
            if realized > 0:
                self._farm_touch(tid, pos.condition_id, "profit_sell_fill")
            self._record_fill_event()
        else:
            self._db.insert_order(OpenOrder(
                order_id=oid, token_id=tid, condition_id=pos.condition_id,
                market_question=pos.market_question, side="SELL", price=price, size=shares,
                remaining_size=shares, status="OPEN", is_paper=True, order_tag=tag))

    def _place_sell_live(self, pos, price, shares, rung_idx, book, tag):
        tid = pos.token_id
        if not self._live_adapter or not self._live_adapter.is_ready:
            return
        oid = self._live_adapter.place_limit_order(
            tid, "SELL", price, shares, post_only=False, book=book)
        if oid:
            self._db.insert_order(OpenOrder(
                order_id=oid, token_id=tid, condition_id=pos.condition_id,
                market_question=pos.market_question, side="SELL", price=price, size=shares,
                remaining_size=shares, status="OPEN", is_paper=False, order_tag=tag))
            self._db.insert_trade(TradeRecord(
                timestamp=_now(), action="SELL", market_question=pos.market_question,
                outcome=pos.outcome, token_id=tid, price=price, size=shares,
                gross_value=price * shares, notes=f"Live exit (tag={tag})", is_paper=False))

    # ==================================================================
    # Fill processing — seeds farm list
    # ==================================================================
    def _process_fill(self, order: OpenOrder, fill_price: float, fill_qty: float):
        if order.side == "BUY":
            self._pnl.record_buy(order.token_id, order.condition_id,
                                  order.market_question, "", fill_price, fill_qty,
                                  is_paper=self.is_paper)
            self._db.insert_trade(TradeRecord(
                timestamp=_now(), action="BUY", market_question=order.market_question,
                token_id=order.token_id, price=fill_price, size=fill_qty,
                gross_value=fill_price * fill_qty, notes="Resting fill",
                is_paper=self.is_paper))
            self._farm_touch(order.token_id, order.condition_id, "buy_fill")
            self._record_fill_event()
        elif order.side == "SELL":
            realized = self._pnl.record_sell(order.token_id, fill_price, fill_qty,
                                              is_paper=self.is_paper)
            self._db.insert_trade(TradeRecord(
                timestamp=_now(), action="SELL", market_question=order.market_question,
                token_id=order.token_id, price=fill_price, size=fill_qty,
                gross_value=fill_price * fill_qty, realized_pnl=realized,
                notes="Resting fill", is_paper=self.is_paper))
            if realized > 0:
                self._farm_touch(order.token_id, order.condition_id, "profit_sell_fill")
            self._record_fill_event()
            tag = order.order_tag or self._db.get_order_tag(order.order_id)
            if tag.startswith("EXIT_RUNG:"):
                try:
                    rung = int(tag.split(":")[1])
                    rem = max(0, order.remaining_size - fill_qty)
                    if rem <= 0.01:
                        self._pnl.advance_exit_rung(order.token_id, rung + 1, self.is_paper)
                except (ValueError, IndexError):
                    pass

        rem = max(0, order.remaining_size - fill_qty)
        self._db.update_order_status(order.order_id, "FILLED" if rem <= 0 else "OPEN", rem)
        self.last_order_time.emit(_now())

    # ==================================================================
    # Kill switch + state emission
    # ==================================================================
    def _execute_kill_switch(self):
        logger.warning("Kill switch — cancelling all orders")
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
                timestamp=_now(), total_exposure=summary.total_exposure,
                cash_reserved=summary.cash_reserved, unrealized_pnl=summary.unrealized_pnl,
                realized_pnl=summary.realized_pnl, total_pnl=summary.total_pnl))
        except Exception as exc:
            logger.error("Emit state error: %s", exc)
