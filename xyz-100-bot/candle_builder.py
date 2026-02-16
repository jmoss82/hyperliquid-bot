"""
5-Second Candle Builder from Tick Data

Builds OHLC candles from streaming bid/ask data and calculates WMA.
"""
from collections import deque
from dataclasses import dataclass
from typing import Optional
import time


@dataclass
class Candle:
    """5-second OHLC candle"""
    timestamp: float
    open: float
    high: float
    low: float
    close: float

    def weighted_close(self) -> float:
        """Calculate (H+L+C+C)/4 - Heiken-Ashi style weighted close"""
        return (self.high + self.low + self.close + self.close) / 4

    def mid_price(self) -> float:
        """Calculate (H+L)/2 - simple mid price"""
        return (self.high + self.low) / 2


class CandleBuilder:
    """Builds 5-second candles from streaming tick data"""

    def __init__(self, candle_interval_seconds: int = 5, max_candles: int = 400):
        """
        Initialize candle builder.

        Args:
            candle_interval_seconds: How often to close a candle (default: 5s)
            max_candles: Maximum number of candles to store (default: 400)
        """
        self.candle_interval = candle_interval_seconds
        self.candles: deque[Candle] = deque(maxlen=max_candles)
        self.current_candle: Optional[Candle] = None
        self.candle_start_time: float = 0

    def update(self, bid: float, ask: float, timestamp: Optional[float] = None) -> Optional[Candle]:
        """
        Update candles with new bid/ask tick.

        Args:
            bid: Current best bid
            ask: Current best ask
            timestamp: Tick timestamp (uses time.time() if None)

        Returns:
            Completed candle if one was closed, None otherwise
        """
        if timestamp is None:
            timestamp = time.time()

        mid = (bid + ask) / 2
        completed_candle = None

        # Check if we need to start a new candle
        if self.current_candle is None or timestamp - self.candle_start_time >= self.candle_interval:
            # Save current candle if it exists
            if self.current_candle is not None:
                self.candles.append(self.current_candle)
                completed_candle = self.current_candle

            # Start new candle
            self.current_candle = Candle(
                timestamp=timestamp,
                open=mid,
                high=mid,
                low=mid,
                close=mid
            )
            self.candle_start_time = timestamp
        else:
            # Update current candle
            self.current_candle.high = max(self.current_candle.high, mid)
            self.current_candle.low = min(self.current_candle.low, mid)
            self.current_candle.close = mid

        return completed_candle

    def calculate_wma(self, period: int, price_type: str = 'weighted_close') -> Optional[float]:
        """
        Calculate Weighted Moving Average.

        Args:
            period: Number of candles to use (e.g., 15)
            price_type: 'weighted_close' for (H+L+C+C)/4, 'mid_price' for (H+L)/2, 'close' for C

        Returns:
            WMA value, or None if insufficient data
        """
        if len(self.candles) < period:
            return None

        # Get last 'period' candles
        recent_candles = list(self.candles)[-period:]

        # Extract prices based on type
        if price_type == 'weighted_close':
            prices = [c.weighted_close() for c in recent_candles]
        elif price_type == 'mid_price':
            prices = [c.mid_price() for c in recent_candles]
        elif price_type == 'close':
            prices = [c.close for c in recent_candles]
        else:
            raise ValueError(f"Invalid price_type: {price_type}")

        # Calculate WMA (most recent price gets highest weight)
        weights = range(1, period + 1)
        weighted_sum = sum(p * w for p, w in zip(prices, weights))
        weight_sum = sum(weights)  # equals period * (period + 1) / 2

        return weighted_sum / weight_sum

    def get_trend(self, period: int = 15, price_type: str = 'weighted_close',
                  threshold: float = 0.0005) -> str:
        """
        Determine current trend based on price vs WMA.

        Args:
            period: WMA period (default: 15)
            price_type: Price calculation method (default: 'weighted_close')
            threshold: Buffer percentage to avoid whipsaw (default: 0.05%)

        Returns:
            'UP', 'DOWN', 'FLAT', or 'UNKNOWN' (insufficient data)
        """
        wma = self.calculate_wma(period, price_type)

        if wma is None or self.current_candle is None:
            return "UNKNOWN"

        # Get current price
        if price_type == 'weighted_close':
            current_price = self.current_candle.weighted_close()
        elif price_type == 'mid_price':
            current_price = self.current_candle.mid_price()
        elif price_type == 'close':
            current_price = self.current_candle.close
        else:
            current_price = self.current_candle.close

        # Apply threshold to avoid trading right on the line
        upper_threshold = wma * (1 + threshold)
        lower_threshold = wma * (1 - threshold)

        if current_price > upper_threshold:
            return "UP"
        elif current_price < lower_threshold:
            return "DOWN"
        else:
            return "FLAT"

    def get_stats(self) -> dict:
        """Get current statistics for debugging/monitoring"""
        if not self.candles or not self.current_candle:
            return {
                'total_candles': 0,
                'wma_15': None,
                'trend': 'UNKNOWN',
                'current_price': None
            }

        return {
            'total_candles': len(self.candles),
            'wma_15': self.calculate_wma(15),
            'trend': self.get_trend(15),
            'current_price': self.current_candle.weighted_close(),
            'current_candle': {
                'open': self.current_candle.open,
                'high': self.current_candle.high,
                'low': self.current_candle.low,
                'close': self.current_candle.close
            }
        }
