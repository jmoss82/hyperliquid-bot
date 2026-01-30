"""
Grid Bot Configuration Settings

XYZ HIP-3 Neutral Grid Bot with Funding Rate Optimization
"""
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum
from dotenv import load_dotenv

# ==================== LOAD ENVIRONMENT VARIABLES ====================
# Load from .env.hyperliquid file if it exists
env_file = Path(__file__).parent.parent / ".env.hyperliquid"
if env_file.exists():
    load_dotenv(env_file)
    print(f"Loaded environment from: {env_file}")

# ==================== WALLET CONFIGURATION ====================
# Load from environment variables for security
# Set these in your shell or .env file:
#   export HL_WALLET_ADDRESS="0x..."
#   export HL_PRIVATE_KEY="0x..."

WALLET_ADDRESS = os.environ.get("HL_WALLET_ADDRESS", "")
PRIVATE_KEY = os.environ.get("HL_PRIVATE_KEY", "")

# Validate wallet is configured (only required for live trading)
def validate_wallet_config():
    """Check if wallet is properly configured for live trading"""
    if not WALLET_ADDRESS:
        raise ValueError("HL_WALLET_ADDRESS environment variable not set")
    if not PRIVATE_KEY:
        raise ValueError("HL_PRIVATE_KEY environment variable not set")
    if not WALLET_ADDRESS.startswith("0x") or len(WALLET_ADDRESS) != 42:
        raise ValueError(f"Invalid wallet address format: {WALLET_ADDRESS}")
    if not PRIVATE_KEY.startswith("0x") or len(PRIVATE_KEY) != 66:
        raise ValueError("Invalid private key format (should be 0x + 64 hex chars)")


class GridBias(Enum):
    """Grid position bias"""
    NEUTRAL = "neutral"      # Equal long/short exposure
    LONG = "long"            # Favor long positions
    SHORT = "short"          # Favor short positions
    DYNAMIC = "dynamic"      # Adjust based on funding rates


class FundingStrategy(Enum):
    """How to handle funding rate optimization"""
    IGNORE = "ignore"        # Run neutral grid, don't optimize for funding
    SHORT_BIAS = "short_bias"  # Maintain net short exposure to collect positive funding
    LONG_BIAS = "long_bias"    # Maintain net long exposure (rare - for negative funding)
    DYNAMIC = "dynamic"        # Adjust bias based on current funding rate direction


class GridSpacingMode(Enum):
    """How grid spacing is calculated"""
    PERCENT = "percent"        # Geometric spacing by percentage
    ARITHMETIC = "arithmetic"  # Fixed price spacing across a range
    SIMPLE_GRID = "simple_grid"  # BYDFi-style: All levels active, simple counter-orders


@dataclass
class GridParameters:
    """Grid configuration parameters"""
    # Grid structure
    # XYZ100 has ~10% daily volatility, so ±12% range (24% total) covers ~2.5 days
    # With 0.25% spacing, that's 48 levels each side (96 total)
    num_levels: int = 48              # Percent mode: levels per side; arithmetic: total grids if total_grids unset
    grid_spacing_pct: float = 0.25    # Space between levels (%) - tight for HIP-3 low fees
    grid_spacing_mode: GridSpacingMode = GridSpacingMode.PERCENT
    # Arithmetic grid options (used when grid_spacing_mode=ARITHMETIC)
    price_range_low: Optional[float] = None
    price_range_high: Optional[float] = None
    total_grids: Optional[int] = None  # Total grid intervals (both sides combined)
    
    # Position sizing
    total_capital_usd: float = 100    # Total capital to deploy (start small!)
    leverage: float = 5.0             # Leverage multiplier (conservative)
    size_per_level_pct: float = 2.0   # % of notional per grid level (48 * 2% = 96%)
    
    # Risk management
    max_position_pct: float = 50.0    # Max position as % of total notional
    stop_loss_pct: float = 15.0       # Stop loss trigger (%) - scaled for leverage
    max_drawdown_pct: float = 20.0    # Max drawdown before pause
    margin_buffer_pct: float = 30.0   # Keep 30% margin buffer to avoid liquidation
    
    # Grid behavior
    bias: GridBias = GridBias.NEUTRAL
    rebalance_threshold_pct: float = 10.0  # Rebalance when drift exceeds this (was 2%)


@dataclass
class FundingConfig:
    """Funding rate optimization settings"""
    strategy: FundingStrategy = FundingStrategy.SHORT_BIAS
    
    # Funding parameters
    funding_period_hours: int = 1     # XYZ funding is hourly (24x per day)
    
    # Position bias - how much to skew the grid
    # e.g., 20% bias means: if grid has 10 levels each side,
    # we run 12 short levels and 8 long levels
    bias_pct: float = 20.0
    
    # Minimum rate to bother optimizing (below this, run neutral)
    min_rate_threshold: float = 0.0001  # 0.01% hourly = ~87% APR


@dataclass
class TradingPair:
    """XYZ trading pair configuration"""
    name: str                         # e.g., "xyz:SILVER"
    index_in_meta: int                # Position in XYZ universe
    asset_id: Optional[int] = None    # Numeric ID for orders (TBD)

    # Pair-specific overrides
    max_leverage: int = 20
    size_decimals: int = 2
    price_decimals: int = 0           # Price decimals (0 = whole dollars)
    min_order_usd: float = 10.0

    # Risk parameters
    volatility_factor: float = 1.0    # Adjust grid width for volatility

    @property
    def tick_size(self) -> float:
        """Calculate tick size from price decimals"""
        return 10 ** (-self.price_decimals) if self.price_decimals > 0 else 1.0


# ==================== XYZ HIP-3 ASSET IDS ====================
# Formula: asset_id = 100000 + (perp_dex_index * 10000) + index_in_meta
# XYZ perp_dex_index = 1, so base = 110000

XYZ_DEX_INDEX = 1
XYZ_ASSET_BASE = 110000  # 100000 + (1 * 10000)

XYZ_ASSET_IDS = {
    "xyz:XYZ100": 110000,
    "xyz:TSLA": 110001,
    "xyz:NVDA": 110002,
    "xyz:GOLD": 110003,
    "xyz:HOOD": 110004,
    "xyz:INTC": 110005,
    "xyz:PLTR": 110006,
    "xyz:COIN": 110007,
    "xyz:META": 110008,
    "xyz:AAPL": 110009,
    "xyz:MSFT": 110010,
    "xyz:ORCL": 110011,
    "xyz:GOOGL": 110012,
    "xyz:AMZN": 110013,
    "xyz:AMD": 110014,
    "xyz:MU": 110015,
    "xyz:SNDK": 110016,
    "xyz:MSTR": 110017,
    "xyz:CRCL": 110018,
    "xyz:NFLX": 110019,
    "xyz:COST": 110020,
    "xyz:LLY": 110021,
    "xyz:SKHX": 110022,
    "xyz:TSM": 110023,
    "xyz:JPY": 110024,
    "xyz:EUR": 110025,
    "xyz:SILVER": 110026,
    "xyz:RIVN": 110027,
    "xyz:BABA": 110028,
    "xyz:CL": 110029,
    "xyz:COPPER": 110030,
    "xyz:NATGAS": 110031,
    "xyz:URANIUM": 110032,
    "xyz:ALUMINIUM": 110033,
}


# ==================== XYZ PAIR DEFINITIONS ====================
# Based on discovered metadata with asset IDs

XYZ_PAIRS = {
    "xyz:XYZ100": TradingPair(name="xyz:XYZ100", index_in_meta=0, asset_id=110000, max_leverage=25, size_decimals=4, price_decimals=0),
    "xyz:TSLA": TradingPair(name="xyz:TSLA", index_in_meta=1, asset_id=110001, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:NVDA": TradingPair(name="xyz:NVDA", index_in_meta=2, asset_id=110002, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:GOLD": TradingPair(name="xyz:GOLD", index_in_meta=3, asset_id=110003, max_leverage=20, size_decimals=4, price_decimals=1),
    "xyz:HOOD": TradingPair(name="xyz:HOOD", index_in_meta=4, asset_id=110004, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:INTC": TradingPair(name="xyz:INTC", index_in_meta=5, asset_id=110005, max_leverage=10, size_decimals=2, price_decimals=2),
    "xyz:PLTR": TradingPair(name="xyz:PLTR", index_in_meta=6, asset_id=110006, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:COIN": TradingPair(name="xyz:COIN", index_in_meta=7, asset_id=110007, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:META": TradingPair(name="xyz:META", index_in_meta=8, asset_id=110008, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:AAPL": TradingPair(name="xyz:AAPL", index_in_meta=9, asset_id=110009, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:MSFT": TradingPair(name="xyz:MSFT", index_in_meta=10, asset_id=110010, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:ORCL": TradingPair(name="xyz:ORCL", index_in_meta=11, asset_id=110011, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:GOOGL": TradingPair(name="xyz:GOOGL", index_in_meta=12, asset_id=110012, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:AMZN": TradingPair(name="xyz:AMZN", index_in_meta=13, asset_id=110013, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:AMD": TradingPair(name="xyz:AMD", index_in_meta=14, asset_id=110014, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:MU": TradingPair(name="xyz:MU", index_in_meta=15, asset_id=110015, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:SNDK": TradingPair(name="xyz:SNDK", index_in_meta=16, asset_id=110016, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:MSTR": TradingPair(name="xyz:MSTR", index_in_meta=17, asset_id=110017, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:CRCL": TradingPair(name="xyz:CRCL", index_in_meta=18, asset_id=110018, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:NFLX": TradingPair(name="xyz:NFLX", index_in_meta=19, asset_id=110019, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:COST": TradingPair(name="xyz:COST", index_in_meta=20, asset_id=110020, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:LLY": TradingPair(name="xyz:LLY", index_in_meta=21, asset_id=110021, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:SKHX": TradingPair(name="xyz:SKHX", index_in_meta=22, asset_id=110022, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:TSM": TradingPair(name="xyz:TSM", index_in_meta=23, asset_id=110023, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:JPY": TradingPair(name="xyz:JPY", index_in_meta=24, asset_id=110024, max_leverage=50, size_decimals=2, price_decimals=2),
    "xyz:EUR": TradingPair(name="xyz:EUR", index_in_meta=25, asset_id=110025, max_leverage=50, size_decimals=1, price_decimals=4),
    "xyz:SILVER": TradingPair(name="xyz:SILVER", index_in_meta=26, asset_id=110026, max_leverage=20, size_decimals=2, price_decimals=2),
    "xyz:RIVN": TradingPair(name="xyz:RIVN", index_in_meta=27, asset_id=110027, max_leverage=10, size_decimals=2, price_decimals=2),
    "xyz:BABA": TradingPair(name="xyz:BABA", index_in_meta=28, asset_id=110028, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:CL": TradingPair(name="xyz:CL", index_in_meta=29, asset_id=110029, max_leverage=20, size_decimals=3, price_decimals=2),  # Crude Oil
    "xyz:COPPER": TradingPair(name="xyz:COPPER", index_in_meta=30, asset_id=110030, max_leverage=20, size_decimals=2, price_decimals=4),
    "xyz:NATGAS": TradingPair(name="xyz:NATGAS", index_in_meta=31, asset_id=110031, max_leverage=10, size_decimals=1, price_decimals=3),
    "xyz:URANIUM": TradingPair(name="xyz:URANIUM", index_in_meta=32, asset_id=110032, max_leverage=10, size_decimals=3, price_decimals=2),
    "xyz:ALUMINIUM": TradingPair(name="xyz:ALUMINIUM", index_in_meta=33, asset_id=110033, max_leverage=20, size_decimals=4, price_decimals=1),
}


@dataclass
class BotConfig:
    """Complete bot configuration"""
    # Trading pairs to run grids on (top volume pairs)
    # XYZ100 (25x), SILVER (20x), GOLD (20x), INTC (10x), TSLA (10x), NVDA (10x)
    pairs: List[str] = field(default_factory=lambda: [
        "xyz:XYZ100",   # 25x max leverage
        "xyz:SILVER",   # 20x max leverage  
        "xyz:GOLD",     # 20x max leverage
        "xyz:INTC",     # 10x max leverage
        "xyz:TSLA",     # 10x max leverage
        "xyz:NVDA",     # 10x max leverage
    ])
    
    # Grid parameters
    grid: GridParameters = field(default_factory=GridParameters)
    
    # Funding optimization
    funding: FundingConfig = field(default_factory=FundingConfig)
    
    # API settings
    api_base_url: str = "https://api.hyperliquid.xyz"
    
    # Execution settings
    order_refresh_seconds: int = 30   # How often to refresh grid orders
    data_refresh_seconds: int = 5     # How often to refresh market data
    
    # Logging
    log_level: str = "INFO"
    log_file: str = "grid_bot.log"
    
    # Safety
    dry_run: bool = True              # If True, don't place real orders
    max_orders_per_minute: int = 30   # Rate limiting


# ==================== DEFAULT CONFIGURATION ====================
#
# XYZ100 ARITHMETIC GRID (BYDFi-style)
#
# ⚠️  CRITICAL: ALWAYS UPDATE PRICE RANGE BEFORE RUNNING! ⚠️
#
# This configuration was set for: XYZ100 @ $25,500 (Jan 24, 2026)
# Check current price at: https://app.hyperliquid.xyz/trade/xyz:XYZ100
#
# To update for current price:
#   1. Get current XYZ100 price (e.g., $30,000)
#   2. Calculate: low = current_price * 0.88 (12% below)
#   3. Calculate: high = current_price * 1.12 (12% above)
#   4. Update price_range_low and price_range_high below
#
# Example: If XYZ100 is at $30,000:
#   - price_range_low  = 26,400  ($30,000 * 0.88)
#   - price_range_high = 33,600  ($30,000 * 1.12)
#
# The bot will ERROR if current price is >5% outside the range!
#

DEFAULT_CONFIG = BotConfig(
    pairs=[
        "xyz:XYZ100",   # 25x max leverage - BEST FOR GRID (300M volume, 10% daily swings)
        # Add more pairs after testing:
        # "xyz:SILVER",   # 20x max leverage, 10% daily swings
        # "xyz:GOLD",     # 20x max leverage, 5% daily swings
    ],
    grid=GridParameters(
        # ===== SIMPLE GRID MODE (BYDFi-style) =====
        # All levels always active, simple counter-orders at price ± grid_step
        grid_spacing_mode=GridSpacingMode.SIMPLE_GRID,

        # Price range - Updated for current XYZ100 price
        # Current XYZ100 price: ~$25,493 (as of Jan 25, 2026)
        # Formula: low = current_price * 0.88, high = current_price * 1.12
        price_range_low=22434.0,    # ~12% below center ($25,493 * 0.88)
        price_range_high=28552.0,   # ~12% above center ($25,493 * 1.12)

        # Grid count - 4 total intervals = 2 buys + 2 sells
        # Conservative for $25 account testing
        # With $25 capital, 2x leverage, 4 orders = $12.50 per order
        total_grids=4,

        # ===== POSITION SIZING =====
        total_capital_usd=25,       # Current account balance
        leverage=2.0,               # Ultra-conservative 2x for first test
        size_per_level_pct=50.0,    # Not used in SIMPLE_GRID (kept for other modes)

        # ===== RISK MANAGEMENT =====
        max_position_pct=50.0,      # Max 50% of notional on one side
        bias=GridBias.NEUTRAL,      # True neutral grid - profit both ways

        # Percent mode settings (not used in arithmetic mode)
        num_levels=48,
        grid_spacing_pct=0.25,
        rebalance_threshold_pct=10.0,
    ),
    funding=FundingConfig(
        strategy=FundingStrategy.IGNORE,  # Focus on grid profits, not funding
        funding_period_hours=1,
        bias_pct=0.0,  # No bias - true neutral
    ),
    dry_run=False,  # LIVE TRADING - REAL ORDERS
)


# ==================== FEE STRUCTURE ====================

class Fees:
    """XYZ HIP-3 fee structure"""
    MAKER_FEE = 0.00003     # 0.003%
    TAKER_FEE = 0.00009     # 0.009%
    
    # For grid bots, we use 100% limit orders = maker fees
    ROUND_TRIP_FEE = MAKER_FEE * 2  # 0.006%
