"""
Quick diagnostic to dump full HyperLiquid API response for debugging position detection
"""
import sys
import json
import os
from pathlib import Path
from dotenv import load_dotenv

# Load credentials from .env
env_file = Path(__file__).parent.parent / "grid-bot" / ".env.hyperliquid"
if env_file.exists():
    load_dotenv(env_file)

WALLET_ADDRESS = os.environ.get("HL_WALLET_ADDRESS", "")
PRIVATE_KEY = os.environ.get("HL_PRIVATE_KEY", "")
ACCOUNT_ADDRESS = os.environ.get("HL_ACCOUNT_ADDRESS") or WALLET_ADDRESS

# Add grid-bot to path for XYZ client import
sys.path.insert(0, str(Path(__file__).parent.parent / "grid-bot"))

from xyz_client import XYZClient

def main():
    print("=" * 80)
    print("HYPERLIQUID POSITION DEBUG")
    print("=" * 80)

    client = XYZClient(
        wallet_address=WALLET_ADDRESS,
        private_key=PRIVATE_KEY,
        account_address=ACCOUNT_ADDRESS,
        testnet=False,
    )

    print(f"\nWallet (signer): {WALLET_ADDRESS}")
    print(f"Account (query): {ACCOUNT_ADDRESS}")
    print()

    # Get full user state
    user_state = client._info.user_state(ACCOUNT_ADDRESS)

    print("TOP-LEVEL KEYS:")
    print(json.dumps(list(user_state.keys()), indent=2))
    print()

    print("FULL USER STATE:")
    print(json.dumps(user_state, indent=2, default=str))
    print()

    # Specifically check assetPositions
    asset_positions = user_state.get("assetPositions", [])
    print(f"assetPositions count: {len(asset_positions)}")

    if asset_positions:
        print("\nPOSITIONS FOUND:")
        for i, pos_data in enumerate(asset_positions):
            pos = pos_data.get("position", {})
            print(f"\n  Position {i}:")
            print(f"    Coin: {pos.get('coin')}")
            print(f"    Size: {pos.get('szi')}")
            print(f"    Entry: {pos.get('entryPx')}")
            print(f"    Mark: {pos.get('markPx')}")
            print(f"    PnL: {pos.get('unrealizedPnl')}")
    else:
        print("\nNO POSITIONS IN assetPositions")

if __name__ == "__main__":
    main()
