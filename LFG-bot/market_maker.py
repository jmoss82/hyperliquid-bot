"""
Automated Market Making Bot for HIP-3 Pairs

Strategy (Phase 3: WMA Trend-Based with Fast Exits):
1. Build 5-second candles from streaming bid/ask data
2. Calculate 10-period WMA using weighted close (H+L+C+C)/4
3. Detect trend: UP (price > WMA) or DOWN (price < WMA)
4. Place ONE-SIDED MAKER order based on trend direction:
   - Uptrend → BUY order (closer to ask, post-only)
   - Downtrend → SELL order (closer to bid, post-only)
5. Monitor position for exit conditions (ALL TAKER EXITS):
   - Stop Loss: Exit as taker if P&L <= -7 bps
   - Take Profit: Exit as taker if P&L >= +20 bps
   - Max Hold Time: Exit as taker if held > 120s
6. Repeat continuously

Key parameters:
- spread_threshold_bps: Minimum spread to enter (default: 6 bps)
- position_size_usd: Size per trade (default: $11)
- spread_position: Where to place orders (0=edge, 0.5=mid, default: 0.2)
- min_trade_interval: Cooldown between trades (default: 5s)
- take_profit_bps: Take profit threshold (default: +20 bps)
- stop_loss_bps: Stop loss threshold (default: -7 bps)
- max_hold_time: Maximum position hold time (default: 120s)
"""
import asyncio
import websockets
import json
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, Dict, List
import sys
from pathlib import Path
from contextlib import suppress

# Import local config (renamed to avoid conflict with grid-bot/config package)
from lfg_config import WALLET_ADDRESS, PRIVATE_KEY, ACCOUNT_ADDRESS

# Add grid-bot to path for XYZ client import
sys.path.insert(0, str(Path(__file__).parent.parent / "grid-bot"))

from xyz_client import XYZClient, OrderSide, OrderStatus as XYZOrderStatus
from candle_builder import CandleBuilder


@dataclass
class TrackedOrder:
    """Track an individual order"""
    side: OrderSide
    price: float
    size: float
    order_id: Optional[int] = None
    status: str = "PENDING"
    filled_size: float = 0.0
    timestamp: float = 0.0
    filled_timestamp: Optional[float] = None


@dataclass
class Opportunity:
    """A market making opportunity with trend direction"""
    bid: float
    ask: float
    spread_bps: float
    timestamp: float
    trend: str  # 'UP', 'DOWN', 'FLAT', or 'UNKNOWN'


class MarketMaker:
    """Automated market making bot"""

    def __init__(
        self,
        client: XYZClient,
        coin: str = "xyz:SILVER",
        spread_threshold_bps: float = 5.0,
        position_size_usd: float = 10.0,
        spread_position: float = 0.3,
        max_patience_ms: int = 300,
        max_positions: int = 1,
        max_trades: int = 10,
        max_loss: float = 5.0,
        min_trade_interval: float = 5.0,
        dry_run: bool = True,
        max_quote_age_ms: float = 500.0,
        ws_stale_timeout_s: float = 5.0,
        opportunity_queue_size: int = 1,
        wma_period: int = 60,
        wma_price_type: str = "weighted_close",
        wma_threshold: float = 0.0005,
        candle_interval_seconds: int = 5,
        max_candles: int = 400,
        min_trend_streak: int = 2,
        min_wma_distance_bps: float = 3.0,
        min_wma_slope_bps: float = 0.8,
        trend_enter_bps: float = 4.0,
        trend_exit_bps: float = 8.0,
        wma_slope_shift_candles: int = 3,
        structure_break_buffer_bps: float = 3.0,
        signal_ttl_s: float = 6.0,
    ):
        self.client = client
        self.coin = coin
        self.spread_threshold_bps = spread_threshold_bps
        self.position_size_usd = position_size_usd
        self.spread_position = spread_position
        self.max_patience_ms = max_patience_ms
        self.max_positions = max_positions
        self.max_trades = max_trades
        self.max_loss = max_loss
        self.min_trade_interval = min_trade_interval
        self.dry_run = dry_run
        self.max_quote_age_ms = max_quote_age_ms
        self.ws_stale_timeout_s = ws_stale_timeout_s
        self.opportunity_queue_size = opportunity_queue_size
        self.signal_ttl_s = signal_ttl_s

        # State
        self.ws_url = "wss://api.hyperliquid.xyz/ws"
        self.current_bid = None
        self.current_ask = None
        self.current_mid = None

        # Active orders
        self.active_orders: List[TrackedOrder] = []
        self.open_positions = 0

        # Stats
        self.opportunities_seen = 0
        self.trades_attempted = 0
        self.both_filled = 0  # Keep for backward compatibility (won't be used in Phase 2)
        self.one_filled = 0
        self.long_entries = 0  # PHASE 2: Track directional entries
        self.short_entries = 0  # PHASE 2: Track directional entries
        self.total_profit = 0.0
        self.total_loss = 0.0

        # Orphan exit tracking
        self.orphan_exits_attempted = 0
        self.orphan_exits_filled = 0
        self.orphan_market_orders = 0
        self.orphan_in_progress = False

        # Exit parameters
        self.max_orphan_loss = 0.02  # $0.02 loss triggers immediate exit
        self.orphan_chase_time_1 = 3.0  # First reprice at 3s
        self.orphan_max_time = 5.0  # Market order at 5s

        # Position/cache tracking
        self.last_position_size = 0.0
        self.last_position_ts = 0.0
        self._open_orders_empty_streak = 0
        self.unpaired_cancel_ms = max(1000, self.max_patience_ms * 2)

        # Cooldown tracking
        self.last_placement_time = 0.0

        # Websocket latency monitoring
        self.ws_ping_interval_s = 30.0
        self.ws_ping_timeout_s = 5.0

        # Quote freshness tracking
        self.last_book_update_ts = 0.0
        self._last_stale_log_ts = 0.0

        # Task coordination
        self.trade_queue: asyncio.Queue[Opportunity] = asyncio.Queue(
            maxsize=self.opportunity_queue_size
        )
        self.cycle_lock = asyncio.Lock()
        self._trade_task = None
        self._fills_task = None
        self._watchdog_task = None

        # WMA trend detection (Unified signal)
        self.candle_builder = CandleBuilder(
            candle_interval_seconds=candle_interval_seconds,
            max_candles=max_candles
        )
        self.wma_period = wma_period
        self.wma_price_type = wma_price_type  # (H+L+C+C)/4
        self.wma_threshold = wma_threshold
        self.last_trend = "UNKNOWN"
        self.trend_state = "UNKNOWN"
        self._trend_streak = 0
        self.min_trend_streak = min_trend_streak
        self.min_wma_distance_bps = min_wma_distance_bps
        self.min_wma_slope_bps = min_wma_slope_bps
        self.trend_enter_bps = trend_enter_bps
        self.trend_exit_bps = trend_exit_bps
        self.wma_slope_shift_candles = wma_slope_shift_candles
        self.structure_break_buffer_bps = structure_break_buffer_bps
        self.last_eval_price: Optional[float] = None
        self.last_wma: Optional[float] = None
        self.last_raw_trend: str = "UNKNOWN"
        self.latest_signal: Optional[Dict] = None
        self.latest_signal_ts: Optional[float] = None

        # Position management parameters (Phase 3: Smart exits)
        self.take_profit_bps = 20.0      # Exit at +20 bps profit
        self.stop_loss_bps = 7.0         # Exit at -7 bps loss
        self.min_hold_time = 10.0        # SL inactive for first 10s (filter noise)
        self.max_hold_time = 120.0       # Max seconds to hold position
        self.position_check_interval = 0.1  # Check every 0.1 seconds

    def _normalize_status(self, status: Optional[XYZOrderStatus]) -> str:
        """Normalize order status to internal uppercase strings."""
        if isinstance(status, XYZOrderStatus):
            return status.value.upper()
        if status is None:
            return "PENDING"
        return str(status).upper()

    def _quote_age_ms(self) -> Optional[float]:
        """Return age of latest quote in ms, or None if unknown."""
        if self.last_book_update_ts <= 0:
            return None
        return (time.monotonic() - self.last_book_update_ts) * 1000

    async def ensure_fresh_quote(
        self,
        context: str,
        max_age_ms: Optional[float] = None,
        wait_timeout_s: float = 0.25
    ) -> bool:
        """
        Ensure we have a fresh quote before taking action.

        Attempts a REST refresh, then briefly waits for a WS update.
        """
        max_age_ms = max_age_ms or self.max_quote_age_ms
        age = self._quote_age_ms()
        if age is not None and age <= max_age_ms:
            return True

        # Attempt quick REST refresh
        if self.refresh_quotes():
            age = self._quote_age_ms()
            if age is not None and age <= max_age_ms:
                return True

        # Briefly wait for WS update
        if wait_timeout_s > 0:
            start = time.monotonic()
            while time.monotonic() - start < wait_timeout_s:
                age = self._quote_age_ms()
                if age is not None and age <= max_age_ms:
                    return True
                await asyncio.sleep(0.05)

        age_str = f"{age:.0f}ms" if age is not None else "unknown"
        print(f"[STALE] {context}: quote age {age_str} > {max_age_ms:.0f}ms", flush=True)
        return False

    def enqueue_opportunity(self, opportunity: Opportunity) -> None:
        """Non-blocking enqueue of a trade opportunity."""
        if self.cycle_lock.locked():
            return
        if self.trade_queue.full():
            return
        age = self._quote_age_ms()
        if age is None or age > self.max_quote_age_ms:
            return
        try:
            self.trade_queue.put_nowait(opportunity)
        except asyncio.QueueFull:
            pass

    async def trade_worker(self):
        """Process queued opportunities without blocking the WS reader."""
        while True:
            opportunity = await self.trade_queue.get()
            try:
                async with self.cycle_lock:
                    bid = self.current_bid
                    ask = self.current_ask
                    if not bid or not ask:
                        continue
                    refreshed_opportunity = self.should_enter(bid, ask)
                    if not refreshed_opportunity:
                        continue
                    if not await self.ensure_fresh_quote("trade_worker"):
                        continue
                    await self.place_order(refreshed_opportunity)
            except Exception as e:
                print(f"[TRADE WORKER] Error: {e}", flush=True)
            finally:
                self.trade_queue.task_done()

    async def fills_worker(self):
        """Periodic fill checks without blocking WS processing."""
        while True:
            await asyncio.sleep(1.0)
            if not self.active_orders or self.orphan_in_progress:
                continue
            if self.cycle_lock.locked():
                continue
            async with self.cycle_lock:
                try:
                    await self.check_fills()
                except Exception as e:
                    print(f"[FILLS WORKER] Error: {e}", flush=True)

    async def book_watchdog(self):
        """Watch for stale WS data and refresh with REST when needed."""
        while True:
            await asyncio.sleep(0.5)
            age = self._quote_age_ms()
            if age is None:
                continue
            if age > self.ws_stale_timeout_s * 1000:
                now = time.monotonic()
                if now - self._last_stale_log_ts > 5.0:
                    print(f"[STALE] WS book age {age:.0f}ms - refreshing quotes", flush=True)
                    self._last_stale_log_ts = now
                self.refresh_quotes()

    async def verified_cancel(self, order_id: int, max_wait_s: float = 2.0) -> bool:
        """
        Cancel an order and VERIFY it's gone from the book.
        
        Returns:
            True if order is confirmed gone (cancelled or filled)
            False if order still on book after timeout
        """
        # Attempt cancel
        try:
            self.client.cancel_order(self.coin, order_id)
            print(f"[CANCEL] Sent cancel for order {order_id}", flush=True)
        except Exception as e:
            print(f"[CANCEL] Cancel request failed (may already be gone): {e}", flush=True)
        
        # Poll until order is gone or timeout
        start = time.time()
        while time.time() - start < max_wait_s:
            await asyncio.sleep(0.15)
            try:
                open_orders = self.client.get_open_orders(self.coin)
                open_ids = {o.order_id for o in open_orders}
                if order_id not in open_ids:
                    print(f"[CANCEL] Verified order {order_id} is gone", flush=True)
                    return True
            except Exception as e:
                print(f"[CANCEL] Error checking orders: {e}", flush=True)
        
        print(f"[CANCEL] WARNING: Order {order_id} still on book after {max_wait_s}s", flush=True)
        return False

    async def ensure_flat(self, max_attempts: int = 3) -> bool:
        """
        Ensure no orders on book and no position before new cycle.
        
        This is the GATE that must pass before any new order placement.
        
        Returns:
            True if verified flat (no orders, no position)
            False if unable to get flat after max_attempts
        """
        # Guard against recursive calls (e.g., if exit_orphan triggers ensure_flat)
        if self.orphan_in_progress:
            print(f"[FLAT CHECK] Skipping - orphan exit in progress", flush=True)
            return False
        
        for attempt in range(max_attempts):
            if attempt > 0:
                print(f"[FLAT CHECK] Attempt {attempt + 1}/{max_attempts}", flush=True)
                await asyncio.sleep(0.3)
            
            # Step 1: Get all open orders
            try:
                open_orders = self.client.get_open_orders(self.coin)
            except Exception as e:
                print(f"[FLAT CHECK] Error fetching orders: {e}", flush=True)
                continue
            
            # Step 2: Cancel any open orders with verification
            if open_orders:
                print(f"[FLAT CHECK] Found {len(open_orders)} open orders - cancelling", flush=True)
                for order in open_orders:
                    await self.verified_cancel(order.order_id)
                
                # Re-check after cancellations
                await asyncio.sleep(0.2)
                open_orders = self.client.get_open_orders(self.coin)
                if open_orders:
                    print(f"[FLAT CHECK] Still have {len(open_orders)} orders after cancel", flush=True)
                    continue  # Try again
            
            # Step 3: Check position
            try:
                position = self.client.get_position(self.coin)
                if position and abs(position.size) > 0:
                    print(f"[FLAT CHECK] Have position: {position.size:.4f} @ ${position.entry_price:.2f}", flush=True)
                    
                    # Create orphan order to exit
                    filled = TrackedOrder(
                        side=OrderSide.BUY if position.size > 0 else OrderSide.SELL,
                        price=position.entry_price or self.current_mid or 0.0,
                        size=abs(position.size),
                        status="FILLED",
                        timestamp=datetime.now(timezone.utc).timestamp(),
                        filled_timestamp=datetime.now(timezone.utc).timestamp(),
                    )
                    
                    # Exit the position
                    await self.exit_orphan(filled)
                    
                    # Re-check position after exit
                    await asyncio.sleep(0.3)
                    position = self.client.get_position(self.coin)
                    if position and abs(position.size) > 0:
                        print(f"[FLAT CHECK] Still have position after exit: {position.size:.4f}", flush=True)
                        continue  # Try again
            except Exception as e:
                print(f"[FLAT CHECK] Error checking position: {e}", flush=True)
                continue
            
            # Step 4: Final verification - must have no orders AND no position
            try:
                final_orders = self.client.get_open_orders(self.coin)
                final_position = self.client.get_position(self.coin)
                
                has_orders = len(final_orders) > 0
                has_position = final_position and abs(final_position.size) > 0
                
                if not has_orders and not has_position:
                    print(f"[FLAT CHECK] ✓ Verified FLAT - ready for new cycle", flush=True)
                    # Clear internal tracking state
                    self.active_orders.clear()
                    self.open_positions = 0
                    return True
                else:
                    print(f"[FLAT CHECK] Not flat: orders={has_orders}, position={has_position}", flush=True)
            except Exception as e:
                print(f"[FLAT CHECK] Error in final verification: {e}", flush=True)
        
        print(f"[FLAT CHECK] ✗ Failed to get flat after {max_attempts} attempts", flush=True)
        return False

    def calculate_order_prices(self, bid: float, ask: float) -> tuple[float, float]:
        """
        Calculate buy and sell prices inside the spread.

        ⚠️ DEAD CODE (Phase 2): No longer used - kept as backup
        This was used for atomic two-sided placement. Now replaced by
        one-sided logic in place_order() that determines price based on trend.

        MOMENTUM STRATEGY: Place orders to capture directional moves
        - BUY closer to ask (fills when price rising)
        - SELL closer to bid (fills when price falling)

        Args:
            bid: Current best bid
            ask: Current best ask

        Returns:
            (buy_price, sell_price)
        """
        mid = (bid + ask) / 2
        spread = ask - bid

        # Place orders to capture momentum (FLIPPED from mean reversion)
        # spread_position = 0 means at ask/bid (reversed)
        # spread_position = 0.5 means at mid
        # spread_position = 0.3 means 30% away from ask/bid into spread
        buy_price = ask - (spread * self.spread_position)
        sell_price = bid + (spread * self.spread_position)

        return buy_price, sell_price

    def should_enter(self, bid: float, ask: float) -> Optional[Opportunity]:
        """
        Check if we should enter based on unified signal gating.

        Returns:
            Opportunity if criteria met, None otherwise
        """
        # Safety: Check max trades limit
        if self.trades_attempted >= self.max_trades:
            print(f"\n[SAFETY] Max trades reached ({self.max_trades}). Stopping.")
            return None

        # Safety: Check max loss limit
        net_pnl = self.total_profit - self.total_loss
        if net_pnl <= -self.max_loss:
            print(f"\n[SAFETY] Max loss reached (${net_pnl:.2f}). Stopping.")
            return None

        # Don't enter if already at position limit
        if self.open_positions >= self.max_positions:
            return None

        # Don't enter if we have active orders pending
        if self.active_orders:
            return None

        # Don't enter if currently handling an orphan
        if self.orphan_in_progress:
            return None

        # Ensure we have a recent signal (from a completed candle)
        if not self.latest_signal or not self.latest_signal_ts:
            return None

        age = time.monotonic() - self.latest_signal_ts
        if age > self.signal_ttl_s:
            return None

        # Recheck spread on current quotes
        mid = (bid + ask) / 2
        spread = ask - bid
        spread_bps = (spread / mid) * 10000
        if spread_bps < self.spread_threshold_bps:
            return None

        signal = self.latest_signal.get("signal")
        if signal not in ("LONG", "SHORT"):
            return None

        trend = "UP" if signal == "LONG" else "DOWN"
        return Opportunity(
            bid=bid,
            ask=ask,
            spread_bps=spread_bps,
            timestamp=datetime.now(timezone.utc).timestamp(),
            trend=trend
        )

    async def place_order(self, opportunity: Opportunity):
        """
        Place single-sided order based on trend direction.

        PHASE 2: One-sided momentum strategy
        - Enter LONG when trend is UP (buy closer to ask)
        - Enter SHORT when trend is DOWN (sell closer to bid)

        Args:
            opportunity: The market making opportunity with trend
        """
        self.opportunities_seen += 1

        # ====================================================================================
        # COOLDOWN: Enforce minimum time between order placements
        # ====================================================================================
        if self.last_placement_time > 0:
            time_since_last = time.time() - self.last_placement_time
            if time_since_last < self.min_trade_interval:
                cooldown_remaining = self.min_trade_interval - time_since_last
                print(f"[COOLDOWN] {cooldown_remaining:.1f}s remaining before next placement", flush=True)
                return

        # ====================================================================================
        # FLAT GATE: Verify we're completely flat before placing new order
        # ====================================================================================
        is_flat = await self.ensure_flat()
        if not is_flat:
            print(f"[BLOCKED] Cannot place order - not flat. Skipping this opportunity.", flush=True)
            return

        # ====================================================================================
        # FRESH QUOTE GATE: Avoid placing orders on stale data
        # ====================================================================================
        if not await self.ensure_fresh_quote("place_order"):
            print(f"[BLOCKED] Cannot place order - stale quote.", flush=True)
            return

        # ====================================================================================
        # Determine side and price from trend
        # ====================================================================================
        spread = opportunity.ask - opportunity.bid

        if opportunity.trend == 'UP':
            # Uptrend: Place BUY order (closer to ask to catch momentum)
            side = OrderSide.BUY
            price = opportunity.ask - (spread * self.spread_position)
        elif opportunity.trend == 'DOWN':
            # Downtrend: Place SELL order (closer to bid to catch momentum)
            side = OrderSide.SELL
            price = opportunity.bid + (spread * self.spread_position)
        else:
            print(f"[ERROR] Invalid trend for entry: {opportunity.trend}", flush=True)
            return

        # Calculate size to meet minimum notional
        position_size = self.position_size_usd / price

        print(f"\n{'='*80}", flush=True)
        print(f"[{opportunity.trend} TREND #{self.opportunities_seen}] {datetime.now(timezone.utc).strftime('%H:%M:%S')}", flush=True)
        print(f"{'='*80}", flush=True)
        print(f"Spread: {opportunity.spread_bps:.2f} bps | Bid: ${opportunity.bid:.2f} | Ask: ${opportunity.ask:.2f}", flush=True)
        print(f"Placing {side.value}: ${price:.2f} | Size: {position_size:.4f}", flush=True)
        print(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}", flush=True)

        # ====================================================================================
        # PLACE SINGLE ORDER: Post-only limit order
        # ====================================================================================
        try:
            order = self.client.place_limit_order(
                coin=self.coin,
                side=side,
                price=price,
                size=position_size,
                reduce_only=False,
                post_only=True,  # Maker order only
                dry_run=self.dry_run
            )
        except Exception as e:
            print(f"[ERROR] Order placement failed: {e}", flush=True)
            return

        # ====================================================================================
        # CHECK RESPONSE: If rejected, just move on (no cleanup needed)
        # ====================================================================================
        if not order or not order.order_id:
            print(f"[REJECTED] Order was rejected (likely post-only violation)", flush=True)
            return

        print(f"[SUCCESS] Order accepted! ID: {order.order_id}", flush=True)

        self.trades_attempted += 1
        self.last_placement_time = time.time()

        # Track entry side for stats
        if side == OrderSide.BUY:
            self.long_entries += 1
        else:
            self.short_entries += 1

        # Check if already filled during placement
        if hasattr(order, 'status') and order.status and 'filled' in str(order.status).lower():
            print(f"[INSTANT FILL] Order filled immediately!", flush=True)
            self.one_filled += 1
            return

        # Track the order
        tracked = TrackedOrder(
            side=side,
            price=price,
            size=position_size,
            order_id=order.order_id,
            status="OPEN",
            timestamp=datetime.now(timezone.utc).timestamp()
        )
        self.active_orders = [tracked]
        self.open_positions = 1

        # ====================================================================================
        # MONITOR: Short timeout, then cancel if not filled
        # ====================================================================================
        await self.monitor_order(tracked)

    async def monitor_order(self, order: TrackedOrder):
        """
        Monitor single order for fill.

        PHASE 3: Monitor entry order, then hand off to position monitoring.
        - If fills: Start position monitoring (TP/SL/trend/timeout)
        - If doesn't fill: Cancel and move on

        Args:
            order: The tracked order to monitor
        """
        TIMEOUT_SECONDS = 1.0
        CHECK_INTERVAL = 0.2

        start_time = time.time()
        order_id = order.order_id

        print(f"[MONITOR] Watching {order.side.value} order {order_id} for {TIMEOUT_SECONDS}s", flush=True)

        while time.time() - start_time < TIMEOUT_SECONDS:
            await asyncio.sleep(CHECK_INTERVAL)

            try:
                # Check if order is still on book
                open_orders = self.client.get_open_orders(self.coin)
                open_ids = {o.order_id for o in open_orders}

                if order_id not in open_ids:
                    # Order filled - start position monitoring!
                    print(f"[FILLED] {order.side.value} filled @ ${order.price:.2f}", flush=True)
                    self.one_filled += 1
                    self.active_orders.clear()
                    self.open_positions = 1

                    # PHASE 3: Monitor position with TP/SL/trend/timeout
                    await self.monitor_position(order.side, order.price, order.size)
                    return

            except Exception as e:
                print(f"[MONITOR] Error checking order: {e}", flush=True)

        # ====================================================================================
        # TIMEOUT: Cancel order
        # ====================================================================================
        print(f"[TIMEOUT] Order didn't fill in {TIMEOUT_SECONDS}s - cancelling", flush=True)
        await self.verified_cancel(order_id)

        # Check if it filled during cancel
        try:
            position = self.client.get_position(self.coin)
            if position and abs(position.size) > 0:
                print(f"[TIMEOUT] Position detected ({position.size:.4f}) - filled during cancel", flush=True)
                self.one_filled += 1

                # PHASE 3: Monitor position with TP/SL/trend/timeout
                await self.monitor_position(order.side, order.price, abs(position.size))
                return
        except Exception as e:
            print(f"[TIMEOUT] Error checking position: {e}", flush=True)

        self.active_orders.clear()
        self.open_positions = 0

        # Final flat check
        await self.ensure_flat()

    async def monitor_position(self, entry_side: OrderSide, entry_price: float, size: float):
        """
        Monitor an open position for exit conditions.

        PHASE 3: Fast exit logic - ALL TAKER EXITS.
        Priority order:
        1. Stop Loss (-7 bps) → Taker exit (active after min_hold_time only)
        2. Take Profit (+20 bps) → Taker exit immediately
        3. Max Hold Time (120s) → Taker exit immediately

        Args:
            entry_side: The side we entered (BUY for long, SELL for short)
            entry_price: The price we entered at
            size: Position size
        """
        position_type = "LONG" if entry_side == OrderSide.BUY else "SHORT"
        entry_trend = self.last_trend  # Remember trend at entry

        print(f"\n{'='*60}", flush=True)
        print(f"[POSITION] {position_type} @ ${entry_price:.2f} | Size: {size:.4f}", flush=True)
        print(f"[POSITION] TP: +{self.take_profit_bps} bps | SL: -{self.stop_loss_bps} bps | Max: {self.max_hold_time}s", flush=True)
        print(f"{'='*60}\n", flush=True)

        entry_time = time.time()

        while True:
            await asyncio.sleep(self.position_check_interval)

            # Get current market data
            bid = self.current_bid
            ask = self.current_ask
            mid = self.current_mid

            if not all([bid, ask, mid]):
                continue

            # Calculate current P&L in bps using ACTUAL exit prices (not mid)
            # This prevents SL bleed by accounting for spread cost at exit
            if entry_side == OrderSide.BUY:
                # Long: exit by selling at BID
                exit_price = bid * 0.9995  # Actual exit price (bid minus buffer)
                pnl_dollars = (exit_price - entry_price) * size
                pnl_bps = ((exit_price - entry_price) / entry_price) * 10000
            else:
                # Short: exit by buying at ASK
                exit_price = ask * 1.0005  # Actual exit price (ask plus buffer)
                pnl_dollars = (entry_price - exit_price) * size
                pnl_bps = ((entry_price - exit_price) / entry_price) * 10000

            # Get current trend (sticky state from completed candles)
            current_trend = self.trend_state
            elapsed = time.time() - entry_time

            # ====================================================================================
            # CONDITION 0: TREND FLIP EXIT
            # ====================================================================================
            if entry_side == OrderSide.BUY and current_trend == "DOWN":
                print(f"\n[TREND FLIP] Trend turned DOWN - exiting LONG", flush=True)
                await self.exit_position_fast(entry_side, entry_price, size)
                self.active_orders.clear()
                self.open_positions = 0
                return

            if entry_side == OrderSide.SELL and current_trend == "UP":
                print(f"\n[TREND FLIP] Trend turned UP - exiting SHORT", flush=True)
                await self.exit_position_fast(entry_side, entry_price, size)
                self.active_orders.clear()
                self.open_positions = 0
                return

            # ====================================================================================
            # CONDITION 1: STOP LOSS (active only after min_hold_time)
            # ====================================================================================
            if elapsed >= self.min_hold_time and pnl_bps <= -self.stop_loss_bps:
                print(f"\n[STOP LOSS] P&L: {pnl_bps:.1f} bps (${pnl_dollars:.4f}) <= -{self.stop_loss_bps} bps | held {elapsed:.1f}s", flush=True)
                print(f"[STOP LOSS] Exiting {position_type} as TAKER", flush=True)
                await self.exit_position_fast(entry_side, entry_price, size)
                self.active_orders.clear()
                self.open_positions = 0
                return

            # ====================================================================================
            # CONDITION 2: TAKE PROFIT
            # ====================================================================================
            if pnl_bps >= self.take_profit_bps:
                print(f"\n[TAKE PROFIT] P&L: {pnl_bps:.1f} bps (${pnl_dollars:.4f}) >= +{self.take_profit_bps} bps", flush=True)
                print(f"[TAKE PROFIT] Exiting {position_type} as TAKER", flush=True)
                await self.exit_position_fast(entry_side, entry_price, size)
                self.active_orders.clear()
                self.open_positions = 0
                return

            # ====================================================================================
            # CONDITION 3: MAX HOLD TIME
            # ====================================================================================
            if elapsed >= self.max_hold_time:
                print(f"\n[MAX TIME] Held for {elapsed:.1f}s >= {self.max_hold_time}s", flush=True)
                print(f"[MAX TIME] P&L: {pnl_bps:.1f} bps (${pnl_dollars:.4f})", flush=True)
                print(f"[MAX TIME] Exiting {position_type} as TAKER", flush=True)
                await self.exit_position_fast(entry_side, entry_price, size)
                self.active_orders.clear()
                self.open_positions = 0
                return

            # ====================================================================================
            # Still holding - log periodic updates (every 10 seconds)
            # ====================================================================================
            elapsed_int = int(elapsed)
            if not hasattr(self, '_last_hold_log') or self._last_hold_log != elapsed_int:
                if elapsed_int > 0 and elapsed_int % 10 == 0:
                    self._last_hold_log = elapsed_int
                    print(f"[HOLDING] {position_type} | {elapsed:.0f}s | P&L: {pnl_bps:.1f} bps (${pnl_dollars:.4f}) | Trend: {current_trend}", flush=True)

    async def exit_position_maker(self, entry_side: OrderSide, entry_price: float, size: float, reason: str):
        """
        Exit a position as a MAKER (post-only) with taker fallback.

        Used for non-urgent exits: Take Profit, Trend Reversal, Max Time.
        Places a maker order first, falls back to taker if not filled.

        Args:
            entry_side: The side we entered (BUY for long, SELL for short)
            entry_price: The price we entered at
            size: Position size
            reason: Why we're exiting (for logging)
        """
        self.orphan_in_progress = True

        try:
            exit_side = OrderSide.SELL if entry_side == OrderSide.BUY else OrderSide.BUY
            position_type = "LONG" if entry_side == OrderSide.BUY else "SHORT"

            # Get current market
            bid = self.current_bid
            ask = self.current_ask

            if not bid or not ask:
                self.refresh_quotes()
                bid = self.current_bid
                ask = self.current_ask

            if not bid or not ask:
                print(f"[MAKER EXIT] No market data - falling back to taker", flush=True)
                await self.exit_position_fast(entry_side, entry_price, size)
                return

            # Calculate maker exit price (inside spread for better fill)
            spread = ask - bid
            if exit_side == OrderSide.SELL:
                # Selling: place between mid and ask (favorable for us)
                exit_price = bid + (spread * 0.7)  # 70% into spread from bid
            else:
                # Buying: place between bid and mid (favorable for us)
                exit_price = ask - (spread * 0.7)  # 70% into spread from ask

            exit_price = self.client.format_price(self.coin, exit_price)

            print(f"[MAKER EXIT] Placing {exit_side.value} @ ${exit_price:.2f} (post-only)", flush=True)

            # Place maker exit order
            try:
                exit_order = self.client.place_limit_order(
                    coin=self.coin,
                    side=exit_side,
                    price=exit_price,
                    size=size,
                    reduce_only=True,
                    post_only=True,  # Maker only
                    dry_run=self.dry_run
                )
            except Exception as e:
                print(f"[MAKER EXIT] Order failed: {e} - falling back to taker", flush=True)
                await self.exit_position_fast(entry_side, entry_price, size)
                return

            if not exit_order or not exit_order.order_id:
                print(f"[MAKER EXIT] Order rejected - falling back to taker", flush=True)
                await self.exit_position_fast(entry_side, entry_price, size)
                return

            # Wait for maker order to fill (up to 3 seconds)
            MAKER_TIMEOUT = 3.0
            start_time = time.time()

            while time.time() - start_time < MAKER_TIMEOUT:
                await asyncio.sleep(0.3)

                try:
                    open_orders = self.client.get_open_orders(self.coin)
                    open_ids = {o.order_id for o in open_orders}

                    if exit_order.order_id not in open_ids:
                        # Filled!
                        mid = self.current_mid or ((bid + ask) / 2)
                        if entry_side == OrderSide.BUY:
                            pnl = (exit_price - entry_price) * size
                        else:
                            pnl = (entry_price - exit_price) * size

                        if pnl >= 0:
                            self.total_profit += pnl
                        else:
                            self.total_loss += abs(pnl)

                        print(f"\n{'*'*60}", flush=True)
                        print(f"[MAKER EXIT SUCCESS] {reason}", flush=True)
                        print(f"Entry: ${entry_price:.2f} | Exit: ${exit_price:.2f}", flush=True)
                        print(f"P&L: ${pnl:.4f}", flush=True)
                        print(f"{'*'*60}\n", flush=True)
                        return

                except Exception as e:
                    print(f"[MAKER EXIT] Error checking fill: {e}", flush=True)

            # Maker didn't fill - cancel and use taker
            print(f"[MAKER EXIT] Timeout after {MAKER_TIMEOUT}s - falling back to taker", flush=True)
            await self.verified_cancel(exit_order.order_id)
            await self.exit_position_fast(entry_side, entry_price, size)

        except Exception as e:
            print(f"[MAKER EXIT] Error: {e}", flush=True)
            # Last resort: taker exit
            await self.exit_position_fast(entry_side, entry_price, size)
        finally:
            self.orphan_in_progress = False

    async def monitor_pair_fast(self, buy: TrackedOrder, sell: TrackedOrder):
        """
        Monitor a pair of orders with a SHORT timeout.

        ⚠️ DEAD CODE (Phase 2): No longer used - kept as backup
        This was used for atomic two-sided placement. Now replaced by
        monitor_order() which monitors a single order.

        Philosophy: If not both filled within 1 second, cancel everything.
        Data shows: trades that complete in ≤1s are winners, longer = losers.
        """
        TIMEOUT_SECONDS = 1.0
        CHECK_INTERVAL = 0.2
        
        start_time = time.time()
        buy_id = buy.order_id
        sell_id = sell.order_id
        
        print(f"[MONITOR] Watching for {TIMEOUT_SECONDS}s (fast exit if not both filled): BUY {buy_id} | SELL {sell_id}", flush=True)
        
        while time.time() - start_time < TIMEOUT_SECONDS:
            await asyncio.sleep(CHECK_INTERVAL)
            
            try:
                # Check what's still on the book
                open_orders = self.client.get_open_orders(self.coin)
                open_ids = {o.order_id for o in open_orders}
                
                buy_open = buy_id in open_ids
                sell_open = sell_id in open_ids
                
                # Check position to determine fills
                position = self.client.get_position(self.coin)
                position_size = position.size if position else 0.0
                
                # Case 1: Both gone from book, no position = both cancelled (rare)
                if not buy_open and not sell_open and abs(position_size) < 0.0001:
                    print(f"[MONITOR] Both orders gone, no position - cancelled", flush=True)
                    self.active_orders.clear()
                    self.open_positions = 0
                    return
                
                # Case 2: Both gone from book, position = one filled, other didn't
                # This shouldn't happen with atomic placement, but handle it
                if not buy_open and not sell_open and abs(position_size) > 0:
                    print(f"[MONITOR] Both gone but have position {position_size:.4f} - exiting", flush=True)
                    self.active_orders.clear()
                    self.open_positions = 0
                    side = OrderSide.BUY if position_size > 0 else OrderSide.SELL
                    entry_price = position.entry_price if position else self.current_mid
                    await self.exit_position_fast(side, entry_price, abs(position_size))
                    return
                
                # Case 3: BUY filled (not on book + long position), SELL still open
                if not buy_open and sell_open and position_size > 0:
                    print(f"[MONITOR] BUY filled, SELL still resting - cancelling SELL, exiting LONG", flush=True)
                    await self.verified_cancel(sell_id)
                    self.active_orders.clear()
                    self.open_positions = 0
                    await self.exit_position_fast(OrderSide.BUY, buy.price, position_size)
                    return
                
                # Case 4: SELL filled (not on book + short position), BUY still open
                if not sell_open and buy_open and position_size < 0:
                    print(f"[MONITOR] SELL filled, BUY still resting - cancelling BUY, exiting SHORT", flush=True)
                    await self.verified_cancel(buy_id)
                    self.active_orders.clear()
                    self.open_positions = 0
                    await self.exit_position_fast(OrderSide.SELL, sell.price, abs(position_size))
                    return
                
                # Case 5: Both filled! (no orders on book, position is flat or closed)
                # This is the success case - both orders crossed
                if not buy_open and not sell_open:
                    # Both orders executed - calculate profit
                    profit = (sell.price - buy.price) * buy.size
                    self.total_profit += max(0, profit)
                    self.both_filled += 1
                    print(f"\n{'*'*60}", flush=True)
                    print(f"[SUCCESS] Both sides filled! Profit: ${profit:.4f}", flush=True)
                    print(f"{'*'*60}\n", flush=True)
                    self.active_orders.clear()
                    self.open_positions = 0
                    return
                
            except Exception as e:
                print(f"[MONITOR] Error checking state: {e}", flush=True)
        
        # ====================================================================================
        # TIMEOUT: Cancel everything
        # ====================================================================================
        print(f"[TIMEOUT] {TIMEOUT_SECONDS}s elapsed - cancelling all orders", flush=True)
        
        # Cancel both orders
        await self.verified_cancel(buy_id)
        await self.verified_cancel(sell_id)
        
        # Check if we have a position (one might have filled during cancel)
        try:
            position = self.client.get_position(self.coin)
            if position and abs(position.size) > 0:
                print(f"[TIMEOUT] Have position {position.size:.4f} after cancel - exiting", flush=True)
                side = OrderSide.BUY if position.size > 0 else OrderSide.SELL
                entry_price = position.entry_price if position else self.current_mid
                await self.exit_position_fast(side, entry_price, abs(position.size))
        except Exception as e:
            print(f"[TIMEOUT] Error checking position: {e}", flush=True)
        
        self.active_orders.clear()
        self.open_positions = 0
        
        # Final flat check
        await self.ensure_flat()

    async def exit_position_fast(self, entry_side: OrderSide, entry_price: float, size: float):
        """
        Exit a position IMMEDIATELY as a taker.
        
        Philosophy: Accept the small loss. Get out NOW. Orphans are the enemy.
        No chasing, no waiting - just cross the spread and move on.
        """
        self.orphan_in_progress = True
        
        try:
            exit_side = OrderSide.SELL if entry_side == OrderSide.BUY else OrderSide.BUY
            position_type = "LONG" if entry_side == OrderSide.BUY else "SHORT"
            
            print(f"[FAST EXIT] Exiting {position_type} position @ ${entry_price:.2f}", flush=True)

            fresh = await self.ensure_fresh_quote("exit_position_fast", wait_timeout_s=0.5)
            if not fresh:
                print("[FAST EXIT] Quote stale - proceeding with last known prices", flush=True)
            
            # Get current market
            bid = self.current_bid
            ask = self.current_ask
            
            if not bid or not ask:
                self.refresh_quotes()
                bid = self.current_bid
                ask = self.current_ask
            
            if not bid or not ask:
                print(f"[FAST EXIT] No market data - cannot exit!", flush=True)
                return
            
            # Place aggressive taker order (cross the spread)
            if exit_side == OrderSide.SELL:
                # Selling: place below bid to guarantee fill
                exit_price = bid * 0.9995  # 0.05% below bid
            else:
                # Buying: place above ask to guarantee fill
                exit_price = ask * 1.0005  # 0.05% above ask
            
            exit_price = self.client.format_price(self.coin, exit_price)
            
            print(f"[FAST EXIT] Placing taker {exit_side.value} @ ${exit_price:.2f}", flush=True)
            
            # Place the exit order (NOT post-only - we want to cross)
            exit_order = self.client.place_limit_order(
                coin=self.coin,
                side=exit_side,
                price=exit_price,
                size=size,
                reduce_only=True,
                post_only=False,  # TAKER - cross the spread
                dry_run=self.dry_run
            )
            
            if exit_order:
                # Calculate P&L
                if exit_side == OrderSide.SELL:
                    pnl = (exit_order.price - entry_price) * size
                else:
                    pnl = (entry_price - exit_order.price) * size
                
                if pnl >= 0:
                    self.total_profit += pnl
                else:
                    self.total_loss += abs(pnl)
                
                self.one_filled += 1
                
                print(f"[FAST EXIT] Done! P&L: ${pnl:.4f}", flush=True)
            else:
                print(f"[FAST EXIT] Exit order failed!", flush=True)
                
        except Exception as e:
            print(f"[FAST EXIT] Error: {e}", flush=True)
        finally:
            self.orphan_in_progress = False
            # Verify we're flat
            await self.ensure_flat()

    async def resolve_unpaired_order(self, order: TrackedOrder, context: str):
        """
        Handle a single open order when the opposing order fails.
        
        Uses ensure_flat() to guarantee we're completely clean before returning.
        """
        print(f"[RESOLVE] {context} - ensuring we get flat", flush=True)
        
        # Use ensure_flat to handle everything: cancel orders, exit positions, verify
        is_flat = await self.ensure_flat(max_attempts=5)
        
        if is_flat:
            print(f"[RESOLVE] Successfully resolved - now flat", flush=True)
        else:
            print(f"[RESOLVE] WARNING: Could not get flat after resolution attempt", flush=True)
            # Log current state for debugging
            try:
                open_orders = self.client.get_open_orders(self.coin)
                position = self.client.get_position(self.coin)
                print(f"[RESOLVE] Current state: {len(open_orders)} orders, position={position.size if position else 0}", flush=True)
            except Exception as e:
                print(f"[RESOLVE] Error checking state: {e}", flush=True)

    def calculate_chase_price(self, bid: float, ask: float, exit_side: OrderSide, aggression: float) -> float:
        """
        Calculate chase price for orphan exit.

        Args:
            bid: Current best bid
            ask: Current best ask
            exit_side: Side we need to exit on (BUY or SELL)
            aggression: How far into spread (0.8 = 80% toward favorable side)

        Returns:
            Chase price
        """
        spread = ask - bid

        if exit_side == OrderSide.SELL:
            # Selling: start from bid, move toward ask
            chase_price = bid + (spread * aggression)
        else:
            # Buying: start from ask, move toward bid
            chase_price = ask - (spread * aggression)

        return chase_price

    def calculate_unrealized_pnl(self, entry_price: float, entry_side: OrderSide, current_mid: float, size: float) -> float:
        """
        Calculate unrealized P&L for an orphan position.

        Args:
            entry_price: Price we entered at
            entry_side: Side of the filled order (BUY or SELL)
            current_mid: Current mid price
            size: Position size

        Returns:
            Unrealized P&L (positive = profit, negative = loss)
        """
        if entry_side == OrderSide.BUY:
            # Long position: current_mid - entry_price
            pnl = (current_mid - entry_price) * size
        else:
            # Short position: entry_price - current_mid
            pnl = (entry_price - current_mid) * size

        return pnl

    def refresh_quotes(self) -> bool:
        """Refresh bid/ask using REST if websocket is unavailable."""
        try:
            book = self.client.get_order_book(self.coin)
            if book and book.best_bid and book.best_ask:
                self.current_bid = book.best_bid
                self.current_ask = book.best_ask
                self.current_mid = (book.best_bid + book.best_ask) / 2
                self.last_book_update_ts = time.monotonic()
                return True
        except Exception as e:
            print(f"[WARN] Failed to refresh quotes: {e}", flush=True)
        return False

    def get_trend(self) -> str:
        """
        Get current trend from WMA analysis.

        Returns:
            'UP' if price above WMA, 'DOWN' if below, 'FLAT' if on line, 'UNKNOWN' if insufficient data
        """
        return self.candle_builder.get_trend(
            period=self.wma_period,
            price_type=self.wma_price_type,
            threshold=self.wma_threshold
        )

    def candle_price(self, candle: "Candle") -> float:
        """Price representation for a completed candle based on configured mode."""
        if self.wma_price_type == "weighted_close":
            return candle.weighted_close()
        if self.wma_price_type == "mid_price":
            return candle.mid_price()
        return candle.close

    def trend_from_price(self, wma: Optional[float], price: Optional[float]) -> str:
        """Classify trend from a given price vs WMA."""
        if wma is None or price is None:
            return "UNKNOWN"
        upper = wma * (1 + self.wma_threshold)
        lower = wma * (1 - self.wma_threshold)
        if price > upper:
            return "UP"
        if price < lower:
            return "DOWN"
        return "FLAT"

    def update_trend_streak(self, trend: str) -> int:
        """Track consecutive completed candles with same trend state."""
        if trend in ("UP", "DOWN"):
            if trend == self.trend_state:
                self._trend_streak += 1
            else:
                self._trend_streak = 1
        else:
            self._trend_streak = 0
        return self._trend_streak

    def update_trend_state(self, wma: Optional[float], price: Optional[float]) -> str:
        """
        Hysteresis trend state machine based on bps distance to WMA.
        """
        if wma is None or price is None or wma == 0:
            self.trend_state = "UNKNOWN"
            return self.trend_state

        delta_bps = ((price - wma) / wma) * 10000

        if self.trend_state == "UP":
            if delta_bps <= -self.trend_exit_bps:
                self.trend_state = "DOWN"
            else:
                self.trend_state = "UP"
            return self.trend_state

        if self.trend_state == "DOWN":
            if delta_bps >= self.trend_exit_bps:
                self.trend_state = "UP"
            else:
                self.trend_state = "DOWN"
            return self.trend_state

        if delta_bps >= self.trend_enter_bps:
            self.trend_state = "UP"
        elif delta_bps <= -self.trend_enter_bps:
            self.trend_state = "DOWN"
        else:
            self.trend_state = "FLAT"

        return self.trend_state

    def _calculate_wma_from_values(self, values: List[float]) -> Optional[float]:
        """Simple WMA helper for slope calculation."""
        if not values:
            return None
        weights = range(1, len(values) + 1)
        weighted_sum = sum(v * w for v, w in zip(values, weights))
        return weighted_sum / sum(weights)

    def get_wma_slope_bps(self) -> float:
        """
        WMA slope in bps between latest and prior complete-candle windows.
        Positive = rising WMA, negative = falling WMA.
        """
        candles = list(self.candle_builder.candles)
        shift = max(1, self.wma_slope_shift_candles)
        if len(candles) < self.wma_period + shift:
            return 0.0

        latest_window = candles[-self.wma_period :]
        prior_window = candles[-(self.wma_period + shift) : -shift]
        latest_vals = [self.candle_price(c) for c in latest_window]
        prior_vals = [self.candle_price(c) for c in prior_window]

        wma_now = self._calculate_wma_from_values(latest_vals)
        wma_prev = self._calculate_wma_from_values(prior_vals)
        if not wma_now or not wma_prev or wma_prev == 0:
            return 0.0
        return ((wma_now - wma_prev) / wma_prev) * 10000

    def get_true_structure_levels(self) -> tuple[Optional[float], Optional[float]]:
        """Return (support, resistance) using CandleBuilder output."""
        structure = self.candle_builder.get_structure_stats()
        support = structure.get("last_support")
        resistance = structure.get("last_resistance")
        return support, resistance

    def unified_signal(
        self,
        bid: float,
        ask: float,
        eval_price: Optional[float],
        wma: Optional[float],
        trend: str,
    ) -> Dict:
        """Return one unified signal decision for this candle."""
        mid = (bid + ask) / 2
        spread = ask - bid
        spread_bps = (spread / mid) * 10000

        streak = self.update_trend_streak(trend)
        support, resistance = self.get_true_structure_levels()
        wma_slope_bps = self.get_wma_slope_bps()

        if wma is None or eval_price is None:
            return {
                "signal": "NO_TRADE",
                "reason": "No WMA/current price yet",
                "spread_bps": spread_bps,
                "trend_streak": streak,
                "wma_distance_bps": 0.0,
                "wma_slope_bps": wma_slope_bps,
                "support": support,
                "resistance": resistance,
            }

        wma_distance_bps = abs((eval_price - wma) / wma) * 10000

        structure_block_long = False
        structure_block_short = False
        if support:
            support_break = support * (1 - self.structure_break_buffer_bps / 10000)
            structure_block_long = eval_price < support_break
        if resistance:
            resistance_break = resistance * (1 + self.structure_break_buffer_bps / 10000)
            structure_block_short = eval_price > resistance_break

        can_trade = spread_bps >= self.spread_threshold_bps
        has_persistence = streak >= self.min_trend_streak
        far_enough_from_wma = wma_distance_bps >= self.min_wma_distance_bps
        slope_long_ok = wma_slope_bps >= self.min_wma_slope_bps
        slope_short_ok = wma_slope_bps <= -self.min_wma_slope_bps

        long_ok = (
            trend == "UP"
            and can_trade
            and has_persistence
            and far_enough_from_wma
            and slope_long_ok
            and not structure_block_long
        )
        short_ok = (
            trend == "DOWN"
            and can_trade
            and has_persistence
            and far_enough_from_wma
            and slope_short_ok
            and not structure_block_short
        )

        if long_ok:
            return {
                "signal": "LONG",
                "reason": "UP trend + persistence + WMA distance + spread",
                "spread_bps": spread_bps,
                "trend_streak": streak,
                "wma_distance_bps": wma_distance_bps,
                "wma_slope_bps": wma_slope_bps,
                "support": support,
                "resistance": resistance,
            }

        if short_ok:
            return {
                "signal": "SHORT",
                "reason": "DOWN trend + persistence + WMA distance + spread",
                "spread_bps": spread_bps,
                "trend_streak": streak,
                "wma_distance_bps": wma_distance_bps,
                "wma_slope_bps": wma_slope_bps,
                "support": support,
                "resistance": resistance,
            }

        return {
            "signal": "NO_TRADE",
            "reason": "Filter blocked (spread/trend/persistence/distance/structure)",
            "spread_bps": spread_bps,
            "trend_streak": streak,
            "wma_distance_bps": wma_distance_bps,
            "wma_slope_bps": wma_slope_bps,
            "support": support,
            "resistance": resistance,
        }

    def get_wma_stats(self) -> dict:
        """Get current WMA statistics for logging/debugging."""
        wma = self.candle_builder.calculate_wma(self.wma_period, self.wma_price_type)
        current_price = None

        if self.candle_builder.current_candle:
            if self.wma_price_type == 'weighted_close':
                current_price = self.candle_builder.current_candle.weighted_close()
            elif self.wma_price_type == 'mid_price':
                current_price = self.candle_builder.current_candle.mid_price()
            else:
                current_price = self.candle_builder.current_candle.close

        return {
            'wma': wma,
            'current_price': current_price,
            'trend': self.get_trend(),
            'total_candles': len(self.candle_builder.candles)
        }

    async def exit_orphan(self, filled_order: TrackedOrder):
        """
        Chase-then-kill exit strategy for orphan positions.

        Timeline:
        - t=0s: Place exit @ 80% into spread (ALO)
        - t=3s: Reprice to 90% into spread (ALO)
        - t=5s: Market order (kill)

        Emergency: If loss > $0.02, market order immediately
        """
        self.orphan_in_progress = True
        self.orphan_exits_attempted += 1

        # Determine exit side
        exit_side = OrderSide.SELL if filled_order.side == OrderSide.BUY else OrderSide.BUY
        position_type = "LONG" if filled_order.side == OrderSide.BUY else "SHORT"

        print(f"\n{'='*80}")
        print(f"[ORPHAN DETECTED] {position_type} position @ ${filled_order.price:.2f}")
        print(f"[ORPHAN EXIT] Starting chase-then-kill strategy")
        print(f"{'='*80}")

        entry_time = datetime.now(timezone.utc).timestamp()
        current_order = None
        phase = 1

        try:
            while True:
                elapsed = datetime.now(timezone.utc).timestamp() - entry_time

                # Get current market
                bid = self.current_bid
                ask = self.current_ask
                mid = self.current_mid

                if not all([bid, ask, mid]):
                    await asyncio.sleep(0.1)
                    continue

                age = self._quote_age_ms()
                if age is None or age > self.max_quote_age_ms:
                    fresh = await self.ensure_fresh_quote("exit_orphan", wait_timeout_s=0.5)
                    if not fresh:
                        await asyncio.sleep(0.1)
                        continue
                    bid = self.current_bid
                    ask = self.current_ask
                    mid = self.current_mid

                # Calculate unrealized P&L
                pnl = self.calculate_unrealized_pnl(
                    filled_order.price,
                    filled_order.side,
                    mid,
                    filled_order.size
                )

                # EMERGENCY EXIT: Loss > $0.02
                if pnl < -self.max_orphan_loss:
                    print(f"\n[EMERGENCY] Loss ${pnl:.4f} > ${self.max_orphan_loss:.2f} threshold")
                    print(f"[EMERGENCY] Executing market order NOW")

                    if current_order:
                        try:
                            self.client.cancel_order(self.coin, current_order.order_id)
                        except:
                            pass

                    # Market order (aggressive limit that crosses spread)
                    try:
                        # Place limit far from market to guarantee fill (taker order)
                        if exit_side == OrderSide.SELL:
                            aggressive_price = bid * 0.995  # 0.5% below bid
                        else:
                            aggressive_price = ask * 1.005  # 0.5% above ask

                        aggressive_price = self.client.format_price(self.coin, aggressive_price)

                        market_order = self.client.place_limit_order(
                            coin=self.coin,
                            side=exit_side,
                            price=aggressive_price,
                            size=filled_order.size,
                            reduce_only=True,
                            post_only=False,  # Allow crossing spread (taker)
                            dry_run=self.dry_run
                        )

                        if market_order:
                            self.orphan_market_orders += 1
                            current_order = TrackedOrder(
                                side=exit_side,
                                price=aggressive_price,
                                size=filled_order.size,
                                order_id=market_order.order_id,
                                status="OPEN",
                                timestamp=datetime.now(timezone.utc).timestamp()
                            )
                            print(f"[EMERGENCY EXIT] Order placed @ ${aggressive_price:.2f} | Waiting for fill...", flush=True)

                            # Wait up to 3 seconds for market order to fill
                            emergency_wait_start = datetime.now(timezone.utc).timestamp()
                            while datetime.now(timezone.utc).timestamp() - emergency_wait_start < 3.0:
                                await asyncio.sleep(0.2)
                                try:
                                    open_orders = self.client.get_open_orders(self.coin)
                                    open_order_ids = {o.order_id for o in open_orders}

                                    if market_order.order_id not in open_order_ids:
                                        # Filled!
                                        wait_time = datetime.now(timezone.utc).timestamp() - emergency_wait_start
                                        loss = abs(pnl)
                                        self.total_loss += loss
                                        print(f"[EMERGENCY EXIT] Filled after {wait_time:.1f}s wait | Loss: ${loss:.4f}", flush=True)
                                        break
                                except Exception as e:
                                    print(f"[ERROR] Checking emergency fill: {e}", flush=True)
                            else:
                                # Timeout waiting for fill - cleanup will cancel it
                                print(f"[WARNING] Emergency order did not fill after 3s - cleanup will cancel", flush=True)
                    except Exception as e:
                        print(f"[ERROR] Emergency exit failed: {e}", flush=True)

                    break

                # PHASE 1: Initial exit @ 80% into spread (0-3s)
                if phase == 1 and elapsed < self.orphan_chase_time_1:
                    if current_order is None:
                        chase_price = self.calculate_chase_price(bid, ask, exit_side, aggression=0.80)
                        chase_price = self.client.format_price(self.coin, chase_price)

                        try:
                            order_result = self.client.place_limit_order(
                                coin=self.coin,
                                side=exit_side,
                                price=chase_price,
                                size=filled_order.size,
                                reduce_only=True,
                                post_only=True,
                                dry_run=self.dry_run
                            )

                            if order_result:
                                current_order = TrackedOrder(
                                    side=exit_side,
                                    price=chase_price,
                                    size=filled_order.size,
                                    order_id=order_result.order_id,
                                    status="OPEN",
                                    timestamp=datetime.now(timezone.utc).timestamp()
                                )
                                print(f"[PHASE 1] Exit order @ ${chase_price:.2f} (80% into spread) | P&L: ${pnl:.4f}")
                        except Exception as e:
                            print(f"[ERROR] Phase 1 order failed: {e}")

                # PHASE 2: Reprice to 90% into spread (3-5s)
                elif phase == 1 and elapsed >= self.orphan_chase_time_1 and elapsed < self.orphan_max_time:
                    phase = 2

                    # Cancel phase 1 order
                    if current_order:
                        try:
                            self.client.cancel_order(self.coin, current_order.order_id)
                            print(f"[PHASE 2] Repricing after {elapsed:.1f}s")
                        except Exception as e:
                            print(f"[ERROR] Cancel failed: {e}")

                    # Place more aggressive order
                    chase_price = self.calculate_chase_price(bid, ask, exit_side, aggression=0.90)
                    chase_price = self.client.format_price(self.coin, chase_price)

                    try:
                        order_result = self.client.place_limit_order(
                            coin=self.coin,
                            side=exit_side,
                            price=chase_price,
                            size=filled_order.size,
                            reduce_only=True,
                            post_only=True,
                            dry_run=self.dry_run
                        )

                        if order_result:
                            current_order = TrackedOrder(
                                side=exit_side,
                                price=chase_price,
                                size=filled_order.size,
                                order_id=order_result.order_id,
                                status="OPEN",
                                timestamp=datetime.now(timezone.utc).timestamp()
                            )
                            print(f"[PHASE 2] Exit order @ ${chase_price:.2f} (90% into spread) | P&L: ${pnl:.4f}")
                    except Exception as e:
                        print(f"[ERROR] Phase 2 order failed: {e}")

                # PHASE 3: Market order kill (5s+)
                elif elapsed >= self.orphan_max_time:
                    phase = 3
                    print(f"\n[KILL] Max time ({self.orphan_max_time}s) reached")

                    # Cancel limit order
                    if current_order:
                        try:
                            self.client.cancel_order(self.coin, current_order.order_id)
                        except:
                            pass

                    # Market order (aggressive limit that crosses spread)
                    try:
                        # Place limit far from market to guarantee fill (taker order)
                        if exit_side == OrderSide.SELL:
                            aggressive_price = bid * 0.995  # 0.5% below bid
                        else:
                            aggressive_price = ask * 1.005  # 0.5% above ask

                        aggressive_price = self.client.format_price(self.coin, aggressive_price)

                        market_order = self.client.place_limit_order(
                            coin=self.coin,
                            side=exit_side,
                            price=aggressive_price,
                            size=filled_order.size,
                            reduce_only=True,
                            post_only=False,  # Allow crossing spread (taker)
                            dry_run=self.dry_run
                        )

                        if market_order:
                            self.orphan_market_orders += 1
                            current_order = TrackedOrder(
                                side=exit_side,
                                price=aggressive_price,
                                size=filled_order.size,
                                order_id=market_order.order_id,
                                status="OPEN",
                                timestamp=datetime.now(timezone.utc).timestamp()
                            )
                            print(f"[MARKET EXIT] Order placed @ ${aggressive_price:.2f} | Waiting for fill...", flush=True)

                            # Wait up to 3 seconds for market order to fill
                            market_wait_start = datetime.now(timezone.utc).timestamp()
                            while datetime.now(timezone.utc).timestamp() - market_wait_start < 3.0:
                                await asyncio.sleep(0.2)
                                try:
                                    open_orders = self.client.get_open_orders(self.coin)
                                    open_order_ids = {o.order_id for o in open_orders}

                                    if market_order.order_id not in open_order_ids:
                                        # Filled!
                                        wait_time = datetime.now(timezone.utc).timestamp() - market_wait_start
                                        estimated_loss = abs(pnl) if pnl < 0 else 0
                                        self.total_loss += estimated_loss
                                        print(f"[MARKET EXIT] Filled after {wait_time:.1f}s wait | Est. Loss: ${estimated_loss:.4f}", flush=True)
                                        break
                                except Exception as e:
                                    print(f"[ERROR] Checking market fill: {e}", flush=True)
                            else:
                                # Timeout waiting for fill - cleanup will cancel it
                                print(f"[WARNING] Market order did not fill after 3s - cleanup will cancel", flush=True)
                    except Exception as e:
                        print(f"[ERROR] Market exit failed: {e}", flush=True)

                    break

                # Check if current order filled
                if current_order:
                    try:
                        open_orders = self.client.get_open_orders(self.coin)
                        open_order_ids = {o.order_id for o in open_orders}

                        if current_order.order_id not in open_order_ids:
                            # Filled!
                            self.orphan_exits_filled += 1
                            if pnl >= 0:
                                self.total_profit += pnl
                            else:
                                self.total_loss += abs(pnl)

                            print(f"\n{'*'*80}")
                            print(f"[EXIT SUCCESS] Orphan closed in {elapsed:.1f}s")
                            print(f"Entry: ${filled_order.price:.2f} | Exit: ${current_order.price:.2f}")
                            print(f"P&L: ${pnl:.4f}")
                            print(f"{'*'*80}\n")
                            break
                    except Exception as e:
                        print(f"[ERROR] Checking fills: {e}")

                # Wait before next check
                await asyncio.sleep(0.5)

        except Exception as e:
            print(f"[ERROR] Orphan exit error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # Cleanup: Cancel any remaining open orders
            try:
                print(f"[CLEANUP] Checking for remaining open orders...", flush=True)
                open_orders = self.client.get_open_orders(self.coin)
                if open_orders:
                    print(f"[CLEANUP] Found {len(open_orders)} open orders, cancelling...", flush=True)
                    for order in open_orders:
                        try:
                            self.client.cancel_order(self.coin, order.order_id)
                            print(f"[CLEANUP] Cancelled order {order.order_id}", flush=True)
                        except Exception as e:
                            print(f"[CLEANUP ERROR] Failed to cancel {order.order_id}: {e}", flush=True)
                else:
                    print(f"[CLEANUP] No open orders remaining", flush=True)
            except Exception as e:
                print(f"[CLEANUP ERROR] Failed to check orders: {e}", flush=True)

            self.orphan_in_progress = False

    async def check_fills(self):
        """
        Check if orders have filled.
        
        Simplified logic:
        1. If we have active tracked orders, check if they're still on the book
        2. If orders are gone from book, check position to determine outcome
        3. If state is ambiguous, use ensure_flat() to clean up
        """
        # Don't check for new fills if we're handling an orphan
        if self.orphan_in_progress:
            return

        if not self.active_orders:
            return

        print(f"[DEBUG] Checking fills for {len(self.active_orders)} active orders", flush=True)

        try:
            # Get current state from exchange
            open_orders = self.client.get_open_orders(self.coin)
            open_order_ids = {o.order_id for o in open_orders}
            position_info = self.client.get_position(self.coin)
            position_size = position_info.size if position_info else 0.0
            
            print(f"[DEBUG] Exchange: {len(open_order_ids)} open orders, position={position_size:.4f}", flush=True)
            
            # Track for other uses
            self.last_position_size = position_size
            self.last_position_ts = datetime.now(timezone.utc).timestamp()
            
            # Count how many of our tracked orders are still on the book
            tracked_ids = {o.order_id for o in self.active_orders if o.order_id}
            still_open = tracked_ids & open_order_ids
            gone_from_book = tracked_ids - open_order_ids
            
            # Case 1: All our orders are still on the book - nothing to do
            if len(still_open) == len(tracked_ids):
                return
            
            # Case 2: Some/all orders gone from book - need to determine what happened
            print(f"[FILLS] {len(gone_from_book)} orders gone from book, {len(still_open)} still open", flush=True)
            
            # If we have a position, at least one order filled
            if abs(position_size) > 0:
                print(f"[FILLS] Position exists ({position_size:.4f}) - at least one fill occurred", flush=True)
                
                # If all orders gone and we have position = one side filled (orphan)
                # If some orders still open and we have position = partial state
                # Either way, ensure_flat will handle it
                print(f"[FILLS] Calling ensure_flat to resolve state", flush=True)
                await self.ensure_flat()
                return
            
            # Case 3: No position and orders gone = cancelled/rejected
            if len(gone_from_book) > 0 and position_size == 0:
                # Wait for confirmation (API lag)
                self._open_orders_empty_streak += 1
                if self._open_orders_empty_streak < 2:
                    print(f"[FILLS] Orders gone but no position - waiting for confirmation ({self._open_orders_empty_streak}/2)", flush=True)
                    return
                
                # Confirmed: orders were cancelled/rejected, no position
                print(f"[FILLS] Confirmed: orders cancelled/rejected with no position", flush=True)
                self._open_orders_empty_streak = 0
                self.active_orders.clear()
                self.open_positions = 0
                return
            
            # Reset streak if we have open orders
            if still_open:
                self._open_orders_empty_streak = 0

        except Exception as e:
            print(f"[ERROR] Checking fills: {e}", flush=True)

    async def monitor_orderbook(self):
        """Monitor orderbook and execute market making strategy"""
        print("="*80)
        print(f"AUTOMATED MARKET MAKER - {self.coin}")
        print("="*80)
        print(f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"\nParameters:")
        print(f"  Spread threshold: {self.spread_threshold_bps:.1f} bps")
        print(f"  Position size:    ${self.position_size_usd:.2f}")
        print(f"  Spread position:  {self.spread_position:.1%}")
        print(f"  Max positions:    {self.max_positions}")
        print(f"  Trade cooldown:   {self.min_trade_interval:.0f}s")
        print(f"\nWMA Trend Detection (Phase 3: Smart Exits):")
        print(f"  WMA Period:       {self.wma_period}")
        print(f"  Price Type:       {self.wma_price_type}")
        print(f"  Trend Threshold:  {self.wma_threshold:.2%}")
        print(f"  Trend Enter/Exit: {self.trend_enter_bps:.1f}/{self.trend_exit_bps:.1f} bps")
        print(f"  Min Streak:       {self.min_trend_streak} candles")
        print(f"  Min WMA Dist:     {self.min_wma_distance_bps:.1f} bps")
        print(f"  Min WMA Slope:    {self.min_wma_slope_bps:.1f} bps/candle")
        print(f"  WMA Slope Shift:  {self.wma_slope_shift_candles} candles")
        print(f"  Structure Buffer: {self.structure_break_buffer_bps:.1f} bps")
        print(f"\nPosition Management:")
        print(f"  Take Profit:      +{self.take_profit_bps:.0f} bps")
        print(f"  Stop Loss:        -{self.stop_loss_bps:.0f} bps (active after {self.min_hold_time:.0f}s)")
        print(f"  Max Hold Time:    {self.max_hold_time:.0f}s")
        print(f"\nSafety Limits:")
        print(f"  Max trades:       {self.max_trades}")
        print(f"  Max loss:         ${self.max_loss:.2f}")
        print(f"\n  Mode:             {'DRY RUN' if self.dry_run else '*** LIVE TRADING ***'}")
        print("="*80 + "\n")

        async with websockets.connect(self.ws_url) as ws:
            # Subscribe to L2 orderbook
            subscribe_msg = {
                "method": "subscribe",
                "subscription": {
                    "type": "l2Book",
                    "coin": self.coin
                }
            }
            await ws.send(json.dumps(subscribe_msg))

            print("[OK] Connected to orderbook feed\n")

            ping_task = asyncio.create_task(self.ping_ws(ws))
            self._trade_task = asyncio.create_task(self.trade_worker())
            self._fills_task = asyncio.create_task(self.fills_worker())
            self._watchdog_task = asyncio.create_task(self.book_watchdog())

            try:
                async for message in ws:
                    try:
                        data = json.loads(message)

                        if data.get('channel') == 'l2Book':
                            book_data = data.get('data', {})
                            levels = book_data.get('levels', [[], []])

                            if len(levels) == 2 and levels[0] and levels[1]:
                                bids = levels[0]
                                asks = levels[1]

                                bid = float(bids[0]['px'])
                                ask = float(asks[0]['px'])

                                self.current_bid = bid
                                self.current_ask = ask
                                self.current_mid = (bid + ask) / 2
                                self.last_book_update_ts = time.monotonic()

                                # ========================================
                                # UNIFIED SIGNAL: Use completed candles only
                                # ========================================
                                completed_candle = self.candle_builder.update(bid, ask)

                                if completed_candle:
                                    eval_price = self.candle_price(completed_candle)
                                    wma = self.candle_builder.calculate_wma(self.wma_period, self.wma_price_type)
                                    raw_trend = self.trend_from_price(wma, eval_price)
                                    trend = self.update_trend_state(wma, eval_price)

                                    self.last_eval_price = eval_price
                                    self.last_wma = wma
                                    self.last_raw_trend = raw_trend

                                    signal = self.unified_signal(
                                        bid=bid,
                                        ask=ask,
                                        eval_price=eval_price,
                                        wma=wma,
                                        trend=trend,
                                    )
                                    self.latest_signal = signal
                                    self.latest_signal_ts = time.monotonic()

                                    # Log trend changes
                                    if trend != self.last_trend:
                                        print(f"\n{'='*60}", flush=True)
                                        print(f"[WMA TREND CHANGE] {self.last_trend} → {trend} (raw {raw_trend})", flush=True)
                                        if wma and eval_price:
                                            print(f"Price: ${eval_price:.3f} | WMA: ${wma:.3f}", flush=True)
                                        print(f"Candles: {len(self.candle_builder.candles)}", flush=True)
                                        print(f"{'='*60}\n", flush=True)
                                        self.last_trend = trend

                                # Calculate spread for logging
                                spread_abs = ask - bid
                                spread_bps = (spread_abs / self.current_mid) * 10000

                                # Log every 100th update to show it's working
                                if not hasattr(self, '_update_count'):
                                    self._update_count = 0
                                self._update_count += 1

                                if self._update_count % 100 == 0:
                                    wma_str = f"WMA: ${self.last_wma:.3f}" if self.last_wma else "WMA: Building..."
                                    trend_str = self.last_trend
                                    cp_str = f"${self.last_eval_price:.3f}" if self.last_eval_price else "n/a"

                                    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                                          f"Bid: ${bid:.2f} | Ask: ${ask:.2f} | "
                                          f"Spread: ${spread_abs:.2f} ({spread_bps:.2f} bps) | "
                                          f"Price: {cp_str} | {wma_str} | Trend: {trend_str}", flush=True)

                                # Check for opportunity only on completed candle signal
                                if completed_candle and self.latest_signal:
                                    if self.latest_signal.get("signal") in ("LONG", "SHORT"):
                                        opportunity = self.should_enter(bid, ask)
                                        if opportunity:
                                            self.enqueue_opportunity(opportunity)

                    except Exception as e:
                        print(f"[ERROR] {e}")
            finally:
                ping_task.cancel()
                if self._trade_task:
                    self._trade_task.cancel()
                if self._fills_task:
                    self._fills_task.cancel()
                if self._watchdog_task:
                    self._watchdog_task.cancel()
                with suppress(asyncio.CancelledError):
                    await ping_task
                with suppress(asyncio.CancelledError):
                    if self._trade_task:
                        await self._trade_task
                with suppress(asyncio.CancelledError):
                    if self._fills_task:
                        await self._fills_task
                with suppress(asyncio.CancelledError):
                    if self._watchdog_task:
                        await self._watchdog_task

    async def ping_ws(self, ws):
        """Periodically measure websocket RTT using ping/pong."""
        while True:
            await asyncio.sleep(self.ws_ping_interval_s)
            try:
                start = time.perf_counter()
                pong_waiter = ws.ping()
                await asyncio.wait_for(pong_waiter, timeout=self.ws_ping_timeout_s)
                rtt_ms = (time.perf_counter() - start) * 1000
                print(f"[LATENCY] WS ping RTT: {rtt_ms:.1f} ms", flush=True)
            except asyncio.TimeoutError:
                print("[LATENCY] WS ping timeout", flush=True)
            except Exception as e:
                print(f"[LATENCY] WS ping error: {e}", flush=True)
                return

    async def run(self):
        """Run the market maker"""
        try:
            await self.monitor_orderbook()
        except KeyboardInterrupt:
            print("\n\n" + "="*80)
            print("[STOPPED] User interrupted")
            print("="*80)
            self.print_stats()
            print("="*80)
        except Exception as e:
            print(f"\n[ERROR] {e}")
            import traceback
            traceback.print_exc()
        finally:
            await self.cleanup_on_exit()

    async def cleanup_on_exit(self):
        """Attempt to cancel open orders and exit any leftover position."""
        if self.orphan_in_progress:
            return

        print("[CLEANUP] Cancelling open orders on exit", flush=True)
        try:
            self.client.cancel_all_orders(self.coin)
        except Exception as e:
            print(f"[CLEANUP ERROR] Failed to cancel open orders: {e}", flush=True)

        try:
            position_info = self.client.get_position(self.coin)
            if position_info and abs(position_info.size) > 0:
                print(f"[CLEANUP] Open position detected ({position_info.size:.4f}); exiting", flush=True)
                if not all([self.current_bid, self.current_ask, self.current_mid]):
                    self.refresh_quotes()
                filled = TrackedOrder(
                    side=OrderSide.BUY if position_info.size > 0 else OrderSide.SELL,
                    price=position_info.entry_price or self.current_mid or 0.0,
                    size=abs(position_info.size),
                    status="FILLED",
                    timestamp=datetime.now(timezone.utc).timestamp(),
                    filled_timestamp=datetime.now(timezone.utc).timestamp(),
                )
                await self.exit_orphan(filled)
        except Exception as e:
            print(f"[CLEANUP ERROR] Failed to exit position: {e}", flush=True)

    def print_stats(self):
        """Print performance statistics"""
        print("\nPERFORMANCE STATS:")
        print(f"  Opportunities seen:  {self.opportunities_seen}")
        print(f"  Trades attempted:    {self.trades_attempted}")
        print(f"\nPHASE 2 - Directional Entries:")
        print(f"  LONG entries:        {self.long_entries}")
        print(f"  SHORT entries:       {self.short_entries}")
        print(f"  Fills:               {self.one_filled}")
        if self.both_filled > 0:
            print(f"  (Legacy both-fills:  {self.both_filled})")
        print(f"\nORPHAN EXIT STATS:")
        print(f"  Orphan exits attempted: {self.orphan_exits_attempted}")
        print(f"  Orphan exits filled:    {self.orphan_exits_filled}")
        print(f"  Market orders used:     {self.orphan_market_orders}")
        if self.orphan_exits_attempted > 0:
            fill_rate = (self.orphan_exits_filled / self.orphan_exits_attempted) * 100
            print(f"  Limit fill success:     {fill_rate:.1f}%")
        print(f"\nP&L:")
        print(f"  Total profit:        ${self.total_profit:.2f}")
        print(f"  Total loss:          ${self.total_loss:.2f}")
        print(f"  Net P&L:             ${self.total_profit - self.total_loss:.2f}")


async def main():
    """Main entry point"""
    try:
        # Create XYZ client
        print("Initializing XYZ client...", flush=True)
        client = XYZClient(
            wallet_address=WALLET_ADDRESS,
            private_key=PRIVATE_KEY,
            account_address=ACCOUNT_ADDRESS,
            testnet=False
        )
        print("Client initialized\n", flush=True)

        # Clean up any lingering orders from previous runs
        try:
            cancelled = client.cancel_all_orders("xyz:SILVER")
            if cancelled:
                print(f"[STARTUP] Cancelled {cancelled} open orders", flush=True)
        except Exception as e:
            print(f"[STARTUP WARNING] Failed to cancel open orders: {e}", flush=True)

        # Create market maker
        print("Creating market maker...", flush=True)
        mm = MarketMaker(
            client=client,
            coin="xyz:SILVER",
            spread_threshold_bps=6.0,       # Only trade when spread > 6 bps
            position_size_usd=11.0,         # $11 per trade
            spread_position=0.2,            # 20% into spread (closer to edges)
            max_patience_ms=300,            # 300ms patience
            max_positions=1,                # One at a time
            max_trades=999999,              # No practical limit
            max_loss=5.0,                   # Stop if lose $5
            min_trade_interval=5.0,         # 5 second cooldown between trades
            dry_run=False,                  # LIVE MODE
            max_quote_age_ms=1200.0,        # Speed-prioritized freshness gate
            ws_stale_timeout_s=15.0,        # Reduce REST refresh frequency
            wma_period=60,                  # 60-period WMA (5 minutes)
            wma_price_type="weighted_close",
            wma_threshold=0.0005,           # 0.05% buffer
            candle_interval_seconds=5,
            max_candles=400,                # Enough for swing detection window
            min_trend_streak=2,
            min_wma_distance_bps=3.0,
            min_wma_slope_bps=0.8,
            trend_enter_bps=4.0,
            trend_exit_bps=8.0,
            wma_slope_shift_candles=3,
            structure_break_buffer_bps=3.0,
            signal_ttl_s=6.0
        )

        print("Starting market maker...", flush=True)
        await mm.run()
    except Exception as e:
        print(f"\n[FATAL ERROR] {e}", flush=True)
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    print("=" * 80, flush=True)
    print("MARKET MAKER STARTING", flush=True)
    print("=" * 80, flush=True)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[STOPPED] Keyboard interrupt", flush=True)
    except Exception as e:
        print(f"\n[ERROR] Main loop failed: {e}", flush=True)
        import traceback
        traceback.print_exc()
