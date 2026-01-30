"""Grid Bot Configuration"""
from .settings import (
    # Wallet config
    WALLET_ADDRESS,
    PRIVATE_KEY,
    validate_wallet_config,
    # Bot config
    BotConfig,
    GridParameters,
    FundingConfig,
    TradingPair,
    GridBias,
    FundingStrategy,
    GridSpacingMode,
    Fees,
    XYZ_PAIRS,
    XYZ_ASSET_IDS,
    XYZ_DEX_INDEX,
    XYZ_ASSET_BASE,
    DEFAULT_CONFIG,
)

__all__ = [
    # Wallet
    "WALLET_ADDRESS",
    "PRIVATE_KEY",
    "validate_wallet_config",
    # Bot config
    "BotConfig",
    "GridParameters", 
    "FundingConfig",
    "TradingPair",
    "GridBias",
    "FundingStrategy",
    "GridSpacingMode",
    "Fees",
    "XYZ_PAIRS",
    "XYZ_ASSET_IDS",
    "XYZ_DEX_INDEX",
    "XYZ_ASSET_BASE",
    "DEFAULT_CONFIG",
]
