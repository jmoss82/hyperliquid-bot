"""
Order Flow Signal Generator

Monitors aggressive order flow to generate trading signals:
- Tracks buy vs sell volume (15-second windows)
- Calculates cumulative delta (net buying/selling pressure)
- Detects whale trades (>$10k)
- Generates signals when imbalance >30% + cumulative trend confirms

Usage:
    python signals.py                    # Monitor xyz:SILVER
    python signals.py --coin xyz:GOLD    # Monitor different asset

How it works:
- "side": "B" = Market BUY (hit ask) = Bullish
- "side": "A" = Market SELL (hit bid) = Bearish
- Filters trades <$1,000 to remove retail noise
- Updates every 15 seconds with volume analysis
"""
import asyncio
import websockets
import json
import argparse
from datetime import datetime, timezone
from collections import deque


class OrderFlowMonitor:
    """Real-time order flow analysis"""

    def __init__(self, coin="xyz:SILVER", min_trade_size=1000):
        self.coin = coin
        self.ws_url = "wss://api.hyperliquid.xyz/ws"
        self.min_trade_size = min_trade_size

        # Cumulative tracking
        self.cumulative_buy_volume = 0
        self.cumulative_sell_volume = 0
        self.cumulative_delta = 0  # Running buy - sell

        # 15-second window
        self.window_buy_volume = 0
        self.window_sell_volume = 0
        self.window_trades = 0
        self.last_window_reset = datetime.now(timezone.utc)

        # Price tracking
        self.last_price = None
        self.window_high = 0
        self.window_low = float('inf')

        # Whale tracking
        self.large_order_threshold = 10000  # $10k+
        self.recent_large_orders = deque(maxlen=10)

        # Signal tracking
        self.signal_count = 0
        self.last_signal = None
        self.last_signal_price = None

    def reset_window(self):
        """Reset 15-second window"""
        self.window_buy_volume = 0
        self.window_sell_volume = 0
        self.window_trades = 0
        self.window_high = 0
        self.window_low = float('inf')
        self.last_window_reset = datetime.now(timezone.utc)

    def process_trade(self, trade):
        """Process aggressive trade"""
        try:
            price = float(trade.get('px', 0))
            size = float(trade.get('sz', 0))
            side = trade.get('side', '')

            if not price or not size:
                return None

            notional = price * size

            # Filter small retail trades
            if notional < self.min_trade_size:
                return None

            # Update price
            self.last_price = price
            if price > self.window_high:
                self.window_high = price
            if price < self.window_low:
                self.window_low = price

            # Track volume by side
            is_buy = (side == 'B')

            if is_buy:
                self.window_buy_volume += notional
                self.cumulative_buy_volume += notional
                self.cumulative_delta += notional
            else:
                self.window_sell_volume += notional
                self.cumulative_sell_volume += notional
                self.cumulative_delta -= notional

            self.window_trades += 1

            # Track large orders
            is_large = (notional >= self.large_order_threshold)
            if is_large:
                self.recent_large_orders.append({
                    'time': datetime.now(timezone.utc).strftime('%H:%M:%S'),
                    'side': 'BUY' if is_buy else 'SELL',
                    'price': price,
                    'notional': notional
                })

            return {
                'price': price,
                'size': size,
                'notional': notional,
                'is_buy': is_buy,
                'is_large': is_large
            }

        except Exception as e:
            print(f"[ERROR] Processing trade: {e}")
            return None

    def calculate_signal(self):
        """Generate signal from volume imbalance + cumulative trend"""
        window_total = self.window_buy_volume + self.window_sell_volume

        # Need minimum volume
        if window_total < 5000:
            return None

        # Window imbalance (short-term)
        window_imbalance = (self.window_buy_volume - self.window_sell_volume) / window_total

        # Cumulative trend (medium-term)
        cumulative_total = self.cumulative_buy_volume + self.cumulative_sell_volume
        if cumulative_total > 0:
            cumulative_pct = (self.cumulative_delta / cumulative_total) * 100
        else:
            cumulative_pct = 0

        # Generate signal (requires both short-term imbalance AND trend confirmation)
        signal = None

        if window_imbalance > 0.4 and self.cumulative_delta > 0:
            signal = "STRONG_LONG"
        elif window_imbalance > 0.3:
            signal = "LONG"
        elif window_imbalance < -0.4 and self.cumulative_delta < 0:
            signal = "STRONG_SHORT"
        elif window_imbalance < -0.3:
            signal = "SHORT"

        return signal, window_imbalance, cumulative_pct

    def print_update(self):
        """Print 15-second update"""
        now = datetime.now(timezone.utc)
        window_total = self.window_buy_volume + self.window_sell_volume

        if window_total == 0:
            return

        window_buy_pct = (self.window_buy_volume / window_total * 100)
        window_sell_pct = (self.window_sell_volume / window_total * 100)

        signal_result = self.calculate_signal()

        # Header
        print("\n" + "="*90)
        print(f"ORDER FLOW | {self.coin} | {now.strftime('%H:%M:%S UTC')} | "
              f"${self.last_price:.2f} (${self.window_low:.2f}-${self.window_high:.2f})")
        print("="*90)

        # 15-sec window
        print(f"15s Window:  {self.window_trades} trades | ${window_total:>10,.0f} total")
        print(f"  Buys:  ${self.window_buy_volume:>10,.0f} ({window_buy_pct:>5.1f}%)")
        print(f"  Sells: ${self.window_sell_volume:>10,.0f} ({window_sell_pct:>5.1f}%)")

        # Cumulative delta
        cumulative_total = self.cumulative_buy_volume + self.cumulative_sell_volume
        print(f"\nCumulative Delta: ${self.cumulative_delta:>+12,.0f} | "
              f"Total: ${cumulative_total:>12,.0f}")
        if cumulative_total > 0:
            delta_pct = (self.cumulative_delta / cumulative_total) * 100
            print(f"  -> Net pressure: {delta_pct:+.1f}% {'BULLISH' if delta_pct > 0 else 'BEARISH'}")

        # Recent large orders
        if self.recent_large_orders:
            print(f"\nRecent Large Orders (>${self.large_order_threshold:,.0f}):")
            for order in list(self.recent_large_orders)[-3:]:
                print(f"  {order['time']} {order['side']:4s} @ ${order['price']:.2f} "
                      f"= ${order['notional']:>10,.0f}")

        # Signal
        if signal_result:
            signal, window_imb, cum_pct = signal_result

            if signal:
                self.signal_count += 1

                # Price change since last signal
                price_change = ""
                if self.last_signal_price:
                    change = ((self.last_price - self.last_signal_price) / self.last_signal_price) * 100
                    price_change = f" | Prev signal: {change:+.2f}%"

                print(f"\n{'*'*90}")
                print(f"*** SIGNAL #{self.signal_count}: {signal} ***{price_change}")

                if "LONG" in signal:
                    print(f"-> Aggressive BUYING detected")
                    print(f"   15s: {window_buy_pct:.1f}% buys vs {window_sell_pct:.1f}% sells")
                    print(f"   Cumulative: ${self.cumulative_delta:+,.0f} net buying pressure")
                    print(f"   Action: Consider LONG @ ${self.last_price:.2f}")
                else:
                    print(f"-> Aggressive SELLING detected")
                    print(f"   15s: {window_sell_pct:.1f}% sells vs {window_buy_pct:.1f}% buys")
                    print(f"   Cumulative: ${abs(self.cumulative_delta):,.0f} net selling pressure")
                    print(f"   Action: Consider SHORT @ ${self.last_price:.2f}")

                print(f"{'*'*90}")

                self.last_signal = signal
                self.last_signal_price = self.last_price

        print("="*90 + "\n")

        # Reset window
        self.reset_window()

    async def connect(self):
        """Connect to websocket and stream trades"""
        print("="*90)
        print(f"ORDER FLOW MONITOR - {self.coin}")
        print("="*90)
        print(f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"\nFilters:")
        print(f"  - Min trade size: ${self.min_trade_size:,.0f} (filters retail)")
        print(f"  - Large orders: >${self.large_order_threshold:,.0f} (whale tracking)")
        print(f"  - Update interval: 15 seconds")
        print(f"\nStrategy:")
        print(f"  - Track aggressive market orders ONLY")
        print(f"  - Cumulative delta shows net institutional positioning")
        print(f"  - Signals when 15s window shows >30% imbalance")
        print(f"\nCompare with live chart:")
        print(f"  https://app.hyperliquid.xyz/trade/{self.coin}")
        print("="*90 + "\n")

        async with websockets.connect(self.ws_url) as ws:
            subscribe_msg = {
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": self.coin}
            }
            await ws.send(json.dumps(subscribe_msg))

            last_update = datetime.now(timezone.utc)

            async for message in ws:
                try:
                    data = json.loads(message)

                    if data.get('channel') == 'subscriptionResponse':
                        print(f"[OK] Connected to {self.coin} trade stream\n")
                        continue

                    if data.get('channel') == 'trades':
                        for trade in data.get('data', []):
                            result = self.process_trade(trade)

                            if result and result['is_large']:
                                # Print large orders immediately
                                side = "BUY " if result['is_buy'] else "SELL"
                                print(f"[WHALE] {side} ${result['notional']:>10,.0f} @ "
                                      f"${result['price']:.2f}")

                    # Print update every 15 seconds
                    now = datetime.now(timezone.utc)
                    if (now - last_update).total_seconds() >= 15:
                        self.print_update()
                        last_update = now

                except Exception as e:
                    print(f"[ERROR] {e}")

    async def run(self):
        """Start monitoring"""
        try:
            await self.connect()
        except KeyboardInterrupt:
            print("\n\n" + "="*90)
            print(f"[STOPPED] Generated {self.signal_count} signals")
            print(f"Final cumulative delta: ${self.cumulative_delta:+,.0f}")
            print("="*90)
        except Exception as e:
            print(f"\n[ERROR] {e}")
            import traceback
            traceback.print_exc()


def main():
    """CLI interface"""
    parser = argparse.ArgumentParser(description='Order Flow Monitor')
    parser.add_argument('--coin', default='xyz:SILVER', help='Trading pair')
    parser.add_argument('--min-size', type=float, default=1000, help='Min trade size ($)')

    args = parser.parse_args()

    monitor = OrderFlowMonitor(
        coin=args.coin,
        min_trade_size=args.min_size
    )

    asyncio.run(monitor.run())


if __name__ == "__main__":
    main()
