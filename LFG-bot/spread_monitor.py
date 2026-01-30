"""
Real-time Spread Monitor for Market Making

Tracks orderbook spreads on HIP-3 pairs (Silver, Gold, XYZ100) to determine
if market making strategy is viable.

Key metrics:
- Bid/ask spread (bps)
- Spread distribution (min, avg, max)
- Spread stability (how often it changes)
- Theoretical both-side fill rate

Usage:
    python spread_monitor.py
"""
import asyncio
import websockets
import json
from datetime import datetime, timezone
from collections import deque
import statistics


class SpreadMonitor:
    """Monitor real-time orderbook spreads"""

    def __init__(self, coin="xyz:SILVER"):
        self.coin = coin
        self.ws_url = "wss://api.hyperliquid.xyz/ws"

        # Spread tracking
        self.spreads_bps = deque(maxlen=1000)
        self.last_bid = None
        self.last_ask = None
        self.last_mid = None

        # Change tracking (for fill rate estimation)
        self.bid_changes = 0
        self.ask_changes = 0
        self.total_updates = 0
        self.both_sides_stable = 0  # Updates where neither side changed

        # Summary stats
        self.last_summary_time = datetime.now(timezone.utc)
        self.summary_interval = 60  # seconds

    def process_orderbook(self, levels):
        """
        Process orderbook update

        Args:
            levels: Dict with 'bids' and 'asks' arrays
                   Each is [[price, size], ...]
        """
        try:
            bids = levels.get('bids', [])
            asks = levels.get('asks', [])

            if not bids or not asks:
                return None

            # Best bid/ask (format: {"px": "109.09", "sz": "22.1", "n": 2})
            best_bid = float(bids[0]['px'])
            best_ask = float(asks[0]['px'])
            mid_price = (best_bid + best_ask) / 2

            # Calculate spread
            spread_abs = best_ask - best_bid
            spread_bps = (spread_abs / mid_price) * 10000

            # Track changes
            bid_changed = (self.last_bid != best_bid) if self.last_bid else False
            ask_changed = (self.last_ask != best_ask) if self.last_ask else False

            if self.last_bid is not None:  # Skip first update
                self.total_updates += 1

                if bid_changed:
                    self.bid_changes += 1
                if ask_changed:
                    self.ask_changes += 1

                # Both sides stable = higher chance both fill
                if not bid_changed and not ask_changed:
                    self.both_sides_stable += 1

            # Update tracking
            self.last_bid = best_bid
            self.last_ask = best_ask
            self.last_mid = mid_price
            self.spreads_bps.append(spread_bps)

            return {
                'bid': best_bid,
                'ask': best_ask,
                'mid': mid_price,
                'spread_abs': spread_abs,
                'spread_bps': spread_bps,
                'bid_changed': bid_changed,
                'ask_changed': ask_changed,
                'bid_depth': float(bids[0]['sz']) if bids else 0,
                'ask_depth': float(asks[0]['sz']) if asks else 0,
            }

        except Exception as e:
            print(f"[ERROR] Processing orderbook: {e}")
            return None

    def print_summary(self):
        """Print summary statistics"""
        if len(self.spreads_bps) < 10:
            return

        now = datetime.now(timezone.utc)
        elapsed = (now - self.last_summary_time).total_seconds()

        # Spread stats
        avg_spread = statistics.mean(self.spreads_bps)
        min_spread = min(self.spreads_bps)
        max_spread = max(self.spreads_bps)
        median_spread = statistics.median(self.spreads_bps)
        stdev_spread = statistics.stdev(self.spreads_bps) if len(self.spreads_bps) > 1 else 0

        # Change rate (volatility indicator)
        bid_change_pct = (self.bid_changes / self.total_updates * 100) if self.total_updates > 0 else 0
        ask_change_pct = (self.ask_changes / self.total_updates * 100) if self.total_updates > 0 else 0
        stable_pct = (self.both_sides_stable / self.total_updates * 100) if self.total_updates > 0 else 0

        # Estimate fill rates
        # If spreads are stable, higher chance both sides fill
        # If spreads change a lot, likely only one side fills
        estimated_both_fill_rate = stable_pct * 0.8  # Conservative estimate

        print("\n" + "="*80)
        print(f"SPREAD SUMMARY | {self.coin} | {now.strftime('%H:%M:%S UTC')}")
        print("="*80)
        print(f"Last Price: ${self.last_mid:.2f} | Bid: ${self.last_bid:.2f} | Ask: ${self.last_ask:.2f}")
        print()
        print("SPREAD DISTRIBUTION (basis points):")
        print(f"  Average:  {avg_spread:6.2f} bps")
        print(f"  Median:   {median_spread:6.2f} bps")
        print(f"  Min:      {min_spread:6.2f} bps")
        print(f"  Max:      {max_spread:6.2f} bps")
        print(f"  StdDev:   {stdev_spread:6.2f} bps")
        print()
        print("ORDERBOOK VOLATILITY:")
        print(f"  Total updates:       {self.total_updates:6d}")
        print(f"  Bid changes:         {self.bid_changes:6d} ({bid_change_pct:5.1f}%)")
        print(f"  Ask changes:         {self.ask_changes:6d} ({ask_change_pct:5.1f}%)")
        print(f"  Both sides stable:   {self.both_sides_stable:6d} ({stable_pct:5.1f}%)")
        print()
        print("MARKET MAKING VIABILITY:")
        print(f"  Est. both-fill rate: {estimated_both_fill_rate:5.1f}%")

        # Reference our model breakeven rates
        print()
        print("  Breakeven fill rates (from model):")
        if avg_spread >= 10:
            print(f"    10+ bps spread: Need ~51-67% both-fill → EASILY VIABLE")
        elif avg_spread >= 5:
            print(f"    5-10 bps spread: Need ~67-80% both-fill → VIABLE")
        elif avg_spread >= 2:
            print(f"    2-5 bps spread: Need ~72-91% both-fill → MARGINAL")
        else:
            print(f"    <2 bps spread: Need ~84-95% both-fill → DIFFICULT")

        # Assessment
        print()
        if avg_spread >= 5 and estimated_both_fill_rate >= 60:
            print("  ASSESSMENT: ✓ GOOD for market making")
        elif avg_spread >= 2 and estimated_both_fill_rate >= 70:
            print("  ASSESSMENT: ~ MARGINAL for market making")
        else:
            print("  ASSESSMENT: ✗ RISKY for market making")

        print("="*80 + "\n")

        # Reset counters
        self.bid_changes = 0
        self.ask_changes = 0
        self.total_updates = 0
        self.both_sides_stable = 0
        self.last_summary_time = now

    async def connect(self):
        """Connect to websocket and stream orderbook data"""
        print("="*80)
        print(f"ORDERBOOK SPREAD MONITOR - {self.coin}")
        print("="*80)
        print(f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print("\nConnecting to HyperLiquid websocket...")
        print("\nMonitoring:")
        print("  - Bid/ask spread (bps)")
        print("  - Spread stability")
        print("  - Estimated both-side fill rates")
        print(f"\nSummary every {self.summary_interval} seconds")
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

            # Track time for summaries
            last_summary = datetime.now(timezone.utc)
            update_count = 0

            # Listen for messages
            async for message in ws:
                try:
                    data = json.loads(message)

                    # Handle subscription response
                    if data.get('channel') == 'subscriptionResponse':
                        print(f"[OK] Subscribed to {self.coin} L2 orderbook\n")
                        print(f"{'Time':<10} {'Bid':>10} {'Ask':>10} {'Spread (bps)':>14} {'Changed':<10}")
                        print("-"*80)
                        continue

                    # Handle L2 book data
                    if data.get('channel') == 'l2Book':
                        book_data = data.get('data', {})
                        levels = book_data.get('levels', [[],[]])

                        # levels is [bids, asks]
                        if isinstance(levels, list) and len(levels) == 2:
                            orderbook = {
                                'bids': levels[0],
                                'asks': levels[1]
                            }

                            result = self.process_orderbook(orderbook)

                            if result:
                                update_count += 1

                                # Print every 10th update (reduce spam)
                                if update_count % 10 == 0:
                                    timestamp = datetime.now(timezone.utc).strftime('%H:%M:%S')
                                    changed = []
                                    if result['bid_changed']:
                                        changed.append('BID')
                                    if result['ask_changed']:
                                        changed.append('ASK')
                                    changed_str = '+'.join(changed) if changed else '-'

                                    print(f"{timestamp} ${result['bid']:>9.2f} ${result['ask']:>9.2f} "
                                          f"{result['spread_bps']:>13.2f} {changed_str:<10}")

                    # Print summary periodically
                    now = datetime.now(timezone.utc)
                    if (now - last_summary).total_seconds() >= self.summary_interval:
                        self.print_summary()
                        last_summary = now

                except json.JSONDecodeError:
                    print(f"[ERROR] Failed to decode: {message}")
                except Exception as e:
                    print(f"[ERROR] {e}")

    async def run(self):
        """Run the monitor"""
        try:
            await self.connect()
        except KeyboardInterrupt:
            print("\n\n" + "="*80)
            print("[STOPPED] User interrupted")
            print("="*80)
            if len(self.spreads_bps) > 0:
                print(f"Total updates: {len(self.spreads_bps)}")
                print(f"Average spread: {statistics.mean(self.spreads_bps):.2f} bps")
            print("="*80)
        except Exception as e:
            print(f"\n\n[ERROR] {e}")
            import traceback
            traceback.print_exc()


async def main():
    """Main entry point"""
    # Choose coin to monitor
    coins = [
        "xyz:SILVER",   # Most liquid
        # "xyz:GOLD",
        # "xyz:XYZ100",
    ]

    monitor = SpreadMonitor(coin=coins[0])
    await monitor.run()


if __name__ == "__main__":
    asyncio.run(main())
