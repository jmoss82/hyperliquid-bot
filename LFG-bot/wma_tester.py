"""
WMA Trend Testing Script

Tests 15-period WMA trend detection on live orderbook data.
Logs where the bot WOULD enter without placing actual orders.

Usage:
    python wma_tester.py
"""
import asyncio
import websockets
import json
import time
from datetime import datetime, timezone
from candle_builder import CandleBuilder


class WMATester:
    """Test WMA trend signals on live data"""

    def __init__(
        self,
        coin: str = "xyz:SILVER",
        wma_period: int = 15,
        spread_threshold_bps: float = 6.0,
        spread_position: float = 0.2,
        price_type: str = 'weighted_close',
        threshold: float = 0.0005
    ):
        self.coin = coin
        self.wma_period = wma_period
        self.spread_threshold_bps = spread_threshold_bps
        self.spread_position = spread_position
        self.price_type = price_type
        self.threshold = threshold

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
        """
        # Check spread threshold
        spread_bps = self.calculate_spread_bps(bid, ask)
        if spread_bps < self.spread_threshold_bps:
            return {
                'signal': False,
                'reason': f'Spread too tight: {spread_bps:.2f} bps < {self.spread_threshold_bps:.1f} bps'
            }

        # Check trend
        if trend == "UP":
            # Enter LONG only
            price = self.calculate_order_price(bid, ask, 'LONG')
            return {
                'signal': True,
                'side': 'LONG',
                'price': price,
                'spread_bps': spread_bps,
                'reason': 'Price above WMA - uptrend'
            }
        elif trend == "DOWN":
            # Enter SHORT only
            price = self.calculate_order_price(bid, ask, 'SHORT')
            return {
                'signal': True,
                'side': 'SHORT',
                'price': price,
                'spread_bps': spread_bps,
                'reason': 'Price below WMA - downtrend'
            }
        elif trend == "FLAT":
            return {
                'signal': False,
                'reason': 'Price on WMA line - no clear trend'
            }
        else:  # UNKNOWN
            return {
                'signal': False,
                'reason': 'Insufficient data for WMA calculation'
            }

    async def monitor_orderbook(self):
        """Monitor orderbook and log entry signals"""
        print("="*80)
        print(f"WMA TREND TESTER - {self.coin}")
        print("="*80)
        print(f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"\nConfiguration:")
        print(f"  WMA Period:       {self.wma_period}")
        print(f"  Price Type:       {self.price_type}")
        print(f"  Spread Threshold: {self.spread_threshold_bps:.1f} bps")
        print(f"  Spread Position:  {self.spread_position:.1%}")
        print(f"  Trend Threshold:  {self.threshold:.2%}")
        print(f"\n  Mode:             TEST ONLY (no real orders)")
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
                                # Get current trend
                                trend = self.candle_builder.get_trend(
                                    period=self.wma_period,
                                    price_type=self.price_type,
                                    threshold=self.threshold
                                )

                                # Get stats
                                stats = self.candle_builder.get_stats()
                                wma = stats['wma_15']
                                current_price = stats['current_price']

                                # Check if we should enter
                                entry_decision = self.should_enter(trend, bid, ask)

                                # Log trend changes
                                if trend != self.last_trend:
                                    print(f"\n{'='*60}")
                                    print(f"[TREND CHANGE] {self.last_trend} → {trend}")
                                    if wma:
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

                                # Log flat/unknown signals less frequently
                                elif trend == "FLAT":
                                    self.flat_signals += 1
                                elif trend == "UNKNOWN":
                                    self.unknown_signals += 1

                            # Print periodic update every 100 book updates
                            if self.update_count % 100 == 0:
                                timestamp = datetime.now(timezone.utc).strftime('%H:%M:%S')
                                trend = self.candle_builder.get_trend(self.wma_period, self.price_type, self.threshold)
                                spread_bps = self.calculate_spread_bps(bid, ask)

                                stats = self.candle_builder.get_stats()
                                wma = stats['wma_15']

                                if wma:
                                    wma_str = f"WMA: ${wma:.3f}"
                                else:
                                    wma_str = "WMA: Building..."

                                print(f"[{timestamp}] Bid: ${bid:.2f} | Ask: ${ask:.2f} | "
                                      f"Spread: {spread_bps:.2f} bps | {wma_str} | Trend: {trend}")

                except Exception as e:
                    print(f"[ERROR] {e}")

    async def run(self):
        """Run the tester"""
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

    def print_stats(self):
        """Print signal statistics"""
        total_signals = self.long_signals + self.short_signals

        print("\nSIGNAL STATISTICS:")
        print(f"  Total Candles:    {len(self.candle_builder.candles)}")
        print(f"  Book Updates:     {self.update_count}")
        print(f"\n  Entry Signals:    {total_signals}")
        print(f"    LONG signals:   {self.long_signals}")
        print(f"    SHORT signals:  {self.short_signals}")
        print(f"\n  Skipped Signals:")
        print(f"    FLAT (on line): {self.flat_signals}")
        print(f"    UNKNOWN (data): {self.unknown_signals}")


async def main():
    """Main entry point"""
    print("="*80)
    print("WMA TESTER STARTING")
    print("="*80)

    # Create tester with same config as market maker
    tester = WMATester(
        coin="xyz:SILVER",
        wma_period=15,                  # 15-period WMA
        spread_threshold_bps=6.0,       # 6 bps minimum
        spread_position=0.2,            # 20% into spread
        price_type='weighted_close',    # (H+L+C+C)/4
        threshold=0.0005                # 0.05% buffer
    )

    await tester.run()


if __name__ == "__main__":
    print("="*80, flush=True)
    print("WMA TREND TESTER", flush=True)
    print("="*80, flush=True)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[STOPPED] Keyboard interrupt", flush=True)
    except Exception as e:
        print(f"\n[ERROR] Main loop failed: {e}", flush=True)
        import traceback
        traceback.print_exc()
