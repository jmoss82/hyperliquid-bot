"""
Directional trend monitor for manual trading.

Purpose:
- Provide a clean UP/DOWN/FLAT trend read based on WMA and hysteresis
- Print trend changes and a compact status line
- No paper trades, no exits, no order logic

Usage:
    python directional_trend_tester.py
"""
import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

import websockets

from candle_builder import CandleBuilder


class DirectionalTrendTester:
    """Directional trend monitor (WMA + hysteresis)."""

    def __init__(
        self,
        coin: str = "xyz:SILVER",
        wma_period: int = 60,
        price_type: str = "weighted_close",
        candle_interval_seconds: int = 5,
        max_candles: int = 400,
        trend_enter_bps: float = 4.0,
        trend_exit_bps: float = 8.0,
        wma_threshold: float = 0.0005,
    ):
        self.coin = coin
        self.wma_period = wma_period
        self.price_type = price_type
        self.candle_interval_seconds = candle_interval_seconds
        self.max_candles = max_candles
        self.trend_enter_bps = trend_enter_bps
        self.trend_exit_bps = trend_exit_bps
        self.wma_threshold = wma_threshold

        self.ws_url = "wss://api.hyperliquid.xyz/ws"
        self.current_bid = None
        self.current_ask = None

        self.candle_builder = CandleBuilder(
            candle_interval_seconds=self.candle_interval_seconds,
            max_candles=self.max_candles,
        )

        self.trend_state = "UNKNOWN"
        self.last_trend = "UNKNOWN"
        self.up_streak = 0
        self.down_streak = 0
        self.position = None  # "LONG", "SHORT", or None

    def candle_price(self, candle) -> float:
        if self.price_type == "weighted_close":
            return candle.weighted_close()
        if self.price_type == "mid_price":
            return candle.mid_price()
        return candle.close

    def get_wma(self):
        return self.candle_builder.calculate_wma(self.wma_period, self.price_type)

    def trend_from_price(self, wma: Optional[float], price: Optional[float]) -> str:
        if wma is None or price is None:
            return "UNKNOWN"
        upper = wma * (1 + self.wma_threshold)
        lower = wma * (1 - self.wma_threshold)
        if price > upper:
            return "UP"
        if price < lower:
            return "DOWN"
        return "FLAT"

    def update_trend_state(self, wma: Optional[float], price: Optional[float]) -> str:
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

    async def run(self):
        print("=" * 80)
        print(f"DIRECTIONAL TREND MONITOR - {self.coin}")
        print("=" * 80)
        print(f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print("\nConfiguration:")
        print(f"  Candle Interval:      {self.candle_interval_seconds}s")
        print(f"  WMA Period:           {self.wma_period}")
        print(f"  Price Type:           {self.price_type}")
        print(f"  Trend Enter/Exit:     {self.trend_enter_bps:.1f} / {self.trend_exit_bps:.1f} bps")
        print(f"  WMA Threshold:        {self.wma_threshold:.2%}")
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
                data = json.loads(message)
                if data.get("channel") != "l2Book":
                    continue

                book_data = data.get("data", {})
                levels = book_data.get("levels", [[], []])
                if len(levels) != 2 or not levels[0] or not levels[1]:
                    continue

                bid = float(levels[0][0]["px"])
                ask = float(levels[1][0]["px"])
                self.current_bid = bid
                self.current_ask = ask

                completed_candle = self.candle_builder.update(bid, ask)
                if not completed_candle:
                    continue

                price = self.candle_price(completed_candle)
                wma = self.get_wma()
                raw_trend = self.trend_from_price(wma, price)
                trend = self.update_trend_state(wma, price)

                if trend != self.last_trend:
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    wma_str = f"{wma:.3f}" if wma else "n/a"
                    print("\n" + "=" * 60)
                    print(f"[{ts}] TREND CHANGE: {self.last_trend} -> {trend} (raw {raw_trend})")
                    print(f"Price: ${price:.3f} | WMA: ${wma_str}")
                    print("=" * 60 + "\n")
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
                else:
                    self.up_streak = 0
                    self.down_streak = 0

                desired_position = None
                if self.up_streak >= 5:
                    desired_position = "LONG"
                elif self.down_streak >= 5:
                    desired_position = "SHORT"

                if desired_position is not None and desired_position != self.position:
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    if self.position == "LONG":
                        print(f"[{ts}] EXIT LONG | price=${price:.3f} | up_streak={self.up_streak} down_streak={self.down_streak}")
                    elif self.position == "SHORT":
                        print(f"[{ts}] EXIT SHORT | price=${price:.3f} | up_streak={self.up_streak} down_streak={self.down_streak}")

                    if desired_position == "LONG":
                        print(f"[{ts}] ENTER LONG | price=${price:.3f} | up_streak={self.up_streak}")
                    elif desired_position == "SHORT":
                        print(f"[{ts}] ENTER SHORT | price=${price:.3f} | down_streak={self.down_streak}")

                    self.position = desired_position

                # Status line on every completed candle (more transparent streaks)
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                wma_str = f"{wma:.3f}" if wma else "n/a"
                line = (
                    f"[{ts}] Price: ${price:.3f} | WMA: ${wma_str} | "
                    f"Trend: {trend} | streaks U:{self.up_streak} D:{self.down_streak} | pos: {self.position or 'FLAT'}"
                )
                print(line)


if __name__ == "__main__":
    asyncio.run(DirectionalTrendTester().run())
