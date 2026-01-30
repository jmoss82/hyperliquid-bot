"""
Microstructure Signal Monitor v2 - Upgraded based on feedback

Improvements:
- Subscribes to TRADES feed (buyer/seller initiated volume)
- Tracks best-level depletion (size collapsing)
- Measures spread persistence (how long does wide spread last)
- Tracks mid volatility (is price whipping around?)
- Removed fake "fill rate" estimates

Usage:
    python micro_monitor.py
"""

import asyncio
import json
import time
from datetime import datetime
from collections import deque
from typing import Optional, List, Tuple
import websockets
import math

# Configuration
COIN = "SILVER"
WS_URL = "wss://api.hyperliquid.xyz/ws"

# Signal thresholds
T_SOFT = 0.3   # Soft threshold - skew orders
T_HARD = 0.6   # Hard threshold - don't trade
IMBALANCE_LEVELS = 5

# Time windows
TRADE_FLOW_WINDOW_MS = 1000   # Track trades over 1 second
VOLATILITY_WINDOW_MS = 2000   # Track mid volatility over 2 seconds
DEPLETION_WINDOW_MS = 500     # Track size changes over 500ms


class MicrostructureMonitor:
    """Monitor order book + trade flow for adverse selection signals."""
    
    def __init__(self):
        # Current book state
        self.bids: List[Tuple[float, float]] = []  # [(price, size), ...]
        self.asks: List[Tuple[float, float]] = []
        
        # Historical tracking
        self.mid_history = deque(maxlen=100)       # (timestamp_ms, mid)
        self.spread_history = deque(maxlen=100)    # (timestamp_ms, spread_bps)
        self.best_bid_size_history = deque(maxlen=50)  # (timestamp_ms, size)
        self.best_ask_size_history = deque(maxlen=50)
        
        # Trade flow tracking
        self.trades = deque(maxlen=200)  # (timestamp_ms, side, size, price)
        self.buy_volume_1s = 0.0   # Buyer-initiated volume last 1s
        self.sell_volume_1s = 0.0  # Seller-initiated volume last 1s
        
        # Spread persistence tracking
        self.spread_above_threshold_start: Optional[float] = None
        self.spread_threshold_bps = 6.0
        
        # Stats
        self.signal_counts = {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0, "NO_TRADE": 0}
        self.book_updates = 0
        self.trade_updates = 0
        
        # Spread distribution
        self.spread_buckets = {"<4": 0, "4-6": 0, ">=6": 0}
        self.total_spread_samples = 0
    
    def update_book(self, levels: list):
        """Update order book from websocket data."""
        if len(levels) < 2 or not levels[0] or not levels[1]:
            return
        
        now = time.time() * 1000
        
        # Track previous best sizes for depletion detection
        prev_bid_size = self.bids[0][1] if self.bids else None
        prev_ask_size = self.asks[0][1] if self.asks else None
        
        # Update book
        self.bids = [(float(l["px"]), float(l["sz"])) for l in levels[0][:10]]
        self.asks = [(float(l["px"]), float(l["sz"])) for l in levels[1][:10]]
        self.book_updates += 1
        
        if not self.bids or not self.asks:
            return
        
        # Track mid
        mid = (self.bids[0][0] + self.asks[0][0]) / 2
        self.mid_history.append((now, mid))
        
        # Track spread
        spread_bps = ((self.asks[0][0] - self.bids[0][0]) / self.bids[0][0]) * 10000
        self.spread_history.append((now, spread_bps))
        
        # Spread distribution
        self.total_spread_samples += 1
        if spread_bps < 4:
            self.spread_buckets["<4"] += 1
        elif spread_bps < 6:
            self.spread_buckets["4-6"] += 1
        else:
            self.spread_buckets[">=6"] += 1
        
        # Spread persistence tracking
        if spread_bps >= self.spread_threshold_bps:
            if self.spread_above_threshold_start is None:
                self.spread_above_threshold_start = now
        else:
            self.spread_above_threshold_start = None
        
        # Track best level sizes for depletion detection
        self.best_bid_size_history.append((now, self.bids[0][1]))
        self.best_ask_size_history.append((now, self.asks[0][1]))
    
    def update_trade(self, trade_data: dict):
        """Update from trade/fill websocket data."""
        try:
            now = time.time() * 1000
            
            # Parse trade - HyperLiquid format
            # Trade is buyer-initiated if it happened at/near ask (someone lifted)
            # Trade is seller-initiated if it happened at/near bid (someone hit)
            
            price = float(trade_data.get("px", 0))
            size = float(trade_data.get("sz", 0))
            
            if not self.bids or not self.asks:
                return
            
            mid = (self.bids[0][0] + self.asks[0][0]) / 2
            
            # Determine aggressor side
            # If trade price >= mid, buyer was aggressor (lifted ask)
            # If trade price < mid, seller was aggressor (hit bid)
            side = "buy" if price >= mid else "sell"
            
            self.trades.append((now, side, size, price))
            self.trade_updates += 1
            
            # Update rolling volume
            self._update_trade_volumes()
            
        except Exception as e:
            pass
    
    def _update_trade_volumes(self):
        """Recalculate trade volumes over the window."""
        now = time.time() * 1000
        cutoff = now - TRADE_FLOW_WINDOW_MS
        
        buy_vol = 0.0
        sell_vol = 0.0
        
        for ts, side, size, _ in self.trades:
            if ts >= cutoff:
                if side == "buy":
                    buy_vol += size
                else:
                    sell_vol += size
        
        self.buy_volume_1s = buy_vol
        self.sell_volume_1s = sell_vol
    
    def get_trade_flow_imbalance(self) -> Optional[float]:
        """
        Trade flow imbalance over last 1 second.
        TI = (buyVol - sellVol) / (buyVol + sellVol)
        Positive = buyers aggressive (bullish)
        """
        total = self.buy_volume_1s + self.sell_volume_1s
        if total == 0:
            return None
        return (self.buy_volume_1s - self.sell_volume_1s) / total
    
    def get_microprice(self) -> Optional[float]:
        """Size-weighted fair value price."""
        if not self.bids or not self.asks:
            return None
        
        bid_price, bid_size = self.bids[0]
        ask_price, ask_size = self.asks[0]
        
        if bid_size + ask_size == 0:
            return None
        
        return (bid_price * ask_size + ask_price * bid_size) / (ask_size + bid_size)
    
    def get_mid(self) -> Optional[float]:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0][0] + self.asks[0][0]) / 2
    
    def get_spread_bps(self) -> Optional[float]:
        if not self.bids or not self.asks:
            return None
        return ((self.asks[0][0] - self.bids[0][0]) / self.bids[0][0]) * 10000
    
    def get_book_imbalance(self, levels: int = 5) -> Optional[float]:
        """Order book imbalance at top N levels."""
        if len(self.bids) < levels or len(self.asks) < levels:
            return None
        
        bid_vol = sum(s for _, s in self.bids[:levels])
        ask_vol = sum(s for _, s in self.asks[:levels])
        
        if bid_vol + ask_vol == 0:
            return None
        
        return (bid_vol - ask_vol) / (bid_vol + ask_vol)
    
    def get_best_level_depletion(self) -> Tuple[Optional[float], Optional[float]]:
        """
        Detect if best bid/ask sizes are collapsing.
        Returns (bid_depletion_pct, ask_depletion_pct)
        
        Negative = size shrinking (being consumed)
        If ask depleting repeatedly = bullish (buyers lifting)
        If bid depleting repeatedly = bearish (sellers hitting)
        """
        now = time.time() * 1000
        cutoff = now - DEPLETION_WINDOW_MS
        
        # Find oldest sizes in window
        old_bid_size = None
        old_ask_size = None
        
        for ts, size in self.best_bid_size_history:
            if ts >= cutoff:
                old_bid_size = size
                break
        
        for ts, size in self.best_ask_size_history:
            if ts >= cutoff:
                old_ask_size = size
                break
        
        if old_bid_size is None or old_ask_size is None:
            return None, None
        
        if not self.bids or not self.asks:
            return None, None
        
        new_bid_size = self.bids[0][1]
        new_ask_size = self.asks[0][1]
        
        # Calculate % change
        bid_change = ((new_bid_size - old_bid_size) / old_bid_size * 100) if old_bid_size > 0 else 0
        ask_change = ((new_ask_size - old_ask_size) / old_ask_size * 100) if old_ask_size > 0 else 0
        
        return bid_change, ask_change
    
    def get_mid_volatility(self) -> Optional[float]:
        """
        Mid price volatility over last 2 seconds (standard deviation in bps).
        High volatility = dangerous even if spread is wide.
        """
        now = time.time() * 1000
        cutoff = now - VOLATILITY_WINDOW_MS
        
        recent_mids = [mid for ts, mid in self.mid_history if ts >= cutoff]
        
        if len(recent_mids) < 3:
            return None
        
        # Calculate returns in bps
        returns = []
        for i in range(1, len(recent_mids)):
            ret = ((recent_mids[i] - recent_mids[i-1]) / recent_mids[i-1]) * 10000
            returns.append(ret)
        
        if not returns:
            return None
        
        # Standard deviation
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        return math.sqrt(variance)
    
    def get_spread_persistence_ms(self) -> Optional[float]:
        """How long has spread been >= threshold? Returns ms or None."""
        if self.spread_above_threshold_start is None:
            return None
        now = time.time() * 1000
        return now - self.spread_above_threshold_start
    
    def calculate_score(self) -> Tuple[Optional[float], dict]:
        """
        Calculate combined direction score.
        
        Uses: microprice edge, book imbalance, trade flow, best-level depletion
        """
        mid = self.get_mid()
        microprice = self.get_microprice()
        book_imbalance = self.get_book_imbalance(IMBALANCE_LEVELS)
        trade_imbalance = self.get_trade_flow_imbalance()
        bid_depletion, ask_depletion = self.get_best_level_depletion()
        volatility = self.get_mid_volatility()
        
        components = {
            "mid": mid,
            "microprice": microprice,
            "book_imbalance": book_imbalance,
            "trade_imbalance": trade_imbalance,
            "bid_depletion": bid_depletion,
            "ask_depletion": ask_depletion,
            "volatility": volatility,
            "buy_vol_1s": self.buy_volume_1s,
            "sell_vol_1s": self.sell_volume_1s
        }
        
        if None in [mid, microprice, book_imbalance]:
            return None, components
        
        # Microprice edge (normalized to ~[-1, 1])
        mp_edge = ((microprice - mid) / mid) * 10000
        mp_edge_norm = mp_edge / 2.0
        
        # Trade flow contribution
        trade_flow_score = 0
        if trade_imbalance is not None:
            trade_flow_score = trade_imbalance  # Already -1 to 1
        
        # Depletion contribution
        # Ask depleting (negative) = bullish, Bid depleting (negative) = bearish
        depletion_score = 0
        if bid_depletion is not None and ask_depletion is not None:
            # If ask is depleting more than bid, bullish
            depletion_score = (bid_depletion - ask_depletion) / 100  # Normalize
            depletion_score = max(-1, min(1, depletion_score))  # Clamp
        
        # Combined score with weights from ChatGPT recommendation
        score = (
            1.5 * mp_edge_norm +      # Microprice edge
            1.0 * book_imbalance +    # Book imbalance
            1.0 * trade_flow_score +  # Trade flow (important!)
            0.5 * depletion_score     # Level depletion
        )
        
        components["mp_edge"] = mp_edge
        components["score"] = score
        
        return score, components
    
    def get_signal(self, score: float) -> str:
        if abs(score) > T_HARD:
            return "NO_TRADE"
        elif score > T_SOFT:
            return "BULLISH"
        elif score < -T_SOFT:
            return "BEARISH"
        else:
            return "NEUTRAL"
    
    def get_recommendation(self, signal: str, spread_bps: float, volatility: Optional[float]) -> str:
        reasons = []
        
        if spread_bps < 6.0:
            reasons.append("spread too tight")
        
        if volatility and volatility > 5.0:
            reasons.append(f"high volatility ({volatility:.1f} bps)")
        
        if reasons:
            return f"⚠️ WAIT - {', '.join(reasons)}"
        
        if signal == "NO_TRADE":
            return "⛔ NO TRADE - strong adverse selection signal"
        elif signal == "BULLISH":
            return "📈 BUY FIRST - delay/suppress SELL"
        elif signal == "BEARISH":
            return "📉 SELL FIRST - delay/suppress BUY"
        else:
            return "⚖️ BOTH SIDES OK - balanced market"
    
    def print_status(self):
        """Print current status with all signals."""
        mid = self.get_mid()
        microprice = self.get_microprice()
        spread_bps = self.get_spread_bps()
        book_imb = self.get_book_imbalance()
        trade_imb = self.get_trade_flow_imbalance()
        volatility = self.get_mid_volatility()
        persistence = self.get_spread_persistence_ms()
        bid_dep, ask_dep = self.get_best_level_depletion()
        score, components = self.calculate_score()
        
        if mid is None or score is None:
            return
        
        signal = self.get_signal(score)
        self.signal_counts[signal] += 1
        
        recommendation = self.get_recommendation(signal, spread_bps or 0, volatility)
        
        # Color coding
        colors = {
            "BULLISH": "\033[92m",   # Green
            "BEARISH": "\033[91m",   # Red
            "NO_TRADE": "\033[95m",  # Magenta
            "NEUTRAL": "\033[93m"    # Yellow
        }
        color = colors.get(signal, "")
        reset = "\033[0m"
        
        timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        
        print(f"\n{'─'*75}")
        print(f"[{timestamp}] {color}{signal:10}{reset} | Score: {score:+.3f}")
        print(f"{'─'*75}")
        
        # Row 1: Prices
        mp_arrow = "▲" if microprice > mid else "▼" if microprice < mid else "="
        print(f"  Mid: ${mid:.2f} | Microprice: ${microprice:.2f} {mp_arrow} | Spread: {spread_bps:.1f} bps")
        
        # Row 2: Order book
        print(f"  Book Imbalance: {book_imb:+.3f} | ", end="")
        if trade_imb is not None:
            print(f"Trade Flow: {trade_imb:+.3f} ({self.buy_volume_1s:.2f}B / {self.sell_volume_1s:.2f}S)")
        else:
            print("Trade Flow: no data yet")
        
        # Row 3: Volatility & persistence
        vol_str = f"{volatility:.2f} bps" if volatility else "n/a"
        pers_str = f"{persistence:.0f}ms" if persistence else "n/a"
        print(f"  Volatility (2s): {vol_str} | Spread persistence: {pers_str}")
        
        # Row 4: Best level sizes
        if self.bids and self.asks:
            bid_dep_str = f"{bid_dep:+.1f}%" if bid_dep is not None else "n/a"
            ask_dep_str = f"{ask_dep:+.1f}%" if ask_dep is not None else "n/a"
            print(f"  Best Bid: ${self.bids[0][0]:.2f} x {self.bids[0][1]:.3f} ({bid_dep_str}) | "
                  f"Best Ask: ${self.asks[0][0]:.2f} x {self.asks[0][1]:.3f} ({ask_dep_str})")
        
        print(f"\n  → {recommendation}")
    
    def print_stats(self):
        """Print accumulated statistics."""
        print(f"\n{'='*75}")
        print(f"STATISTICS (Book updates: {self.book_updates} | Trade updates: {self.trade_updates})")
        print(f"{'='*75}")
        
        # Signal distribution
        total_signals = sum(self.signal_counts.values())
        if total_signals > 0:
            print(f"\nSignal Distribution:")
            for sig, count in self.signal_counts.items():
                pct = 100 * count / total_signals
                bar = "█" * int(pct / 2)
                print(f"  {sig:10}: {count:4} ({pct:5.1f}%) {bar}")
        
        # Spread distribution
        if self.total_spread_samples > 0:
            print(f"\nSpread Distribution:")
            for bucket, count in self.spread_buckets.items():
                pct = 100 * count / self.total_spread_samples
                bar = "█" * int(pct / 2)
                print(f"  {bucket:8} bps: {count:4} ({pct:5.1f}%) {bar}")
        
        print(f"{'='*75}\n")


async def monitor():
    """Main monitoring loop - subscribes to both L2 book and trades."""
    
    mon = MicrostructureMonitor()
    
    print(f"""
{'='*75}
MICROSTRUCTURE SIGNAL MONITOR v2
{'='*75}
Coin: xyz:{COIN}

Data Feeds:
  ✓ L2 Order Book (bid/ask depth)
  ✓ Trades (buyer/seller initiated volume)

Signals:
  NO_TRADE  (|S| > {T_HARD}) → Don't trade - adverse selection risk
  BULLISH   (S > {T_SOFT})  → Buy first, suppress sell
  BEARISH   (S < -{T_SOFT}) → Sell first, suppress buy
  NEUTRAL   (|S| < {T_SOFT}) → Both sides OK

Additional Checks:
  - Spread >= 6 bps required
  - Volatility < 5 bps (2s window)
  - Spread persistence (is it lasting?)

Press Ctrl+C to stop
{'='*75}
""")
    
    # Subscribe to both L2 book and trades
    subscribe_book = {
        "method": "subscribe",
        "subscription": {"type": "l2Book", "coin": f"xyz:{COIN}"}
    }
    
    subscribe_trades = {
        "method": "subscribe", 
        "subscription": {"type": "trades", "coin": f"xyz:{COIN}"}
    }
    
    last_status_time = 0
    last_stats_time = time.time()
    
    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps(subscribe_book))
        await ws.send(json.dumps(subscribe_trades))
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Connected - subscribed to L2 book + trades\n")
        
        async for msg in ws:
            try:
                data = json.loads(msg)
                channel = data.get("channel", "")
                
                # L2 Book update
                if channel == "l2Book":
                    book_data = data.get("data", {})
                    levels = book_data.get("levels", [[], []])
                    if levels[0] and levels[1]:
                        mon.update_book(levels)
                
                # Trade update
                elif channel == "trades":
                    trades_data = data.get("data", [])
                    for trade in trades_data:
                        mon.update_trade(trade)
                
                # Print status every second
                now = time.time()
                if now - last_status_time >= 1.0:
                    mon.print_status()
                    last_status_time = now
                
                # Print stats every 60 seconds
                if now - last_stats_time >= 60:
                    mon.print_stats()
                    last_stats_time = now
                
            except json.JSONDecodeError:
                continue
            except Exception as e:
                print(f"Error: {e}")
                continue


async def main():
    try:
        await monitor()
    except KeyboardInterrupt:
        print("\n\nStopped by user")


if __name__ == "__main__":
    print("\nStarting Microstructure Monitor v2...")
    print("Press Ctrl+C to stop\n")
    asyncio.run(main())
