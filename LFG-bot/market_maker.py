"""
Directional Trend Bot for HIP-3 Pairs

Strategy (Trend Streak with Market Orders):
1. Build 5-second candles from streaming bid/ask data
2. Calculate 60-period WMA using weighted close (H+L+C+C)/4
3. Determine trend state with hysteresis (UP/DOWN/FLAT)
4. Count consecutive trend streaks
5. Enter after 5 consecutive UP (LONG) or DOWN (SHORT) trends
6. Market entries and exits (taker orders)
7. Exit on: stop loss, trailing take-profit, opposite 5-in-a-row streak, or bias-flip

Key parameters:
- position_size_usd: Size per trade (default: $11)
- stop_loss_pct: Stop loss as percent of position notional (default: 6%)
- wma_period: WMA period (default: 60 = 5 minutes)
- required_streak: Consecutive trends to trigger entry (default: 5)
"""
import asyncio
import websockets
import json
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional
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
class Opportunity:
    """A trend-based entry opportunity"""
    bid: float
    ask: float
    timestamp: float
    trend: str  # 'UP', 'DOWN', 'FLAT', or 'UNKNOWN'


class MarketMaker:
    """Directional trend trading bot"""

    def __init__(
        self,
        client: XYZClient,
        coin: str = "xyz:SILVER",
        position_size_usd: float = 10.0,
        max_positions: int = 1,
        max_trades: int = 10,
        max_loss: float = 5.0,
        min_trade_interval: float = 0.0,
        post_exit_cooldown_s: float = 120.0,
        dry_run: bool = True,
        max_quote_age_ms: float = 500.0,
        ws_stale_timeout_s: float = 5.0,
        opportunity_queue_size: int = 1,
        wma_period: int = 60,
        wma_price_type: str = "weighted_close",
        wma_threshold: float = 0.0005,
        candle_interval_seconds: int = 5,
        max_candles: int = 400,
        trend_enter_bps: float = 4.0,
        trend_exit_bps: float = 8.0,
        wma_slope_shift_candles: int = 3,
        min_wma_slope_bps: float = 0.8,
        bias_candle_interval_seconds: int = 60,
        bias_max_candles: int = 1200,
        bias_wma_period: int = 45,
        bias_price_type: str = "weighted_close",
        bias_enter_bps: float = 4.0,
        bias_exit_bps: float = 12.0,
        bias_slope_shift_candles: int = 3,
        bias_min_slope_bps: float = 0.4,
        bias_confirm_candles: int = 2,
        # Trailing take-profit
        trailing_tp_activation_bps: float = 20.0,
        trailing_tp_trail_bps: float = 25.0,
        # Bias-flip exit
        exit_on_bias_flip: bool = True,
        # Max hold time
        max_hold_seconds: float = 3600.0,
    ):
        self.client = client
        self.coin = coin
        self.position_size_usd = position_size_usd
        self.max_positions = max_positions
        self.max_trades = max_trades
        self.max_loss = max_loss
        self.min_trade_interval = min_trade_interval
        self.post_exit_cooldown_s = post_exit_cooldown_s
        self.dry_run = dry_run
        self.max_quote_age_ms = max_quote_age_ms
        self.ws_stale_timeout_s = ws_stale_timeout_s
        self.opportunity_queue_size = opportunity_queue_size

        # State
        self.ws_url = "wss://api.hyperliquid.xyz/ws"
        self.current_bid = None
        self.current_ask = None
        self.current_mid = None

        # Position tracking
        self.open_positions = 0
        self.entry_in_flight = False

        # Stats
        self.opportunities_seen = 0
        self.trades_attempted = 0
        self.one_filled = 0
        self.long_entries = 0  # Track directional entries
        self.short_entries = 0  # Track directional entries
        self.total_profit = 0.0
        self.total_loss = 0.0

        # Cooldown tracking
        self.last_placement_time = 0.0
        self.last_exit_time = 0.0

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
        self._watchdog_task = None

        # WMA trend detection (Streak strategy)
        self.candle_builder = CandleBuilder(
            candle_interval_seconds=candle_interval_seconds,
            max_candles=max_candles
        )
        self.wma_period = wma_period
        self.wma_price_type = wma_price_type  # (H+L+C+C)/4
        self.wma_threshold = wma_threshold
        self.trend_enter_bps = trend_enter_bps
        self.trend_exit_bps = trend_exit_bps
        self.wma_slope_shift_candles = wma_slope_shift_candles
        self.min_wma_slope_bps = min_wma_slope_bps

        # Bias trend detection (higher-timeframe gate)
        self.bias_candle_builder = CandleBuilder(
            candle_interval_seconds=bias_candle_interval_seconds,
            max_candles=bias_max_candles
        )
        self.bias_candle_interval_seconds = bias_candle_interval_seconds
        self.bias_wma_period = bias_wma_period
        self.bias_price_type = bias_price_type
        self.bias_enter_bps = bias_enter_bps
        self.bias_exit_bps = bias_exit_bps
        self.bias_slope_shift_candles = bias_slope_shift_candles
        self.bias_min_slope_bps = bias_min_slope_bps
        self.bias_confirm_candles = max(1, bias_confirm_candles)

        # Trend state tracking
        self.last_trend = "UNKNOWN"
        self.trend_state = "UNKNOWN"
        self.last_eval_price: Optional[float] = None
        self.last_wma: Optional[float] = None
        self.last_raw_trend: str = "UNKNOWN"

        # Bias state tracking (higher-timeframe)
        self.bias_state = "UNKNOWN"
        self.bias_last_raw = "UNKNOWN"
        self.bias_confirm_up = 0
        self.bias_confirm_down = 0
        self.last_bias = "UNKNOWN"
        self.last_bias_wma: Optional[float] = None
        self.last_bias_eval_price: Optional[float] = None
        self.last_bias_slope_bps: Optional[float] = None
        self.last_bias_distance_bps: Optional[float] = None

        # Streak tracking
        self.required_streak = 5  # 5-in-a-row to trigger entry
        self.up_streak = 0
        self.down_streak = 0
        self.desired_position: Optional[str] = None  # "LONG", "SHORT", or None
        self.position: Optional[str] = None  # "LONG", "SHORT", or None

        # Position management parameters (Streak exits)
        self.stop_loss_pct = 0.04  # Exit at -4% of position notional (was 0.06, then 0.02)
        self.position_check_interval = 0.1  # Check every 0.1 seconds

        # Trailing take-profit
        self.trailing_tp_activation_bps = trailing_tp_activation_bps
        self.trailing_tp_trail_bps = trailing_tp_trail_bps

        # Bias-flip exit
        self.exit_on_bias_flip = exit_on_bias_flip

        # Max hold time
        self.max_hold_seconds = max_hold_seconds

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

    def get_live_position_size(self) -> float:
        """Return absolute live position size from exchange.

        Returns:
            >0: open position size
            0.0: confirmed flat (no position)
            -1.0: API error (unknown state - callers must NOT treat as flat)
        """
        try:
            position = self.client.get_position(self.coin)
            if position and abs(position.size) > 0:
                return abs(position.size)
            return 0.0
        except Exception as e:
            print(f"[POSITION] Live position check failed: {e}", flush=True)
            return -1.0

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

    def should_enter(self, bid: float, ask: float) -> Optional[Opportunity]:
        """
        Check if we should enter based on trend streak gating.

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

        # Don't enter if an entry is in flight
        if self.entry_in_flight:
            return None

        # Universal cooldown after any successful exit
        if self.last_exit_time > 0 and self.post_exit_cooldown_s > 0:
            time_since_exit = time.time() - self.last_exit_time
            if time_since_exit < self.post_exit_cooldown_s:
                if not hasattr(self, "_last_exit_cooldown_log_ts") or time.time() - self._last_exit_cooldown_log_ts > 10:
                    remaining = self.post_exit_cooldown_s - time_since_exit
                    print(f"[EXIT COOLDOWN] {remaining:.1f}s remaining before next entry", flush=True)
                    self._last_exit_cooldown_log_ts = time.time()
                return None

        # Exchange truth gate: if live position exists OR API fails, block new entries.
        # API error (-1.0) is treated as "assume position exists" for safety.
        live_size = self.get_live_position_size()
        if live_size != 0.0:
            self.open_positions = 1
            if not hasattr(self, "_live_position_gate_log_ts") or time.time() - self._live_position_gate_log_ts > 10:
                print(f"[LIVE POSITION BLOCK] Exchange shows open size={live_size:.6f} - skipping new entry", flush=True)
                self._live_position_gate_log_ts = time.time()
            return None

        # Don't enter if already at position limit
        if self.open_positions >= self.max_positions:
            if not hasattr(self, '_position_limit_log_ts') or time.time() - self._position_limit_log_ts > 10:
                print(f"[BLOCKED] Already have {self.open_positions} position(s) - ignoring signal", flush=True)
                self._position_limit_log_ts = time.time()
            return None

        if self.desired_position not in ("LONG", "SHORT"):
            return None
        trend = "UP" if self.desired_position == "LONG" else "DOWN"
        return Opportunity(
            bid=bid,
            ask=ask,
            timestamp=datetime.now(timezone.utc).timestamp(),
            trend=trend
        )

    async def place_order(self, opportunity: Opportunity):
        """
        Place single-sided order based on trend direction.

        Trend streak strategy (market entries):
        - Enter LONG after 5 consecutive UP trends
        - Enter SHORT after 5 consecutive DOWN trends

        Args:
            opportunity: The trend-based entry opportunity
        """
        self.opportunities_seen += 1

        # ====================================================================================
        # GATE: Block any new entries immediately (prevents race condition)
        # ====================================================================================
        print(f"[ENTRY GATE] Setting open_positions=1 to block duplicates", flush=True)
        self.open_positions = 1
        self.entry_in_flight = True

        # ====================================================================================
        # COOLDOWN: Enforce minimum time between order placements
        # ====================================================================================
        if self.last_placement_time > 0:
            time_since_last = time.time() - self.last_placement_time
            if time_since_last < self.min_trade_interval:
                cooldown_remaining = self.min_trade_interval - time_since_last
                print(f"[COOLDOWN] {cooldown_remaining:.1f}s remaining before next placement", flush=True)
                self.open_positions = 0
                self.entry_in_flight = False
                self.position = None
                return

        # ====================================================================================
        # FRESH QUOTE GATE: Avoid placing orders on stale data
        # ====================================================================================
        if not await self.ensure_fresh_quote("place_order"):
            print(f"[BLOCKED] Cannot place order - stale quote.", flush=True)
            self.open_positions = 0
            self.entry_in_flight = False
            return

        # ====================================================================================
        # Determine side and aggressive price from trend (taker)
        # ====================================================================================
        if opportunity.trend == 'UP':
            # Uptrend: Place BUY order (aggressive taker)
            side = OrderSide.BUY
            price = opportunity.ask * 1.0005
        elif opportunity.trend == 'DOWN':
            # Downtrend: Place SELL order (aggressive taker)
            side = OrderSide.SELL
            price = opportunity.bid * 0.9995
        else:
            print(f"[ERROR] Invalid trend for entry: {opportunity.trend}", flush=True)
            self.open_positions = 0
            self.entry_in_flight = False
            return

        # Calculate size to meet minimum notional
        position_size = self.position_size_usd / price

        print(f"\n{'='*80}", flush=True)
        print(f"[{opportunity.trend} TREND #{self.opportunities_seen}] {datetime.now(timezone.utc).strftime('%H:%M:%S')}", flush=True)
        print(f"{'='*80}", flush=True)
        print(f"Bid: ${opportunity.bid:.2f} | Ask: ${opportunity.ask:.2f}", flush=True)
        print(f"Placing {side.value}: ${price:.2f} | Size: {position_size:.4f}", flush=True)
        print(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}", flush=True)

        # ====================================================================================
        # PLACE SINGLE ORDER: Aggressive taker order
        # ====================================================================================
        try:
            place_market = getattr(self.client, "place_market_order", None)
            if callable(place_market):
                order = place_market(
                    coin=self.coin,
                    side=side,
                    size=position_size,
                    reduce_only=False,
                    dry_run=self.dry_run
                )
            else:
                order = self.client.place_limit_order(
                    coin=self.coin,
                    side=side,
                    price=price,
                    size=position_size,
                    reduce_only=False,
                    post_only=False,  # Taker order
                    dry_run=self.dry_run
                )
        except Exception as e:
            print(f"[ERROR] Order placement failed: {e}", flush=True)
            self.open_positions = 0
            self.entry_in_flight = False
            return

        # ====================================================================================
        # CHECK RESPONSE: If rejected, reset and move on
        # ====================================================================================
        if not order:
            print(f"[REJECTED] Order was rejected", flush=True)
            self.open_positions = 0
            self.entry_in_flight = False
            return

        status = self._normalize_status(getattr(order, "status", None))
        if status in ("REJECTED", "CANCELLED"):
            print(f"[REJECTED] Order was rejected", flush=True)
            self.open_positions = 0
            self.entry_in_flight = False
            return

        print(f"[SUCCESS] Order accepted! ID: {order.order_id}", flush=True)

        self.trades_attempted += 1
        self.last_placement_time = time.time()

        # Track entry side for stats
        self.position = "LONG" if side == OrderSide.BUY else "SHORT"
        if side == OrderSide.BUY:
            self.long_entries += 1
        else:
            self.short_entries += 1

        # CRITICAL: Clear desired_position immediately to prevent duplicate entries
        self.desired_position = None
        self.up_streak = 0
        self.down_streak = 0
        
        # Check if already filled during placement
        if hasattr(order, 'status') and order.status and 'filled' in str(order.status).lower():
            print(f"[INSTANT FILL] Order filled immediately!", flush=True)
            print(f"[INSTANT FILL] Starting monitor: side={order.side.value}, price=${order.price:.2f}, size={order.size:.6f}", flush=True)
            self.one_filled += 1
            await self.monitor_position(order.side, order.price, order.size)
            print(f"[INSTANT FILL] Monitor completed, returning from place_order", flush=True)
            return

        # Confirm position before monitoring exits
        print(f"[ENTRY COMPLETE] Checking position to start monitoring...", flush=True)
        try:
            position = self.client.get_position(self.coin)
            print(f"[POSITION CHECK] position={position}, size={position.size if position else 'N/A'}", flush=True)
            if position and abs(position.size) > 0:
                print(f"[MONITOR START] Starting position monitoring for {order.side.value} @ ${order.price:.2f}, size={abs(position.size):.6f}", flush=True)
                await self.monitor_position(order.side, order.price, abs(position.size))
                print(f"[MONITOR END] Position monitoring completed", flush=True)
            else:
                print(f"[WARNING] Position not found after entry - cannot monitor! position={position}, size={position.size if position else 'N/A'}", flush=True)
                self.open_positions = 0
                self.entry_in_flight = False
                self.position = None
        except Exception as e:
            print(f"[POSITION] Error checking position after entry: {e}", flush=True)
            import traceback
            traceback.print_exc()
            self.open_positions = 0
            self.entry_in_flight = False
            self.position = None

    async def monitor_position(self, entry_side: OrderSide, entry_price: float, size: float):
        """
        Monitor an open position for exit conditions.

        ALL TAKER EXITS - priority order:
        1. Stop Loss (-4% notional) -> Taker exit immediately
        2. Trailing Take-Profit (activate at +N bps, trail M bps from high) -> Taker exit
        3. Opposite 5-in-a-row streak -> Taker exit immediately
        4. Bias-flip exit (bias reverses against position) -> Taker exit immediately
        5. Max hold time -> Taker exit immediately

        Args:
            entry_side: The side we entered (BUY for long, SELL for short)
            entry_price: The price we entered at
            size: Position size
        """
        position_type = "LONG" if entry_side == OrderSide.BUY else "SHORT"
        print(f"\n{'='*60}", flush=True)
        print(f"[MONITOR_POSITION ENTRY] Function called! Monitoring {position_type} @ ${entry_price:.2f} | Size: {size:.4f}", flush=True)
        print(f"[POSITION] {position_type} @ ${entry_price:.2f} | Size: {size:.4f}", flush=True)
        print(
            f"[POSITION] Exit: Opposite 5-streak | "
            f"SL -{self.stop_loss_pct:.2%} notional | "
            f"Trail TP {self.trailing_tp_trail_bps:.0f}bps (activate +{self.trailing_tp_activation_bps:.0f}bps)"
            f"{' | Bias-flip' if self.exit_on_bias_flip else ''}"
            f" | Max hold {self.max_hold_seconds/60:.0f}min",
            flush=True
        )
        print(f"{'='*60}\n", flush=True)

        entry_time = time.time()

        # Trailing take-profit state
        trail_active = False
        trail_high_water_bps = 0.0  # Best mid-based P&L seen (bps)
        trail_stop_bps = 0.0        # Current trailing stop level (bps)

        print(f"[MONITOR LOOP] Starting monitoring loop...", flush=True)
        loop_iteration = 0
        while True:
            await asyncio.sleep(self.position_check_interval)
            loop_iteration += 1
            if loop_iteration % 100 == 0:  # Log every 100 iterations (every 10s at 0.1s interval)
                print(f"[MONITOR LOOP] Still running... iteration {loop_iteration}", flush=True)

            # Get current market data
            bid = self.current_bid
            ask = self.current_ask
            mid = self.current_mid

            if not all([bid, ask, mid]):
                continue

            # Calculate current P&L in bps using ACTUAL exit prices (not mid)
            # Use realistic exit pricing to avoid optimistic P&L
            if entry_side == OrderSide.BUY:
                # Long: exit by selling at BID
                exit_price = bid * 0.9995  # Actual exit price (bid minus buffer)
                pnl_dollars = (exit_price - entry_price) * size
                pnl_bps = ((exit_price - entry_price) / entry_price) * 10000
                # Mid-based P&L for trail tracking (more responsive)
                mid_pnl_bps = ((mid - entry_price) / entry_price) * 10000
            else:
                # Short: exit by buying at ASK
                exit_price = ask * 1.0005  # Actual exit price (ask plus buffer)
                pnl_dollars = (entry_price - exit_price) * size
                pnl_bps = ((entry_price - exit_price) / entry_price) * 10000
                # Mid-based P&L for trail tracking (more responsive)
                mid_pnl_bps = ((entry_price - mid) / entry_price) * 10000

            # Get current trend (sticky state from completed candles)
            current_trend = self.trend_state
            elapsed = time.time() - entry_time

            # ====================================================================================
            # CONDITION 1: STOP LOSS (percent of notional)
            # ====================================================================================
            max_loss_dollars = entry_price * size * self.stop_loss_pct
            if pnl_dollars <= -max_loss_dollars:
                print(
                    f"\n[STOP LOSS] P&L: {pnl_bps:.1f} bps (${pnl_dollars:.4f}) "
                    f"<= -{self.stop_loss_pct:.2%} notional (${max_loss_dollars:.4f})",
                    flush=True
                )
                print(f"[STOP LOSS] Exiting {position_type} as TAKER", flush=True)
                exit_ok = await self.exit_position_fast(entry_side, entry_price, size)
                if exit_ok:
                    self.open_positions = 0
                    self.entry_in_flight = False
                    self.position = None
                    return
                print("[STOP LOSS] Exit not confirmed flat yet; continuing monitor/retry.", flush=True)
                continue

            # ====================================================================================
            # CONDITION 2: TRAILING TAKE-PROFIT
            # Track high-water mark on mid; trigger exit on bid/ask P&L
            # ====================================================================================
            if mid_pnl_bps >= self.trailing_tp_activation_bps:
                if not trail_active:
                    trail_active = True
                    trail_high_water_bps = mid_pnl_bps
                    trail_stop_bps = mid_pnl_bps - self.trailing_tp_trail_bps
                    print(
                        f"\n[TRAIL TP] Activated! Mid P&L: {mid_pnl_bps:.1f} bps | "
                        f"High: {trail_high_water_bps:.1f} bps | "
                        f"Trail stop: {trail_stop_bps:.1f} bps",
                        flush=True
                    )
                elif mid_pnl_bps > trail_high_water_bps:
                    old_stop = trail_stop_bps
                    trail_high_water_bps = mid_pnl_bps
                    trail_stop_bps = mid_pnl_bps - self.trailing_tp_trail_bps
                    if trail_stop_bps - old_stop >= 5.0:  # Log when stop moves 5+ bps
                        print(
                            f"[TRAIL TP] New high: {trail_high_water_bps:.1f} bps | "
                            f"Trail stop: {trail_stop_bps:.1f} bps",
                            flush=True
                        )

            # Check if trailing stop has been hit (use actual exit P&L, not mid)
            if trail_active and pnl_bps <= trail_stop_bps:
                print(
                    f"\n[TRAIL TP] Exit triggered! P&L: {pnl_bps:.1f} bps | "
                    f"Trail stop: {trail_stop_bps:.1f} bps | "
                    f"High was: {trail_high_water_bps:.1f} bps",
                    flush=True
                )
                print(f"[TRAIL TP] Exiting {position_type} as TAKER", flush=True)
                exit_ok = await self.exit_position_fast(entry_side, entry_price, size)
                if exit_ok:
                    self.open_positions = 0
                    self.entry_in_flight = False
                    self.position = None
                    return
                print("[TRAIL TP] Exit not confirmed flat yet; continuing monitor/retry.", flush=True)
                continue

            # ====================================================================================
            # CONDITION 3: OPPOSITE STREAK EXIT (5 in a row)
            # Exit only — do NOT carry the signal into a new entry.
            # Clear desired_position and reset streaks so the bot must build
            # a fresh 5-in-a-row before entering the opposite direction.
            # ====================================================================================
            if entry_side == OrderSide.BUY and self.desired_position == "SHORT":
                print(f"\n[STREAK EXIT] Opposite 5-in-a-row - exiting LONG (no auto-flip)", flush=True)
                exit_ok = await self.exit_position_fast(entry_side, entry_price, size)
                if exit_ok:
                    self.open_positions = 0
                    self.entry_in_flight = False
                    self.position = None
                    self.desired_position = None
                    self.up_streak = 0
                    self.down_streak = 0
                    return
                print("[STREAK EXIT] Exit not confirmed flat yet; continuing monitor/retry.", flush=True)
                continue

            if entry_side == OrderSide.SELL and self.desired_position == "LONG":
                print(f"\n[STREAK EXIT] Opposite 5-in-a-row - exiting SHORT (no auto-flip)", flush=True)
                exit_ok = await self.exit_position_fast(entry_side, entry_price, size)
                if exit_ok:
                    self.open_positions = 0
                    self.entry_in_flight = False
                    self.position = None
                    self.desired_position = None
                    self.up_streak = 0
                    self.down_streak = 0
                    return
                print("[STREAK EXIT] Exit not confirmed flat yet; continuing monitor/retry.", flush=True)
                continue

            # ====================================================================================
            # CONDITION 4: BIAS-FLIP EXIT
            # If bias reverses against position direction, exit immediately
            # ====================================================================================
            if self.exit_on_bias_flip:
                current_bias = self.bias_state
                if entry_side == OrderSide.BUY and current_bias == "DOWN":
                    print(
                        f"\n[BIAS EXIT] Bias flipped to DOWN while LONG | "
                        f"P&L: {pnl_bps:.1f} bps (${pnl_dollars:.4f})",
                        flush=True
                    )
                    print(f"[BIAS EXIT] Exiting {position_type} as TAKER", flush=True)
                    exit_ok = await self.exit_position_fast(entry_side, entry_price, size)
                    if exit_ok:
                        self.open_positions = 0
                        self.entry_in_flight = False
                        self.position = None
                        return
                    print("[BIAS EXIT] Exit not confirmed flat yet; continuing monitor/retry.", flush=True)
                    continue

                if entry_side == OrderSide.SELL and current_bias == "UP":
                    print(
                        f"\n[BIAS EXIT] Bias flipped to UP while SHORT | "
                        f"P&L: {pnl_bps:.1f} bps (${pnl_dollars:.4f})",
                        flush=True
                    )
                    print(f"[BIAS EXIT] Exiting {position_type} as TAKER", flush=True)
                    exit_ok = await self.exit_position_fast(entry_side, entry_price, size)
                    if exit_ok:
                        self.open_positions = 0
                        self.entry_in_flight = False
                        self.position = None
                        return
                    print("[BIAS EXIT] Exit not confirmed flat yet; continuing monitor/retry.", flush=True)
                    continue

            # ====================================================================================
            # CONDITION 5: MAX HOLD TIME
            # If position has been open longer than max_hold_seconds, exit
            # ====================================================================================
            if self.max_hold_seconds > 0 and elapsed >= self.max_hold_seconds:
                hold_min = elapsed / 60
                print(
                    f"\n[MAX HOLD] Position held {hold_min:.1f} min (limit {self.max_hold_seconds/60:.0f} min) | "
                    f"P&L: {pnl_bps:.1f} bps (${pnl_dollars:.4f})",
                    flush=True
                )
                print(f"[MAX HOLD] Exiting {position_type} as TAKER", flush=True)
                exit_ok = await self.exit_position_fast(entry_side, entry_price, size)
                if exit_ok:
                    self.open_positions = 0
                    self.entry_in_flight = False
                    self.position = None
                    return
                print("[MAX HOLD] Exit not confirmed flat yet; continuing monitor/retry.", flush=True)
                continue

            # ====================================================================================
            # Still holding - log periodic updates (every 10 seconds)
            # ====================================================================================
            elapsed_int = int(elapsed)
            if not hasattr(self, '_last_hold_log') or self._last_hold_log != elapsed_int:
                if elapsed_int > 0 and elapsed_int % 10 == 0:
                    self._last_hold_log = elapsed_int
                    trail_info = ""
                    if trail_active:
                        trail_info = f" | Trail: stop={trail_stop_bps:.1f} high={trail_high_water_bps:.1f}"
                    print(
                        f"[HOLDING] {position_type} | {elapsed:.0f}s | "
                        f"P&L: {pnl_bps:.1f} bps (${pnl_dollars:.4f}) | "
                        f"Trend: {current_trend} | Bias: {self.bias_state}{trail_info}",
                        flush=True
                    )

    async def exit_position_fast(
        self,
        entry_side: OrderSide,
        entry_price: float,
        size: float,
    ) -> bool:
        """
        Exit a position IMMEDIATELY as a taker.
        
        Philosophy: Accept the small loss. Get out NOW.
        No chasing, no waiting - exit immediately.
        """
        success = False
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
            
            # Place aggressive taker order
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
                post_only=False,  # TAKER
                dry_run=self.dry_run
            )
            
            if exit_order:
                # Confirm position is actually flat before clearing internal state.
                # CRITICAL: Only treat confirmed 0.0 as flat. API errors (-1.0) are NOT flat.
                flat_confirmed = False
                confirm_deadline = time.time() + 3.0
                while time.time() < confirm_deadline:
                    live_size = self.get_live_position_size()
                    if live_size == 0.0:  # Confirmed flat (not -1.0 API error)
                        flat_confirmed = True
                        break
                    await asyncio.sleep(0.15)

                if not flat_confirmed:
                    live_size = self.get_live_position_size()
                    size_str = f"{live_size:.6f}" if live_size >= 0 else "API_ERROR"
                    print(
                        f"[FAST EXIT] Exit order acked but not confirmed flat (size={size_str}); "
                        f"will keep monitor active",
                        flush=True
                    )
                    return False

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
                self.last_exit_time = time.time()
                success = True
                print(f"[FAST EXIT] Done! P&L: ${pnl:.4f}", flush=True)
            else:
                print(f"[FAST EXIT] Exit order failed!", flush=True)
                
        except Exception as e:
            print(f"[FAST EXIT] Error: {e}", flush=True)
        finally:
            pass

        return success

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

    def candle_price(self, candle: "Candle") -> float:
        """Price representation for a completed candle based on configured mode."""
        return self.candle_price_for(candle, self.wma_price_type)

    def candle_price_for(self, candle: "Candle", price_type: str) -> float:
        """Price representation for a completed candle by price_type."""
        if price_type == "weighted_close":
            return candle.weighted_close()
        if price_type == "mid_price":
            return candle.mid_price()
        return candle.close

    def get_price_slope_bps(
        self,
        price_type: str,
        shift_candles: int,
        candle_builder: Optional[CandleBuilder] = None
    ) -> float:
        builder = candle_builder or self.candle_builder
        candles = builder.candles
        if len(candles) <= shift_candles:
            return 0.0
        current = candles[-1]
        past = candles[-1 - shift_candles]
        current_price = self.candle_price_for(current, price_type)
        past_price = self.candle_price_for(past, price_type)
        if not past_price:
            return 0.0
        return ((current_price - past_price) / past_price) * 10000

    def get_wma_slope_bps(self) -> float:
        return self.get_price_slope_bps(self.wma_price_type, self.wma_slope_shift_candles)

    def trend_from_price(self, wma: Optional[float], price: Optional[float], slope_bps: float) -> str:
        """Classify raw trend (no hysteresis) using distance + slope."""
        if wma is None or price is None:
            return "UNKNOWN"
        distance_bps = ((price - wma) / wma) * 10000
        if distance_bps >= self.trend_enter_bps and slope_bps >= self.min_wma_slope_bps:
            return "UP"
        if distance_bps <= -self.trend_enter_bps and slope_bps <= -self.min_wma_slope_bps:
            return "DOWN"
        return "FLAT"

    def bias_trend_from_price(self, wma: Optional[float], price: Optional[float], slope_bps: float) -> str:
        """Classify raw bias (no hysteresis) using distance + slope."""
        if wma is None or price is None:
            return "UNKNOWN"
        distance_bps = ((price - wma) / wma) * 10000
        if distance_bps >= self.bias_enter_bps and slope_bps >= self.bias_min_slope_bps:
            return "UP"
        if distance_bps <= -self.bias_enter_bps and slope_bps <= -self.bias_min_slope_bps:
            return "DOWN"
        return "FLAT"

    def update_trend_state(self, wma: Optional[float], price: Optional[float]) -> str:
        """Trend state machine based on distance to WMA + slope filter."""
        if not wma or not price:
            return self.trend_state

        distance_bps = ((price - wma) / wma) * 10000
        slope_bps = self.get_wma_slope_bps()

        if distance_bps >= self.trend_enter_bps and slope_bps >= self.min_wma_slope_bps:
            self.trend_state = "UP"
        elif distance_bps <= -self.trend_enter_bps and slope_bps <= -self.min_wma_slope_bps:
            self.trend_state = "DOWN"
        else:
            if abs(distance_bps) <= self.trend_exit_bps:
                self.trend_state = "FLAT"

        return self.trend_state

    def update_bias_state(self, wma: Optional[float], price: Optional[float], slope_bps: float) -> str:
        """Sticky bias state machine.

        Key design:
        - FLAT readings PAUSE counters but don't reset them.
          Only the OPPOSITE direction resets a counter.
        - Once in UP/DOWN, only a confirmed opposite signal
          (bias_confirm_candles consecutive readings) can flip it.
        - From UNKNOWN/FLAT, need confirmed signal to enter a direction.
        """
        if not wma or not price:
            return self.bias_state

        raw = self.bias_trend_from_price(wma, price, slope_bps)

        # --- Update counters ---
        # CRITICAL: Only the OPPOSITE direction resets a counter.
        # FLAT readings leave both counters unchanged (sticky).
        if raw == "UP":
            self.bias_confirm_up += 1
            self.bias_confirm_down = 0      # opposite resets
        elif raw == "DOWN":
            self.bias_confirm_down += 1
            self.bias_confirm_up = 0        # opposite resets
        # raw == "FLAT": both counters unchanged

        # --- State transitions ---
        if self.bias_state in ("UNKNOWN", "FLAT"):
            # Need confirmed directional signal to leave neutral
            if self.bias_confirm_up >= self.bias_confirm_candles:
                self.bias_state = "UP"
            elif self.bias_confirm_down >= self.bias_confirm_candles:
                self.bias_state = "DOWN"

        elif self.bias_state == "UP":
            # Only flip on confirmed opposite signal
            if self.bias_confirm_down >= self.bias_confirm_candles:
                self.bias_state = "DOWN"

        elif self.bias_state == "DOWN":
            # Only flip on confirmed opposite signal
            if self.bias_confirm_up >= self.bias_confirm_candles:
                self.bias_state = "UP"

        self.bias_last_raw = raw
        return self.bias_state

    async def monitor_orderbook(self):
        """Monitor orderbook and execute trend streak strategy"""
        print("="*80)
        print(f"DIRECTIONAL TREND BOT - {self.coin}")
        print("="*80)
        print(f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"\nParameters:")
        print(f"  Position size:    ${self.position_size_usd:.2f}")
        print(f"  Max positions:    {self.max_positions}")
        print(f"  Trade cooldown:   {self.min_trade_interval:.0f}s")
        print(f"  Exit cooldown:    {self.post_exit_cooldown_s:.0f}s")
        print(f"  Entry type:       MARKET (taker)")
        print(f"\nWMA Trend Detection (Streak Strategy):")
        print(f"  WMA Period:       {self.wma_period}")
        print(f"  Price Type:       {self.wma_price_type}")
        print(f"  Trend Threshold:  {self.wma_threshold:.2%}")
        print(f"  Trend Enter/Exit: {self.trend_enter_bps:.1f}/{self.trend_exit_bps:.1f} bps")
        print(f"  Required Streak:  {self.required_streak} candles")
        print(f"\nBias Trend Gate (Higher-Timeframe):")
        print(f"  Bias Candle Size: {self.bias_candle_interval_seconds}s")
        print(f"  Bias WMA Period:  {self.bias_wma_period}")
        print(f"  Price Type:       {self.bias_price_type}")
        print(f"  Bias Enter/Exit:  {self.bias_enter_bps:.1f}/{self.bias_exit_bps:.1f} bps")
        print(f"  Min Slope:        {self.bias_min_slope_bps:.1f} bps")
        print(f"  Confirm Candles:  {self.bias_confirm_candles}")
        print(f"\nPosition Management:")
        print(f"  Exit Trigger:     Opposite {self.required_streak}-streak OR stop loss OR trailing TP{' OR bias-flip' if self.exit_on_bias_flip else ''}")
        print(f"  Stop Loss:        -{self.stop_loss_pct:.2%} notional (immediate)")
        print(f"  Trailing TP:      {self.trailing_tp_trail_bps:.0f} bps trail, activate after +{self.trailing_tp_activation_bps:.0f} bps")
        print(f"  Trail Tracking:   Mid-price (trigger on bid/ask)")
        print(f"  Bias-Flip Exit:   {'ON' if self.exit_on_bias_flip else 'OFF'}")
        print(f"  Max Hold Time:    {self.max_hold_seconds/60:.0f} min")
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
                                # STREAK LOGIC: Use completed candles only
                                # ========================================
                                completed_candle = self.candle_builder.update(bid, ask)
                                completed_bias_candle = self.bias_candle_builder.update(bid, ask)

                                # ----------------------------------------
                                # BIAS: Process independently of fast candle
                                # (Fixes race where both had to complete on
                                #  the same WS tick for bias to update)
                                # ----------------------------------------
                                if completed_bias_candle:
                                    bias_eval_price = self.candle_price_for(completed_bias_candle, self.bias_price_type)
                                    bias_wma = self.bias_candle_builder.calculate_wma(
                                        self.bias_wma_period,
                                        self.bias_price_type
                                    )
                                    bias_slope_bps = self.get_price_slope_bps(
                                        self.bias_price_type,
                                        self.bias_slope_shift_candles,
                                        candle_builder=self.bias_candle_builder
                                    )
                                    self.update_bias_state(bias_wma, bias_eval_price, bias_slope_bps)
                                    bias_distance_bps = None
                                    if bias_wma and bias_eval_price:
                                        bias_distance_bps = ((bias_eval_price - bias_wma) / bias_wma) * 10000
                                    self.last_bias_wma = bias_wma
                                    self.last_bias_eval_price = bias_eval_price
                                    self.last_bias_slope_bps = bias_slope_bps
                                    self.last_bias_distance_bps = bias_distance_bps
                                    n_bias = len(self.bias_candle_builder.candles)
                                    wma_str_b = f"${bias_wma:.3f}" if bias_wma else f"n/a (need {self.bias_wma_period})"
                                    print(
                                        f"[BIAS CANDLE #{n_bias}] WMA: {wma_str_b} | "
                                        f"State: {self.bias_state} | Raw: {self.bias_last_raw}",
                                        flush=True
                                    )

                                if completed_candle:
                                    eval_price = self.candle_price(completed_candle)
                                    wma = self.candle_builder.calculate_wma(self.wma_period, self.wma_price_type)
                                    slope_bps = self.get_wma_slope_bps()
                                    raw_trend = self.trend_from_price(wma, eval_price, slope_bps)
                                    trend = self.update_trend_state(wma, eval_price)
                                    distance_bps = None
                                    if wma and eval_price:
                                        distance_bps = ((eval_price - wma) / wma) * 10000
                                    # Use stored bias state (updated independently above)
                                    bias = self.bias_state
                                    bias_distance_bps = self.last_bias_distance_bps

                                    self.last_eval_price = eval_price
                                    self.last_wma = wma
                                    self.last_raw_trend = raw_trend
                                    if trend != self.last_trend:
                                        print(f"\n{'='*60}", flush=True)
                                        print(f"[WMA TREND CHANGE] {self.last_trend} -> {trend} (raw {raw_trend})", flush=True)
                                        if wma and eval_price:
                                            print(f"Price: ${eval_price:.3f} | WMA: ${wma:.3f} | Slope: {slope_bps:+.1f} bps", flush=True)
                                        print(f"Candles: {len(self.candle_builder.candles)}", flush=True)
                                        print(f"{'='*60}\n", flush=True)
                                        if self.last_trend == "UP" and trend != "UP":
                                            self.up_streak = 0
                                        if self.last_trend == "DOWN" and trend != "DOWN":
                                            self.down_streak = 0
                                        self.last_trend = trend

                                    if bias != self.last_bias:
                                        print(f"\n{'='*60}", flush=True)
                                        print(f"[BIAS CHANGE] {self.last_bias} -> {bias} (raw {self.bias_last_raw})", flush=True)
                                        if self.last_bias_wma and self.last_bias_eval_price:
                                            slope_str = (
                                                f"{self.last_bias_slope_bps:+.1f}"
                                                if self.last_bias_slope_bps is not None
                                                else "n/a"
                                            )
                                            print(
                                                f"Price: ${self.last_bias_eval_price:.3f} | "
                                                f"Bias WMA: ${self.last_bias_wma:.3f} | "
                                                f"Slope: {slope_str} bps",
                                                flush=True
                                            )
                                        print(f"Candles: {len(self.bias_candle_builder.candles)}", flush=True)
                                        print(f"{'='*60}\n", flush=True)
                                        self.last_bias = bias

                                    if trend == "UP":
                                        self.up_streak += 1
                                        self.down_streak = 0
                                    elif trend == "DOWN":
                                        self.down_streak += 1
                                        self.up_streak = 0

                                    desired_position = None
                                    if self.up_streak >= self.required_streak:
                                        desired_position = "LONG"
                                    elif self.down_streak >= self.required_streak:
                                        desired_position = "SHORT"

                                    if desired_position is not None:
                                        # Bias gate: REQUIRE bias match (one direction at a time)
                                        # Only LONG when bias=UP, only SHORT when bias=DOWN
                                        if desired_position == "LONG" and bias != "UP":
                                            print(
                                                f"[BIAS BLOCK] wanted LONG, bias={bias} (need UP) | "
                                                f"up={self.up_streak} down={self.down_streak}",
                                                flush=True
                                            )
                                            desired_position = None
                                        elif desired_position == "SHORT" and bias != "DOWN":
                                            print(
                                                f"[BIAS BLOCK] wanted SHORT, bias={bias} (need DOWN) | "
                                                f"up={self.up_streak} down={self.down_streak}",
                                                flush=True
                                            )
                                            desired_position = None

                                    if desired_position is not None and desired_position != self.position:
                                        self.desired_position = desired_position
                                        self.position = desired_position
                                        print(
                                            f"[STREAK] {desired_position} triggered | up={self.up_streak} down={self.down_streak}",
                                            flush=True,
                                        )
                                    # Status line on every completed candle (matches original monitor)
                                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                                    wma_str = f"{wma:.3f}" if wma else "n/a"
                                    dist_str = f"{distance_bps:+.1f}" if distance_bps is not None else "n/a"
                                    bias_str = f"{self.last_bias_wma:.3f}" if self.last_bias_wma else "n/a"
                                    bias_dist_str = f"{bias_distance_bps:+.1f}" if bias_distance_bps is not None else "n/a"
                                    bias_slope_str = (
                                        f"{self.last_bias_slope_bps:+.1f}"
                                        if self.last_bias_slope_bps is not None
                                        else "n/a"
                                    )
                                    n_bias_candles = len(self.bias_candle_builder.candles)
                                    line = (
                                        f"[{ts}] Price: ${eval_price:.3f} | WMA: ${wma_str} | "
                                        f"Dist: {dist_str} bps | Slope: {slope_bps:+.1f} bps | "
                                        f"Trend: {trend} | "
                                        f"Bias: {bias} (WMA:{bias_str} Dist:{bias_dist_str} "
                                        f"Slope:{bias_slope_str} N:{n_bias_candles}/{self.bias_wma_period}) | "
                                        f"streaks U:{self.up_streak} D:{self.down_streak} | "
                                        f"pos: {self.position or 'FLAT'}"
                                    )
                                    print(line, flush=True)

                                # Check for opportunity only on completed candle streak
                                if completed_candle and self.desired_position in ("LONG", "SHORT"):
                                    opportunity = self.should_enter(bid, ask)
                                    if opportunity:
                                        self.enqueue_opportunity(opportunity)

                    except Exception as e:
                        print(f"[ERROR] {e}")
            finally:
                ping_task.cancel()
                if self._trade_task:
                    self._trade_task.cancel()
                if self._watchdog_task:
                    self._watchdog_task.cancel()
                with suppress(asyncio.CancelledError):
                    await ping_task
                with suppress(asyncio.CancelledError):
                    if self._trade_task:
                        await self._trade_task
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
            pass

    def print_stats(self):
        """Print performance statistics"""
        print("\nPERFORMANCE STATS:")
        print(f"  Opportunities seen:  {self.opportunities_seen}")
        print(f"  Trades attempted:    {self.trades_attempted}")
        print(f"\nStreak Entries:")
        print(f"  LONG entries:        {self.long_entries}")
        print(f"  SHORT entries:       {self.short_entries}")
        print(f"  Fills:               {self.one_filled}")
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

        # Create market maker
        print("Creating market maker...", flush=True)
        mm = MarketMaker(
            client=client,
            coin="xyz:SILVER",
            position_size_usd=11.0,         # $11 per trade
            max_positions=1,                # One at a time
            max_trades=999999,              # No practical limit
            max_loss=5.0,                   # Stop if lose $5
            min_trade_interval=0.0,         # No cooldown (position flips)
            post_exit_cooldown_s=120.0,     # Block all re-entries for 2 min after any close
            dry_run=False,                  # LIVE MODE
            max_quote_age_ms=1200.0,        # Speed-prioritized freshness gate
            ws_stale_timeout_s=15.0,        # Reduce REST refresh frequency
            wma_period=60,                  # 60-period WMA (5 minutes)
            wma_price_type="weighted_close",  # (H+L+C+C)/4
            wma_threshold=0.0005,           # 0.05% buffer
            candle_interval_seconds=5,      # 5-second candles
            max_candles=400,                # Keep last 400 candles
            trend_enter_bps=4.0,            # Enter trend at +/-4 bps from WMA
            trend_exit_bps=8.0,             # Exit trend at +/-8 bps from WMA
            bias_candle_interval_seconds=60,  # 1-min bias candles
            bias_max_candles=2000,
            bias_wma_period=45,             # ~45 min higher-timeframe bias WMA (was 120, before that 30)
            bias_price_type="weighted_close",
            bias_enter_bps=4.0,             # Distance from bias WMA to enter
            bias_exit_bps=12.0,             # Wide exit so bias stays locked in
            bias_slope_shift_candles=3,     # ~3 min slope window (1-min candles, was 6)
            bias_min_slope_bps=0.4,         # Slightly forgiving (longer window smooths)
            bias_confirm_candles=2,         # ~2 min confirmation before bias flips (was 4)
            trailing_tp_activation_bps=20.0,  # Trail activates after +20 bps profit (mid)
            trailing_tp_trail_bps=25.0,       # 25 bps trail width from high-water mark
            exit_on_bias_flip=True,           # Exit if bias reverses against position
            max_hold_seconds=3600.0,          # Force exit after 60 min
        )

        # ======================================================================
        # STARTUP SAFETY: Close any orphaned position before starting
        # ======================================================================
        coin = "xyz:SILVER"
        print(f"Checking for orphaned positions on {coin}...", flush=True)
        try:
            pos = client.get_position(coin)
            if pos and abs(pos.size) > 0:
                orphan_size = abs(pos.size)
                orphan_side = "LONG" if pos.size > 0 else "SHORT"
                print(f"[ORPHAN] Found open {orphan_side} position: size={orphan_size:.6f}", flush=True)
                print(f"[ORPHAN] Closing immediately...", flush=True)

                # Determine exit side and price
                book = client.get_order_book(coin)
                if book and book.best_bid and book.best_ask:
                    if pos.size > 0:
                        # Long: sell at bid
                        exit_side = OrderSide.SELL
                        exit_price = client.format_price(coin, book.best_bid * 0.9995)
                    else:
                        # Short: buy at ask
                        exit_side = OrderSide.BUY
                        exit_price = client.format_price(coin, book.best_ask * 1.0005)

                    exit_order = client.place_limit_order(
                        coin=coin,
                        side=exit_side,
                        price=exit_price,
                        size=orphan_size,
                        reduce_only=True,
                        post_only=False,
                        dry_run=False,
                    )
                    if exit_order:
                        # Wait for flat confirmation
                        import time as _time
                        deadline = _time.time() + 5.0
                        while _time.time() < deadline:
                            check = client.get_position(coin)
                            if not check or abs(check.size) <= 0:
                                print(f"[ORPHAN] Position closed successfully!", flush=True)
                                break
                            await asyncio.sleep(0.25)
                        else:
                            check = client.get_position(coin)
                            if check and abs(check.size) > 0:
                                print(f"[ORPHAN] WARNING: Position may still be open (size={abs(check.size):.6f})", flush=True)
                    else:
                        print(f"[ORPHAN] WARNING: Exit order rejected!", flush=True)
                else:
                    print(f"[ORPHAN] WARNING: Cannot get order book to close position!", flush=True)
            else:
                print("No orphaned positions found. Clean start.", flush=True)
        except Exception as e:
            print(f"[ORPHAN] Error checking/closing orphaned position: {e}", flush=True)
            import traceback
            traceback.print_exc()

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

