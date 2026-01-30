"""
Funding Rate Optimizer

Monitors and optimizes for XYZ HIP-3 hourly funding rates.

Key insight: XYZ funding settles EVERY HOUR (24x/day), not 8-hour like standard perps.
- Positive funding = shorts get paid, longs pay
- Negative funding = longs get paid, shorts pay

Strategy: Maintain a persistent bias (usually short) to collect funding automatically.
No timing tricks needed - just hold the position when funding settles.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from loguru import logger

from xyz_client import XYZClient, FundingRecord, AssetContext
from config import FundingConfig, FundingStrategy, GridBias


@dataclass
class FundingOpportunity:
    """Represents a funding collection opportunity"""
    coin: str
    current_rate: float       # Current hourly funding rate
    avg_rate: float           # Average historical rate  
    rate_pct: float           # Current rate as percentage
    hourly_pct: float         # Hourly rate as percentage
    daily_pct: float          # Daily rate (hourly × 24)
    apr: float                # Annualized rate
    direction: str            # "short" or "long" - which side collects
    consistency: float        # % of periods with same sign
    
    @property
    def is_profitable(self) -> bool:
        """Is this worth optimizing for?"""
        return abs(self.apr) > 5.0  # At least 5% APR


class FundingOptimizer:
    """
    Monitors funding rates and recommends grid bias.
    
    XYZ funding is hourly, so we can collect 24 times per day.
    The optimizer:
    1. Analyzes current and historical funding rates
    2. Recommends a persistent grid bias (short/long/neutral)
    3. Tracks funding income over time
    """
    
    # XYZ funding periods per day (hourly)
    FUNDING_PERIODS_PER_DAY = 24
    
    def __init__(
        self,
        client: XYZClient,
        config: FundingConfig,
        pairs: List[str],
    ):
        """
        Initialize funding optimizer
        
        Args:
            client: XYZ API client
            config: Funding configuration
            pairs: List of pairs to monitor
        """
        self.client = client
        self.config = config
        self.pairs = pairs
        
        # Cache for funding data
        self._funding_cache: Dict[str, FundingOpportunity] = {}
        self._last_refresh: Optional[datetime] = None
        
        # Tracking
        self.total_funding_collected = 0.0
        self.funding_events: List[Dict] = []
        
        logger.info(f"FundingOptimizer initialized for {len(pairs)} pairs")
        logger.info(f"Strategy: {config.strategy.value}, Bias: {config.bias_pct}%")
    
    def get_next_funding_time(self) -> datetime:
        """Get the next funding settlement time (on the hour)"""
        now = datetime.now(timezone.utc)
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return next_hour
    
    def minutes_until_funding(self) -> int:
        """Minutes until next funding settlement"""
        now = datetime.now(timezone.utc)
        next_funding = self.get_next_funding_time()
        delta = next_funding - now
        return int(delta.total_seconds() / 60)
    
    def analyze_funding(self, coin: str, lookback: int = 168) -> Optional[FundingOpportunity]:
        """
        Analyze funding rate for a coin
        
        Args:
            coin: Coin to analyze (e.g., "xyz:SILVER")
            lookback: Number of periods to analyze (168 = 1 week hourly)
            
        Returns:
            FundingOpportunity with analysis
        """
        # Get current funding
        ctx = self.client.get_asset_context(coin)
        if not ctx:
            return None
        
        current_rate = ctx.funding_rate
        
        # Get historical funding
        history = self.client.get_funding_history(coin, limit=lookback)
        if not history:
            # No history, use current rate
            history = []
            avg_rate = current_rate
        else:
            rates = [r.funding_rate for r in history]
            avg_rate = sum(rates) / len(rates)
        
        # Calculate statistics
        if history:
            rates = [r.funding_rate for r in history]
            positive_count = sum(1 for r in rates if r > 0)
            negative_count = sum(1 for r in rates if r < 0)
        else:
            positive_count = 1 if current_rate > 0 else 0
            negative_count = 1 if current_rate < 0 else 0
        
        total_periods = max(len(history), 1)
        
        # Determine which direction collects
        if avg_rate > 0:
            direction = "short"  # Shorts collect positive funding
            consistency = positive_count / total_periods * 100
        else:
            direction = "long"   # Longs collect negative funding
            consistency = negative_count / total_periods * 100
        
        # Calculate rates at different time scales
        hourly_pct = current_rate * 100
        daily_pct = hourly_pct * 24
        apr = avg_rate * self.FUNDING_PERIODS_PER_DAY * 365 * 100
        
        return FundingOpportunity(
            coin=coin,
            current_rate=current_rate,
            avg_rate=avg_rate,
            rate_pct=current_rate * 100,
            hourly_pct=hourly_pct,
            daily_pct=daily_pct,
            apr=apr,
            direction=direction,
            consistency=consistency,
        )
    
    def get_all_opportunities(self) -> List[FundingOpportunity]:
        """Get funding opportunities for all monitored pairs"""
        opportunities = []
        
        for coin in self.pairs:
            opp = self.analyze_funding(coin)
            if opp:
                opportunities.append(opp)
                self._funding_cache[coin] = opp
        
        # Sort by APR (highest first)
        return sorted(opportunities, key=lambda x: abs(x.apr), reverse=True)
    
    def get_recommended_bias(self, coin: str) -> GridBias:
        """
        Get recommended grid bias for a coin based on funding strategy
        
        Args:
            coin: Coin to get bias for
            
        Returns:
            GridBias recommendation
        """
        strategy = self.config.strategy
        
        # Simple strategies - return configured bias
        if strategy == FundingStrategy.IGNORE:
            return GridBias.NEUTRAL
        elif strategy == FundingStrategy.SHORT_BIAS:
            return GridBias.SHORT
        elif strategy == FundingStrategy.LONG_BIAS:
            return GridBias.LONG
        
        # Dynamic strategy - analyze funding and decide
        elif strategy == FundingStrategy.DYNAMIC:
            opp = self.analyze_funding(coin)
            if not opp:
                return GridBias.NEUTRAL
            
            # Check minimum rate threshold
            if abs(opp.current_rate) < self.config.min_rate_threshold:
                return GridBias.NEUTRAL
            
            # Follow the funding direction
            if opp.direction == "short":
                return GridBias.SHORT
            else:
                return GridBias.LONG
        
        return GridBias.NEUTRAL
    
    def get_bias_percentage(self) -> float:
        """Get the configured bias percentage for grid level distribution"""
        return self.config.bias_pct / 100
    
    def estimate_funding_income(
        self,
        coin: str,
        position_size_usd: float,
        hours: int = 24,
    ) -> Tuple[float, float]:
        """
        Estimate funding income for a position
        
        Args:
            coin: Coin symbol
            position_size_usd: Position size in USD (positive = short, negative = long)
            hours: Number of hours to estimate
            
        Returns:
            Tuple of (estimated_income_usd, annualized_rate_pct)
        """
        opp = self.analyze_funding(coin)
        if not opp:
            return 0.0, 0.0
        
        # Funding = position × rate × periods
        # Shorts collect positive funding, longs collect negative
        is_short = position_size_usd > 0
        abs_size = abs(position_size_usd)
        
        if is_short:
            # Short position: earn when funding positive, pay when negative
            income = abs_size * opp.avg_rate * hours
        else:
            # Long position: earn when funding negative, pay when positive  
            income = -abs_size * opp.avg_rate * hours
        
        # Annualize
        apr = (income / abs_size) * (365 * 24 / hours) * 100 if abs_size > 0 else 0
        
        return income, apr
    
    def record_funding_event(
        self,
        coin: str,
        rate: float,
        position: float,
        pnl: float,
    ):
        """Record a funding event for tracking"""
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "coin": coin,
            "rate": rate,
            "position": position,
            "pnl": pnl,
        }
        self.funding_events.append(event)
        self.total_funding_collected += pnl
        
        logger.info(
            f"Funding collected: {coin} rate={rate*100:.4f}% "
            f"pos={position:.2f} pnl=${pnl:.4f}"
        )
    
    def get_funding_summary(self) -> Dict:
        """Get funding collection summary"""
        return {
            "total_collected": self.total_funding_collected,
            "events_count": len(self.funding_events),
            "strategy": self.config.strategy.value,
            "bias_pct": self.config.bias_pct,
            "minutes_until_next": self.minutes_until_funding(),
        }
    
    def print_opportunities(self):
        """Print current funding opportunities"""
        opportunities = self.get_all_opportunities()
        
        print(f"\n{'='*80}")
        print(f"Funding Rates | Next settlement in {self.minutes_until_funding()} min | Strategy: {self.config.strategy.value}")
        print(f"{'='*80}")
        print(f"{'Coin':<15} {'Hourly':<12} {'Daily':<12} {'APR':<12} {'Collect':<10} {'Consistency':<12}")
        print(f"{'-'*80}")
        
        for opp in opportunities:
            collect = opp.direction.upper()
            print(
                f"{opp.coin:<15} {opp.hourly_pct:>+10.4f}% {opp.daily_pct:>+10.2f}% "
                f"{opp.apr:>+10.1f}% {collect:<10} {opp.consistency:>10.1f}%"
            )
        
        if not opportunities:
            print("No funding data available")
        
        print(f"{'='*80}")
        print(f"Total funding collected: ${self.total_funding_collected:.2f}")
        print(f"Tip: Positive APR with 'SHORT' = go short to collect")
        print()
