"""
WMA Trend Testing Script V2

Tests WMA trend detection on live orderbook data.
Logs where the bot WOULD enter without placing actual orders.

Improvements over V1:
- Removed redundant header printing
- Consolidated KeyboardInterrupt handling
- Dynamic WMA period in stats (not hardcoded to 15)
- Consistent return dict structure in should_enter()
- Added tight spread rejection tracking
- Consistent price formatting
- Imports at top of file
- Basic reconnection logic

Usage:
    python wma_tester_v2.py
"""
import asyncio
import websockets
import json
import traceback
from datetime import datetime, timezone
from candle_builder import CandleBuilder


class WMATesterV2:
    """Test WMA trend signals on live data"""

    def __init__(
        self,
        coin: str = "xyz:SILVER",
        wma_period: int = 15,
        spread_threshold_bps: float = 6.0,
        spread_position: float = 0.2,
        price_type: str = 'weighted_close',
        threshold: float = 0.0005,
        max_reconnect_attempts: int = 5
    ):
        self.coin = coin
        self.wma_period = wma_period
        self.spread_threshold_bps = spread_threshold_bps
        self.spread_position = spread_position
        self.price_type = price_type
        self.threshold = threshold
        self.max_reconnect_attempts = max_reconnect_attempts

        # Orderbook state
        self.ws_url = "wss://api.hyperliquid.xyz/ws"
        self.current_bid = None
        self.current_ask = None

        # Candle builder
        self.candle_builder = CandleBuilder(candle_interval_seconds=5, max_candles=100)

        # Stats
        self.long_signals = 0
        self.short_signals = 0
        self.flat_signals = 0
        self.unknown_signals = 0
        self.tight_spread_signals = 0
        self.update_count = 0
        self.last_trend = "UNKNOWN"

    def calculate_spread_bps(self, bid: float, ask: float) -> float:
        """Calculate spread in basis points"""
        mid = (bid + ask) / 2
        spread = ask - bid
        return (spread / mid) * 10000

    def calculate_order_price(self, bid: float, ask: float, side: str) -> float:
        """
        Calculate order price for momentum strategy.

        Args:
            bid: Best bid
            ask: Best ask
            side: 'LONG' or 'SHORT'

        Returns:
            Order price
        """
        spread = ask - bid

        if side == 'LONG':
            # BUY closer to ask (momentum)
            return ask - (spread * self.spread_position)
        elif side == 'SHORT':
            # SELL closer to bid (momentum)
            return bid + (spread * self.spread_position)
        else:
            raise ValueError(f"Invalid side: {side}")

    def should_enter(self, trend: str, bid: float, ask: float) -> dict:
        """
        Determine if we should enter based on trend and spread.

        Returns:
            dict with 'signal', 'side', 'price', 'spread_bps', 'reason'
            All keys are always present (side/price may be None if no signal)
        """
        spread_bps = self.calculate_spread_bps(bid, ask)

        # Check spread threshold
        if spread_bps < self.spread_threshold_bps:
            return {
                'signal': False,
                'side': None,
                'price': None,
                'spread_bps': spread_bps,
                'reason': f'Spread too tight: {spread_bps:.2f} bps < {self.spread_threshold_bps:.1f} bps',
                'rejection_type': 'tight_spread'
            }

        # Check trend
        if trend == "UP":
            price = self.calculate_order_price(bid, ask, 'LONG')
            return {
                'signal': True,
                'side': 'LONG',
                'price': price,
                'spread_bps': spread_bps,
                'reason': 'Price above WMA - uptrend',
                'rejection_type': None
            }
        elif trend == "DOWN":
            price = self.calculate_order_price(bid, ask, 'SHORT')
            return {
                'signal': True,
                'side': 'SHORT',
                'price': price,
                'spread_bps': spread_bps,
                'reason': 'Price below WMA - downtrend',
                'rejection_type': None
            }
        elif trend == "FLAT":
            return {
                'signal': False,
                'side': None,
                'price': None,
                'spread_bps': spread_bps,
                'reason': 'Price on WMA line - no clear trend',
                'rejection_type': 'flat'
            }
        else:  # UNKNOWN
            return {
                'signal': False,
                'side': None,
                'price': None,
                'spread_bps': spread_bps,
                'reason': 'Insufficient data for WMA calculation',
                'rejection_type': 'unknown'
            }

    def get_wma(self) -> float | None:
        """Get current WMA using configured period"""
        return self.candle_builder.calculate_wma(self.wma_period, self.price_type)

    def get_current_price(self) -> float | None:
        """Get current price from candle builder"""
        if self.candle_builder.current_candle is None:
            return None

        if self.price_type == 'weighted_close':
            return self.candle_builder.current_candle.weighted_close()
        elif self.price_type == 'mid_price':
            return self.candle_builder.current_candle.mid_price()
        else:
            return self.candle_builder.current_candle.close

    async def monitor_orderbook(self):
        """Monitor orderbook and log entry signals"""
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
                            self.update_count += 1

                            # Update candle builder
                            completed_candle = self.candle_builder.update(bid, ask)

                            # Every 5 seconds when a candle completes, check for signals
                            if completed_candle:
                                trend = self.candle_builder.get_trend(
                                    period=self.wma_period,
                                    price_type=self.price_type,
                                    threshold=self.threshold
                                )

                                wma = self.get_wma()
                                current_price = self.get_current_price()

                                # Check if we should enter
                                entry_decision = self.should_enter(trend, bid, ask)

                                # Log trend changes
                                if trend != self.last_trend:
                                    print(f"\n{'='*60}")
                                    print(f"[TREND CHANGE] {self.last_trend} → {trend}")
                                    if wma and current_price:
                                        print(f"Price: ${current_price:.3f} | WMA: ${wma:.3f}")
                                    print(f"{'='*60}\n")
                                    self.last_trend = trend

                                # Log entry signals
                                if entry_decision['signal']:
                                    side = entry_decision['side']
                                    price = entry_decision['price']
                                    spread_bps = entry_decision['spread_bps']
                                    reason = entry_decision['reason']

                                    timestamp = datetime.now(timezone.utc).strftime('%H:%M:%S')

                                    print(f"[{timestamp}] {'🟢' if side == 'LONG' else '🔴'} {side} SIGNAL")
                                    print(f"  Entry Price: ${price:.3f}")
                                    print(f"  Current:     ${current_price:.3f} | WMA: ${wma:.3f}")
                                    print(f"  Spread:      ${ask - bid:.3f} ({spread_bps:.2f} bps)")
                                    print(f"  Reason:      {reason}")
                                    print()

                                    if side == 'LONG':
                                        self.long_signals += 1
                                    else:
                                        self.short_signals += 1

                                # Track rejection types
                                else:
                                    rejection_type = entry_decision.get('rejection_type')
                                    if rejection_type == 'flat':
                                        self.flat_signals += 1
                                    elif rejection_type == 'unknown':
                                        self.unknown_signals += 1
                                    elif rejection_type == 'tight_spread':
                                        self.tight_spread_signals += 1

                            # Print periodic update every 100 book updates
                            if self.update_count % 100 == 0:
                                timestamp = datetime.now(timezone.utc).strftime('%H:%M:%S')
                                spread_bps = self.calculate_spread_bps(bid, ask)
                                wma = self.get_wma()

                                if wma:
                                    wma_str = f"WMA: ${wma:.3f}"
                                else:
                                    wma_str = "WMA: Building..."

                                print(f"[{timestamp}] Bid: ${bid:.3f} | Ask: ${ask:.3f} | "
                                      f"Spread: {spread_bps:.2f} bps | {wma_str} | Trend: {self.last_trend}")

                except Exception as e:
                    print(f"[ERROR] {e}")

    async def run(self):
        """Run the tester with reconnection logic"""
        self.print_header()

        reconnect_attempts = 0

        while reconnect_attempts < self.max_reconnect_attempts:
            try:
                await self.monitor_orderbook()
            except websockets.exceptions.ConnectionClosed as e:
                reconnect_attempts += 1
                print(f"\n[DISCONNECTED] Connection closed: {e}")
                if reconnect_attempts < self.max_reconnect_attempts:
                    wait_time = min(30, 2 ** reconnect_attempts)
                    print(f"[RECONNECT] Attempt {reconnect_attempts}/{self.max_reconnect_attempts} in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    print(f"[FAILED] Max reconnect attempts reached")
                    break
            except Exception as e:
                print(f"\n[ERROR] {e}")
                traceback.print_exc()
                break

    def print_header(self):
        """Print startup header"""
        print("=" * 80)
        print(f"WMA TREND TESTER V2 - {self.coin}")
        print("=" * 80)
        print(f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"\nConfiguration:")
        print(f"  WMA Period:       {self.wma_period}")
        print(f"  Price Type:       {self.price_type}")
        print(f"  Spread Threshold: {self.spread_threshold_bps:.1f} bps")
        print(f"  Spread Position:  {self.spread_position:.1%}")
        print(f"  Trend Threshold:  {self.threshold:.2%}")
        print(f"\n  Mode:             TEST ONLY (no real orders)")
        print("=" * 80 + "\n")

    def print_stats(self):
        """Print signal statistics"""
        total_signals = self.long_signals + self.short_signals
        total_skipped = self.flat_signals + self.unknown_signals + self.tight_spread_signals

        print("\nSIGNAL STATISTICS:")
        print(f"  Total Candles:    {len(self.candle_builder.candles)}")
        print(f"  Book Updates:     {self.update_count}")
        print(f"\n  Entry Signals:    {total_signals}")
        print(f"    LONG signals:   {self.long_signals}")
        print(f"    SHORT signals:  {self.short_signals}")
        print(f"\n  Skipped Signals:  {total_skipped}")
        print(f"    FLAT (on line): {self.flat_signals}")
        print(f"    UNKNOWN (data): {self.unknown_signals}")
        print(f"    Tight spread:   {self.tight_spread_signals}")


async def main():
    """Main entry point"""
    tester = WMATesterV2(
        coin="xyz:SILVER",
        wma_period=10,
        spread_threshold_bps=6.0,
        spread_position=0.2,
        price_type='weighted_close',
        threshold=0.0005,
        max_reconnect_attempts=5
    )

    try:
        await tester.run()
    except KeyboardInterrupt:
        print("\n\n" + "=" * 80)
        print("[STOPPED] User interrupted")
        print("=" * 80)
        tester.print_stats()
        print("=" * 80)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # Already handled in main()
