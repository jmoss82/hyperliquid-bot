"""
5-Second Candle Builder from Tick Data

Builds OHLC candles from streaming bid/ask data and calculates WMA.
"""
from collections import deque
from dataclasses import dataclass
from typing import Optional, List
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


@dataclass
class SwingPoint:
    """Market structure swing point (support/resistance)"""
    timestamp: float
    price: float
    swing_type: str  # "high" for resistance, "low" for support
    index: int  # Position in candle history when detected


class CandleBuilder:
    """Builds 5-second candles from streaming tick data"""

    def __init__(self, candle_interval_seconds: int = 5, max_candles: int = 400,
                 swing_lookback_left: int = 180, swing_lookback_right: int = 12,
                 swing_dedup_candles: int = 36, swing_dedup_bps: float = 15.0):
        """
        Initialize candle builder.

        Args:
            candle_interval_seconds: How often to close a candle (default: 5s)
            max_candles: Maximum number of candles to store (default: 400)
            swing_lookback_left: Candles before pivot to confirm high/low (default: 180 = 15 minutes)
            swing_lookback_right: Candles after pivot to confirm reversal (default: 12 = 1 minute)
            swing_dedup_candles: Don't report swings within this many candles of last (default: 36 = 3 min)
            swing_dedup_bps: Don't report swings within this many bps of last (default: 15 bps)
        """
        self.candle_interval = candle_interval_seconds
        self.candles: deque[Candle] = deque(maxlen=max_candles)
        self.current_candle: Optional[Candle] = None
        self.candle_start_time: float = 0

        # Swing point tracking (asymmetric for faster detection)
        self.swing_lookback_left = swing_lookback_left
        self.swing_lookback_right = swing_lookback_right
        self.swing_dedup_candles = swing_dedup_candles
        self.swing_dedup_bps = swing_dedup_bps
        self.swing_points: List[SwingPoint] = []  # All detected swing points
        self.last_swing_high: Optional[SwingPoint] = None
        self.last_swing_low: Optional[SwingPoint] = None

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

                # Check for swing points after adding completed candle
                self._check_swing()

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

    def _check_swing(self) -> None:
        """
        Check if the last completed candle forms a swing point.

        Uses asymmetric lookback for faster detection:
        - Left (history): 180 candles (15 min) - confirms it's a real pivot
        - Right (confirmation): 12 candles (1 min) - confirms reversal quickly

        Example with 180/12: pivot at index -13 needs:
        - 180 candles before it (index -193 to -13)
        - 12 candles after it (index -12 to -1)
        """
        required_candles = self.swing_lookback_left + self.swing_lookback_right + 1
        if len(self.candles) < required_candles:
            return  # Not enough data yet

        # Check the candle at position -(right_lookback + 1)
        # This ensures we have right_lookback candles after it for confirmation
        check_idx = -(self.swing_lookback_right + 1)
        pivot_candle = list(self.candles)[check_idx]

        # Get surrounding candles (asymmetric)
        left_candles = list(self.candles)[check_idx - self.swing_lookback_left:check_idx]
        right_candles = list(self.candles)[check_idx + 1:check_idx + 1 + self.swing_lookback_right]

        # Check for swing high (resistance)
        is_swing_high = all(pivot_candle.high > c.high for c in left_candles + right_candles)
        if is_swing_high:
            self._add_swing(pivot_candle.timestamp, pivot_candle.high, "high", len(self.candles) + check_idx)

        # Check for swing low (support)
        is_swing_low = all(pivot_candle.low < c.low for c in left_candles + right_candles)
        if is_swing_low:
            self._add_swing(pivot_candle.timestamp, pivot_candle.low, "low", len(self.candles) + check_idx)

    def _add_swing(self, timestamp: float, price: float, swing_type: str, index: int) -> None:
        """
        Record a new swing point with deduplication.

        Args:
            timestamp: When the swing occurred
            price: Price level of the swing
            swing_type: "high" or "low"
            index: Position in candle history
        """
        # Deduplication: Skip if too close to last swing of same type
        last_swing = self.last_swing_high if swing_type == "high" else self.last_swing_low

        if last_swing is not None:
            # Calculate candle distance (how many candles since last swing)
            candles_since = len(self.candles) - abs(last_swing.index)

            # Calculate price distance in bps
            price_diff_bps = abs((price - last_swing.price) / last_swing.price) * 10000

            # Skip if within dedup window (both time AND price)
            if candles_since < self.swing_dedup_candles and price_diff_bps < self.swing_dedup_bps:
                return  # Skip this swing (too close to last one)

        # Not a duplicate - add it
        swing = SwingPoint(
            timestamp=timestamp,
            price=price,
            swing_type=swing_type,
            index=index
        )

        self.swing_points.append(swing)

        # Update last swing trackers
        if swing_type == "high":
            self.last_swing_high = swing
        else:
            self.last_swing_low = swing

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

    def get_structure(self) -> List[SwingPoint]:
        """
        Get all detected swing points.

        Returns:
            List of SwingPoint objects in chronological order
        """
        return self.swing_points.copy()

    def get_last_support(self) -> Optional[SwingPoint]:
        """
        Get the most recent swing low (support level).

        Returns:
            Last detected SwingPoint with type "low", or None
        """
        return self.last_swing_low

    def get_last_resistance(self) -> Optional[SwingPoint]:
        """
        Get the most recent swing high (resistance level).

        Returns:
            Last detected SwingPoint with type "high", or None
        """
        return self.last_swing_high

    def get_structure_stats(self) -> dict:
        """
        Get market structure statistics.

        Returns:
            Dictionary with swing point counts and levels
        """
        highs = [sp for sp in self.swing_points if sp.swing_type == "high"]
        lows = [sp for sp in self.swing_points if sp.swing_type == "low"]

        return {
            'total_swings': len(self.swing_points),
            'swing_highs': len(highs),
            'swing_lows': len(lows),
            'last_resistance': self.last_swing_high.price if self.last_swing_high else None,
            'last_support': self.last_swing_low.price if self.last_swing_low else None,
            'resistance_time': self.last_swing_high.timestamp if self.last_swing_high else None,
            'support_time': self.last_swing_low.timestamp if self.last_swing_low else None
        }
