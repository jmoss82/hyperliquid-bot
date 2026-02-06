"""
LFG Bot Configuration

Loads HyperLiquid credentials from .env file
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load from .env file(s)
env_candidates = [
    Path(__file__).parent / ".env.hyperliquid",
    Path(__file__).parent.parent / "grid-bot" / ".env.hyperliquid",
    Path(__file__).parent / ".env",
    Path(__file__).parent.parent / ".env",
]

LOADED_ENV_FILE = None
for env_file in env_candidates:
    if env_file.exists():
        load_dotenv(env_file)
        LOADED_ENV_FILE = env_file
        break

# Wallet configuration
WALLET_ADDRESS = os.environ.get("HL_WALLET_ADDRESS", "")
PRIVATE_KEY = os.environ.get("HL_PRIVATE_KEY", "")
ACCOUNT_ADDRESS = (
    os.environ.get("HL_ACCOUNT_ADDRESS")
    or os.environ.get("HL_MASTER_ADDRESS")
    or os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS")
    or WALLET_ADDRESS
)

# Trading defaults
DEFAULT_SYMBOL = "ETH/USDC:USDC"
DEFAULT_LEVERAGE = 1
DEFAULT_MARGIN_MODE = "isolated"  # or "cross"

# Execution parameters
EXECUTION_CONFIG = {
    "patience_ms": 300,          # Wait between reprices (ms)
    "max_duration": 30.0,        # Max chase time (seconds)
}

# Signal parameters
SIGNAL_CONFIG = {
    "min_trade_size": 1000,      # Filter trades < $1,000
    "large_order_threshold": 10000,  # Whale = >$10k
    "imbalance_threshold": 0.30,  # 30% imbalance for signal
    "window_seconds": 15,         # Analysis window
}

# Risk management
RISK_CONFIG = {
    "max_position_pct": 5.0,     # Max 5% of balance per trade
    "default_tp_pct": 1.0,       # 1% take profit
    "default_sl_pct": 0.5,       # 0.5% stop loss
    "max_daily_trades": 20,      # Rate limit
    "max_daily_loss_pct": 3.0,   # Stop if down 3%
}


def validate_credentials():
    """Verify credentials are loaded"""
    if not WALLET_ADDRESS or not PRIVATE_KEY:
        raise ValueError(
            "Missing credentials. Set HL_WALLET_ADDRESS and HL_PRIVATE_KEY in .env file"
        )

    if not WALLET_ADDRESS.startswith("0x"):
        raise ValueError("HL_WALLET_ADDRESS must start with '0x'")

    if not PRIVATE_KEY.startswith("0x"):
        raise ValueError("HL_PRIVATE_KEY must start with '0x'")

    if ACCOUNT_ADDRESS and not ACCOUNT_ADDRESS.startswith("0x"):
        raise ValueError("HL_ACCOUNT_ADDRESS must start with '0x'")

    if LOADED_ENV_FILE:
        print(f"[ENV] Loaded: {LOADED_ENV_FILE}")
    print("[OK] Credentials loaded successfully")
    return True


def print_config():
    """Print current configuration"""
    print("="*60)
    print("LFG BOT CONFIGURATION")
    print("="*60)
    print(f"\nDefault Symbol: {DEFAULT_SYMBOL}")
    print(f"Leverage: {DEFAULT_LEVERAGE}x")
    print(f"Margin Mode: {DEFAULT_MARGIN_MODE}")
    
    print(f"\nExecution:")
    for k, v in EXECUTION_CONFIG.items():
        print(f"  {k}: {v}")
    
    print(f"\nSignals:")
    for k, v in SIGNAL_CONFIG.items():
        print(f"  {k}: {v}")
    
    print(f"\nRisk:")
    for k, v in RISK_CONFIG.items():
        print(f"  {k}: {v}")
    
    print("="*60)


if __name__ == "__main__":
    print_config()
    try:
        validate_credentials()
    except ValueError as e:
        print(f"\n[WARNING] {e}")
