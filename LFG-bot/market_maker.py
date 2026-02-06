"""
Directional Trend Bot for HIP-3 Pairs

Strategy (Trend Streak with Market Orders):
1. Build 5-second candles from streaming bid/ask data
2. Calculate 60-period WMA using weighted close (H+L+C+C)/4
3. Determine trend state with hysteresis (UP/DOWN/FLAT)
4. Count consecutive trend streaks
5. Enter after 5 consecutive UP (LONG) or DOWN (SHORT) trends
6. Market entries and exits (taker orders)
7. Exit on opposite 5-in-a-row streak or stop loss

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
    ):
        self.client = client
        self.coin = coin
        self.position_size_usd = position_size_usd
        self.max_positions = max_positions
        self.max_trades = max_trades
        self.max_loss = max_loss
        self.min_trade_interval = min_trade_interval
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

        # Trend state tracking
        self.last_trend = "UNKNOWN"
        self.trend_state = "UNKNOWN"
        self.last_eval_price: Optional[float] = None
        self.last_wma: Optional[float] = None
        self.last_raw_trend: str = "UNKNOWN"

        # Streak tracking
        self.required_streak = 5  # 5-in-a-row to trigger entry
        self.up_streak = 0
        self.down_streak = 0
        self.desired_position: Optional[str] = None  # "LONG", "SHORT", or None

        # Position management parameters (Streak exits)
        self.stop_loss_pct = 0.06  # Exit at -6% of position notional
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
        self.position = None  # "LONG", "SHORT", or None
        
        # Check if already filled during placement
        if hasattr(order, 'status') and order.status and 'filled' in str(order.status).lower():
            print(f"[INSTANT FILL] Order filled immediately!", flush=True)
            self.one_filled += 1
            await self.monitor_position(order.side, order.price, order.size)
            return

        # Confirm position before monitoring exits
        try:
            position = self.client.get_position(self.coin)
            if position and abs(position.size) > 0:
                await self.monitor_position(order.side, order.price, abs(position.size))
        except Exception as e:
            print(f"[POSITION] Error checking position after entry: {e}", flush=True)

    async def monitor_position(self, entry_side: OrderSide, entry_price: float, size: float):
        """
        Monitor an open position for exit conditions.

        Streak exit logic - ALL TAKER EXITS.
        Priority order:
        1. Stop Loss (-6% notional) -> Taker exit immediately
        2. Opposite 5-in-a-row streak -> Taker exit immediately

        Args:
            entry_side: The side we entered (BUY for long, SELL for short)
            entry_price: The price we entered at
            size: Position size
        """
        position_type = "LONG" if entry_side == OrderSide.BUY else "SHORT"
        print(f"\n{'='*60}", flush=True)
        print(f"[POSITION] {position_type} @ ${entry_price:.2f} | Size: {size:.4f}", flush=True)
        print(
            f"[POSITION] Exit: Opposite 5-streak or "
            f"SL -{self.stop_loss_pct:.2%} notional",
            flush=True
        )
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
            # Use realistic exit pricing to avoid optimistic P&L
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
                await self.exit_position_fast(entry_side, entry_price, size)
                self.open_positions = 0
                self.entry_in_flight = False
                self.position = None
                return

            # ====================================================================================
            # CONDITION 2: OPPOSITE STREAK EXIT (5 in a row)
            # ====================================================================================
            if entry_side == OrderSide.BUY and self.desired_position == "SHORT":
                print(f"\n[STREAK EXIT] Opposite 5-in-a-row - exiting LONG", flush=True)
                await self.exit_position_fast(entry_side, entry_price, size)
                self.open_positions = 0
                self.entry_in_flight = False
                self.position = None
                return

            if entry_side == OrderSide.SELL and self.desired_position == "LONG":
                print(f"\n[STREAK EXIT] Opposite 5-in-a-row - exiting SHORT", flush=True)
                await self.exit_position_fast(entry_side, entry_price, size)
                self.open_positions = 0
                self.entry_in_flight = False
                self.position = None
                return

            # ====================================================================================
            # Still holding - log periodic updates (every 10 seconds)
            # ====================================================================================
            elapsed_int = int(elapsed)
            if not hasattr(self, '_last_hold_log') or self._last_hold_log != elapsed_int:
                if elapsed_int > 0 and elapsed_int % 10 == 0:
                    self._last_hold_log = elapsed_int
                    print(f"[HOLDING] {position_type} | {elapsed:.0f}s | P&L: {pnl_bps:.1f} bps (${pnl_dollars:.4f}) | Trend: {current_trend}", flush=True)

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
        if self.wma_price_type == "weighted_close":
            return candle.weighted_close()
        if self.wma_price_type == "mid_price":
            return candle.mid_price()
        return candle.close

    
    def get_wma_slope_bps(self) -> float:
        candles = self.candle_builder.candles
        if len(candles) <= self.wma_slope_shift_candles:
            return 0.0
        current = candles[-1]
        past = candles[-1 - self.wma_slope_shift_candles]
        current_price = self.candle_price(current)
        past_price = self.candle_price(past)
        if not past_price:
            return 0.0
        return ((current_price - past_price) / past_price) * 10000

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
        print(f"  Entry type:       MARKET (taker)")
        print(f"\nWMA Trend Detection (Streak Strategy):")
        print(f"  WMA Period:       {self.wma_period}")
        print(f"  Price Type:       {self.wma_price_type}")
        print(f"  Trend Threshold:  {self.wma_threshold:.2%}")
        print(f"  Trend Enter/Exit: {self.trend_enter_bps:.1f}/{self.trend_exit_bps:.1f} bps")
        print(f"  Required Streak:  {self.required_streak} candles")
        print(f"\nPosition Management:")
        print(f"  Exit Trigger:     Opposite {self.required_streak}-streak OR stop loss")
        print(f"  Stop Loss:        -{self.stop_loss_pct:.2%} notional (immediate)")
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

                                if completed_candle:
                                    eval_price = self.candle_price(completed_candle)
                                    wma = self.candle_builder.calculate_wma(self.wma_period, self.wma_price_type)
                                    slope_bps = self.get_wma_slope_bps()
                                    raw_trend = self.trend_from_price(wma, eval_price, slope_bps)
                                    trend = self.update_trend_state(wma, eval_price)
                                    distance_bps = None
                                    if wma and eval_price:
                                        distance_bps = ((eval_price - wma) / wma) * 10000

                                    self.last_eval_price = eval_price
                                    self.last_wma = wma
                                    self.last_raw_trend = raw_trend
                                    if trend != self.last_trend:
                                        print(f"
{'='*60}", flush=True)
                                        print(f"[WMA TREND CHANGE] {self.last_trend} -> {trend} (raw {raw_trend})", flush=True)
                                        if wma and eval_price:
                                            print(f"Price: ${eval_price:.3f} | WMA: ${wma:.3f} | Slope: {slope_bps:+.1f} bps", flush=True)
                                        print(f"Candles: {len(self.candle_builder.candles)}", flush=True)
                                        print(f"{'='*60}
", flush=True)
                                        if self.last_trend == "UP" and trend != "UP":
                                            self.up_streak = 0
                                        if self.last_trend == "DOWN" and trend != "DOWN":
                                            self.down_streak = 0
                                        self.last_trend = trend

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

                                    if desired_position is not None and desired_position != self.position:
                                        self.desired_position = desired_position
                                        self.position = desired_position
                                        print(
                                            f"[STREAK] {desired_position} triggered | up={self.up_streak} down={self.down_streak}",
                                            flush=True,
                                        )

                                    # Log trend changes
                                    if trend != self.last_trend:
                                        print(f"\n{'='*60}", flush=True)
                                        print(f"[WMA TREND CHANGE] {self.last_trend} -> {trend} (raw {raw_trend})", flush=True)
                                        if wma and eval_price:
                                            print(f"Price: ${eval_price:.3f} | WMA: ${wma:.3f}", flush=True)
                                        print(f"Candles: {len(self.candle_builder.candles)}", flush=True)
                                        print(f"{'='*60}\n", flush=True)
                                        if self.last_trend == "UP" and trend != "UP":
                                            self.up_streak = 0
                                        if self.last_trend == "DOWN" and trend != "DOWN":
                                        self.down_streak = 0
                                        self.last_trend = trend

                                    # Status line on every completed candle (matches original monitor)
                                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                                    wma_str = f"{wma:.3f}" if wma else "n/a"
                                    dist_str = f"{distance_bps:+.1f}" if distance_bps is not None else "n/a"
                                    line = (
                                        f"[{ts}] Price: ${eval_price:.3f} | WMA: ${wma_str} | "
                                        f"Dist: {dist_str} bps | Slope: {slope_bps:+.1f} bps | "
                                        f"Trend: {trend} | streaks U:{self.up_streak} D:{self.down_streak} | "
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

