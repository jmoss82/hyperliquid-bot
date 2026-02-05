"""
Unified signal tester for HIP-3 orderbook data.

This script keeps the live bot untouched and provides one clear signal
for local testing:
  - LONG
  - SHORT
  - NO_TRADE

The unified signal combines:
  1) WMA trend direction
  2) trend persistence (streak)
  3) minimum WMA distance
  4) spread threshold
  5) optional swing-structure bias filter

Usage:
    python wma_tester.py
"""
import asyncio
import json
from datetime import datetime, timezone

import websockets

from candle_builder import CandleBuilder


class WMATester:
    """Test a unified entry/exit signal on live data."""

    def __init__(
        self,
        coin: str = "xyz:SILVER",
        wma_period: int = 60,
        spread_threshold_bps: float = 6.0,
        spread_position: float = 0.2,
        price_type: str = "weighted_close",
        threshold: float = 0.0005,
        min_trend_streak: int = 2,
        min_wma_distance_bps: float = 3.0,
        min_wma_slope_bps: float = 0.8,
        trend_enter_bps: float = 4.0,
        trend_exit_bps: float = 8.0,
        wma_slope_shift_candles: int = 3,
        structure_break_buffer_bps: float = 3.0,
        stop_loss_bps: float = 7.0,
        catastrophic_stop_bps: float = 15.0,
        min_hold_candles: int = 1,
        max_hold_candles: int = 24,
        max_reconnect_attempts: int = 0,
    ):
        self.coin = coin
        self.wma_period = wma_period
        self.spread_threshold_bps = spread_threshold_bps
        self.spread_position = spread_position
        self.price_type = price_type
        self.threshold = threshold
        self.min_trend_streak = min_trend_streak
        self.min_wma_distance_bps = min_wma_distance_bps
        self.min_wma_slope_bps = min_wma_slope_bps
        self.trend_enter_bps = trend_enter_bps
        self.trend_exit_bps = trend_exit_bps
        self.wma_slope_shift_candles = wma_slope_shift_candles
        self.structure_break_buffer_bps = structure_break_buffer_bps
        self.stop_loss_bps = stop_loss_bps
        self.catastrophic_stop_bps = catastrophic_stop_bps
        self.min_hold_candles = min_hold_candles
        self.max_hold_candles = max_hold_candles
        self.max_reconnect_attempts = max_reconnect_attempts

        # Orderbook state
        self.ws_url = "wss://api.hyperliquid.xyz/ws"
        self.current_bid = None
        self.current_ask = None

        # Candle builder
        self.candle_builder = CandleBuilder(candle_interval_seconds=5)

        # Signal stats
        self.long_signals = 0
        self.short_signals = 0
        self.no_trade_signals = 0
        self.update_count = 0
        self.last_trend = "UNKNOWN"
        self.trend_state = "UNKNOWN"
        self._trend_streak = 0
        self._last_unified_signal = "NO_TRADE"

        # Paper position tracking for unified exits
        self.paper_position = None
        self.paper_trades = 0
        self.paper_wins = 0
        self.paper_losses = 0
        self.paper_net_bps = 0.0
        self.paper_hold_candles = 0

    def calculate_spread_bps(self, bid: float, ask: float) -> float:
        """Calculate spread in basis points."""
        mid = (bid + ask) / 2
        spread = ask - bid
        return (spread / mid) * 10000

    def calculate_order_price(self, bid: float, ask: float, side: str) -> float:
        """Calculate momentum-oriented entry placement."""
        spread = ask - bid

        if side == "LONG":
            return ask - (spread * self.spread_position)
        if side == "SHORT":
            return bid + (spread * self.spread_position)
        raise ValueError(f"Invalid side: {side}")

    def get_wma(self) -> float | None:
        """Get current WMA for configured period."""
        return self.candle_builder.calculate_wma(self.wma_period, self.price_type)

    def get_current_price(self) -> float | None:
        """Get current price from current in-progress candle."""
        if self.candle_builder.current_candle is None:
            return None

        if self.price_type == "weighted_close":
            return self.candle_builder.current_candle.weighted_close()
        if self.price_type == "mid_price":
            return self.candle_builder.current_candle.mid_price()
        return self.candle_builder.current_candle.close

    def candle_price(self, candle) -> float:
        """Price representation for a completed candle based on configured mode."""
        if self.price_type == "weighted_close":
            return candle.weighted_close()
        if self.price_type == "mid_price":
            return candle.mid_price()
        return candle.close

    def trend_from_price(self, wma: float | None, price: float | None) -> str:
        """Classify trend from a given price vs WMA (not from in-progress candle)."""
        if wma is None or price is None:
            return "UNKNOWN"
        upper = wma * (1 + self.threshold)
        lower = wma * (1 - self.threshold)
        if price > upper:
            return "UP"
        if price < lower:
            return "DOWN"
        return "FLAT"

    def update_trend_state(self, wma: float | None, price: float | None) -> str:
        """
        Hysteresis trend state machine based on bps distance to WMA.

        - Enter UP/DOWN when distance exceeds trend_enter_bps.
        - Stay in trend until distance crosses back past trend_exit_bps
          in the opposite direction.
        """
        if wma is None or price is None or wma == 0:
            self.trend_state = "UNKNOWN"
            return self.trend_state

        delta_bps = ((price - wma) / wma) * 10000

        if self.trend_state == "UP":
            if delta_bps <= -self.trend_exit_bps:
                self.trend_state = "DOWN"
            elif abs(delta_bps) <= self.trend_exit_bps:
                self.trend_state = "UP"
            else:
                self.trend_state = "UP"
            return self.trend_state

        if self.trend_state == "DOWN":
            if delta_bps >= self.trend_exit_bps:
                self.trend_state = "UP"
            elif abs(delta_bps) <= self.trend_exit_bps:
                self.trend_state = "DOWN"
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

    def _calculate_wma_from_values(self, values: list[float]) -> float | None:
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

    def get_true_structure_levels(self) -> tuple[float | None, float | None]:
        """Return (support, resistance) using CandleBuilder output."""
        structure = self.candle_builder.get_structure_stats()
        support = structure["last_support"]
        resistance = structure["last_resistance"]
        return support, resistance

    def update_trend_streak(self, trend: str) -> int:
        """Track consecutive completed candles with same trend state."""
        if trend in ("UP", "DOWN"):
            if trend == self.last_trend:
                self._trend_streak += 1
            else:
                self._trend_streak = 1
        else:
            self._trend_streak = 0
        return self._trend_streak

    def unified_signal(
        self,
        trend: str,
        bid: float,
        ask: float,
        current_price: float | None = None,
        wma: float | None = None,
    ) -> dict:
        """Return one unified signal decision for this candle."""
        spread_bps = self.calculate_spread_bps(bid, ask)
        wma = self.get_wma() if wma is None else wma
        current_price = self.get_current_price() if current_price is None else current_price
        streak = self.update_trend_streak(trend)
        support, resistance = self.get_true_structure_levels()
        wma_slope_bps = self.get_wma_slope_bps()

        if not wma or not current_price:
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

        wma_distance_bps = abs((current_price - wma) / wma) * 10000

        # Optional structure filters (if levels exist).
        structure_block_long = False
        structure_block_short = False
        if support:
            support_break = support * (1 - self.structure_break_buffer_bps / 10000)
            structure_block_long = current_price < support_break
        if resistance:
            resistance_break = resistance * (1 + self.structure_break_buffer_bps / 10000)
            structure_block_short = current_price > resistance_break

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

    def _open_paper_position(self, signal: str, price: float, timestamp: str):
        self.paper_position = {
            "side": signal,
            "entry_price": price,
            "entry_time": timestamp,
        }
        self.paper_hold_candles = 0
        print(f"[{timestamp}] PAPER ENTER {signal} @ ${price:.3f}")

    def _close_paper_position(self, exit_price: float, timestamp: str, reason: str):
        if not self.paper_position:
            return

        side = self.paper_position["side"]
        entry = self.paper_position["entry_price"]
        if side == "LONG":
            pnl_bps = ((exit_price - entry) / entry) * 10000
        else:
            pnl_bps = ((entry - exit_price) / entry) * 10000

        self.paper_trades += 1
        self.paper_net_bps += pnl_bps
        if pnl_bps >= 0:
            self.paper_wins += 1
        else:
            self.paper_losses += 1

        print(
            f"[{timestamp}] PAPER EXIT {side} @ ${exit_price:.3f} | "
            f"PnL: {pnl_bps:+.1f} bps | {reason}"
        )
        self.paper_position = None
        self.paper_hold_candles = 0

    async def monitor_orderbook(self):
        """Monitor orderbook and log unified entry/exit signals."""
        print("=" * 80)
        print(f"UNIFIED SIGNAL TESTER - {self.coin}")
        print("=" * 80)
        print(f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print("\nConfiguration:")
        print(f"  WMA Period:           {self.wma_period}")
        print(f"  Price Type:           {self.price_type}")
        print(f"  Spread Threshold:     {self.spread_threshold_bps:.1f} bps")
        print(f"  Trend Threshold:      {self.threshold:.2%}")
        print(f"  Min Trend Streak:     {self.min_trend_streak} candles")
        print(f"  Min WMA Distance:     {self.min_wma_distance_bps:.1f} bps")
        print(f"  Min WMA Slope:        {self.min_wma_slope_bps:.1f} bps/candle")
        print(f"  WMA Slope Shift:      {self.wma_slope_shift_candles} candles")
        print(f"  Trend Enter/Exit:     {self.trend_enter_bps:.1f} / {self.trend_exit_bps:.1f} bps")
        print(f"  Structure Buffer:     {self.structure_break_buffer_bps:.1f} bps")
        print(f"  Stop Loss (paper):    -{self.stop_loss_bps:.1f} bps (after {self.min_hold_candles} candles)")
        print(f"  Catastrophic (paper): -{self.catastrophic_stop_bps:.1f} bps (always on)")
        print(f"  Max Hold (paper):     {self.max_hold_candles} candles")
        print("\n  Swing Detection:      Asymmetric (180 left / 12 right)")
        print("  Structure Warmup:     193 candles (~16 minutes)")
        print("\n  Mode:                 TEST ONLY (no real orders)")
        print("=" * 80 + "\n")

        async with websockets.connect(
            self.ws_url,
            ping_interval=20,
            ping_timeout=30,
            close_timeout=10,
        ) as ws:
            subscribe_msg = {
                "method": "subscribe",
                "subscription": {"type": "l2Book", "coin": self.coin},
            }
            await ws.send(json.dumps(subscribe_msg))

            print("[OK] Connected to orderbook feed\n")

            async for message in ws:
                try:
                    data = json.loads(message)

                    if data.get("channel") == "l2Book":
                        book_data = data.get("data", {})
                        levels = book_data.get("levels", [[], []])

                        if len(levels) == 2 and levels[0] and levels[1]:
                            bids = levels[0]
                            asks = levels[1]

                            bid = float(bids[0]["px"])
                            ask = float(asks[0]["px"])

                            self.current_bid = bid
                            self.current_ask = ask
                            self.update_count += 1

                            completed_candle = self.candle_builder.update(bid, ask)

                            if completed_candle:
                                # Use the completed candle for decisions to avoid first-tick noise.
                                eval_price = self.candle_price(completed_candle)
                                wma = self.get_wma()
                                raw_trend = self.trend_from_price(wma, eval_price)
                                trend = self.update_trend_state(wma, eval_price)
                                signal = self.unified_signal(
                                    trend,
                                    bid,
                                    ask,
                                    current_price=eval_price,
                                    wma=wma,
                                )

                                if trend != self.last_trend:
                                    print("\n" + "=" * 60)
                                    print(f"[TREND CHANGE] {self.last_trend} -> {trend} (raw {raw_trend})")
                                    if wma and eval_price:
                                        print(f"Price: ${eval_price:.3f} | WMA: ${wma:.3f}")
                                    print("=" * 60 + "\n")
                                    self.last_trend = trend

                                timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
                                if signal["signal"] == "LONG":
                                    self.long_signals += 1
                                    if self.paper_position is None:
                                        entry_price = self.calculate_order_price(bid, ask, "LONG")
                                        self._open_paper_position("LONG", entry_price, timestamp)
                                elif signal["signal"] == "SHORT":
                                    self.short_signals += 1
                                    if self.paper_position is None:
                                        entry_price = self.calculate_order_price(bid, ask, "SHORT")
                                        self._open_paper_position("SHORT", entry_price, timestamp)
                                else:
                                    self.no_trade_signals += 1

                                if self.paper_position is not None:
                                    self.paper_hold_candles += 1
                                    side = self.paper_position["side"]
                                    exit_reason = None
                                    entry_price = self.paper_position["entry_price"]
                                    exit_price = bid if side == "LONG" else ask
                                    if side == "LONG":
                                        pnl_bps = ((exit_price - entry_price) / entry_price) * 10000
                                    else:
                                        pnl_bps = ((entry_price - exit_price) / entry_price) * 10000

                                    if pnl_bps <= -self.catastrophic_stop_bps:
                                        exit_reason = f"catastrophic stop <= -{self.catastrophic_stop_bps:.1f} bps"
                                    elif side == "LONG" and trend == "DOWN":
                                        exit_reason = "trend flip to DOWN"
                                    elif side == "SHORT" and trend == "UP":
                                        exit_reason = "trend flip to UP"
                                    elif side == "LONG" and signal["signal"] == "SHORT":
                                        exit_reason = "opposite unified signal"
                                    elif side == "SHORT" and signal["signal"] == "LONG":
                                        exit_reason = "opposite unified signal"
                                    elif self.paper_hold_candles >= self.min_hold_candles and pnl_bps <= -self.stop_loss_bps:
                                        exit_reason = f"stop loss <= -{self.stop_loss_bps:.1f} bps"
                                    elif self.paper_hold_candles >= self.max_hold_candles:
                                        exit_reason = "max hold reached"

                                    if exit_reason:
                                        self._close_paper_position(exit_price, timestamp, exit_reason)

                                if signal["signal"] != self._last_unified_signal:
                                    print(
                                        f"[{timestamp}] UNIFIED: {signal['signal']} | "
                                        f"spread={signal['spread_bps']:.2f}bps | "
                                        f"streak={signal['trend_streak']} | "
                                        f"dist={signal['wma_distance_bps']:.1f}bps | "
                                        f"slope={signal['wma_slope_bps']:+.1f}bps | "
                                        f"{signal['reason']}"
                                    )
                                    self._last_unified_signal = signal["signal"]

                            if self.update_count % 100 == 0:
                                timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
                                trend = self.candle_builder.get_trend(
                                    self.wma_period,
                                    self.price_type,
                                    self.threshold,
                                )
                                spread_bps = self.calculate_spread_bps(bid, ask)
                                wma = self.get_wma()
                                current_price = self.get_current_price()
                                support, resistance = self.get_true_structure_levels()

                                wma_str = f"WMA: ${wma:.3f}" if wma else "WMA: Building..."
                                cp_str = f"${current_price:.3f}" if current_price else "n/a"
                                sr_str = ""
                                if support:
                                    sr_str += f" | Sup: ${support:.3f}"
                                if resistance:
                                    sr_str += f" | Res: ${resistance:.3f}"

                                print(
                                    f"[{timestamp}] Bid: ${bid:.2f} | Ask: ${ask:.2f} | "
                                    f"Spread: {spread_bps:.2f} bps | Price: {cp_str} | "
                                    f"{wma_str} | Trend: {trend}{sr_str}"
                                )

                except Exception as e:
                    print(f"[ERROR] {e}")

    async def run(self):
        """Run the tester."""
        reconnect_attempts = 0
        try:
            while True:
                try:
                    await self.monitor_orderbook()
                    reconnect_attempts = 0
                except KeyboardInterrupt:
                    raise
                except (websockets.exceptions.ConnectionClosedError, OSError, TimeoutError) as e:
                    reconnect_attempts += 1
                    if self.max_reconnect_attempts and reconnect_attempts > self.max_reconnect_attempts:
                        print(
                            f"\n[ERROR] Reconnect attempts exceeded ({self.max_reconnect_attempts}). Last error: {e}"
                        )
                        break
                    wait_s = min(30, 2 ** min(reconnect_attempts, 5))
                    print(
                        f"\n[DISCONNECTED] {e} | reconnecting in {wait_s}s "
                        f"(attempt {reconnect_attempts})"
                    )
                    await asyncio.sleep(wait_s)
                except Exception as e:
                    print(f"\n[ERROR] {e}")
                    import traceback

                    traceback.print_exc()
                    break
        except KeyboardInterrupt:
            print("\n\n" + "=" * 80)
            print("[STOPPED] User interrupted")
            print("=" * 80)
            self.print_stats()
            print("=" * 80)

    def print_stats(self):
        """Print unified signal and paper trade stats."""
        total_signals = self.long_signals + self.short_signals + self.no_trade_signals
        win_rate = (self.paper_wins / self.paper_trades * 100) if self.paper_trades else 0.0

        print("\nSIGNAL STATISTICS:")
        print(f"  Total Candles:    {len(self.candle_builder.candles)}")
        print(f"  Book Updates:     {self.update_count}")
        print(f"\n  Unified Signals:  {total_signals}")
        print(f"    LONG signals:   {self.long_signals}")
        print(f"    SHORT signals:  {self.short_signals}")
        print(f"    NO_TRADE:       {self.no_trade_signals}")

        print("\n  Paper Trades:")
        print(f"    Closed trades:  {self.paper_trades}")
        print(f"    Wins:           {self.paper_wins}")
        print(f"    Losses:         {self.paper_losses}")
        print(f"    Win rate:       {win_rate:.1f}%")
        print(f"    Net PnL:        {self.paper_net_bps:+.1f} bps")


async def main():
    """Main entry point."""
    print("=" * 80)
    print("UNIFIED SIGNAL TESTER STARTING")
    print("=" * 80)

    tester = WMATester(
        coin="xyz:SILVER",
        wma_period=60,
        spread_threshold_bps=6.0,
        spread_position=0.2,
        price_type="weighted_close",
        threshold=0.0005,
        min_trend_streak=2,
        min_wma_distance_bps=3.0,
        min_wma_slope_bps=0.8,
        trend_enter_bps=4.0,
        trend_exit_bps=8.0,
        wma_slope_shift_candles=3,
        structure_break_buffer_bps=3.0,
        max_hold_candles=24,
    )

    await tester.run()


if __name__ == "__main__":
    print("=" * 80, flush=True)
    print("UNIFIED SIGNAL TESTER", flush=True)
    print("=" * 80, flush=True)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[STOPPED] Keyboard interrupt", flush=True)
    except Exception as e:
        print(f"\n[ERROR] Main loop failed: {e}", flush=True)
        import traceback

        traceback.print_exc()
