"""
Raw HTTP request to HyperLiquid API to bypass SDK
"""
import requests
import json
import os
from pathlib import Path
from dotenv import load_dotenv

# Load credentials
env_file = Path(__file__).parent.parent / "grid-bot" / ".env.hyperliquid"
if env_file.exists():
    load_dotenv(env_file)

WALLET_ADDRESS = os.environ.get("HL_WALLET_ADDRESS", "")
ACCOUNT_ADDRESS = os.environ.get("HL_ACCOUNT_ADDRESS") or WALLET_ADDRESS

print("=" * 80)
print("RAW HYPERLIQUID API CHECK")
print("=" * 80)
print(f"\nWallet: {WALLET_ADDRESS}")
print(f"Account: {ACCOUNT_ADDRESS}\n")

# Try both addresses
for addr_name, addr in [("ACCOUNT", ACCOUNT_ADDRESS), ("WALLET", WALLET_ADDRESS)]:
    print(f"\n{'='*60}")
    print(f"Querying {addr_name} address: {addr}")
    print(f"{'='*60}")

    # Raw API request - try both default dex and xyz dex
    url = "https://api.hyperliquid.xyz/info"

    # Try default dex first
    payload = {
        "type": "clearinghouseState",
        "user": addr
    }

    print(f"\n  >> Querying DEFAULT dex")
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
        positions = data.get("assetPositions", [])
        print(f"     assetPositions: {len(positions)}")
    except Exception as e:
        print(f"     ERROR: {e}")

    # Try xyz dex for HIP-3
    payload_xyz = {
        "type": "clearinghouseState",
        "user": addr,
        "dex": "xyz"
    }

    print(f"  >> Querying XYZ dex (HIP-3)")
    payload = payload_xyz

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()

        print(f"\nResponse keys: {list(data.keys())}")

        # Check assetPositions
        asset_positions = data.get("assetPositions", [])
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
                print(f"    Margin: {pos.get('marginUsed')}")
                print(f"    Leverage: {pos.get('leverage')}")
        else:
            print("\nNO POSITIONS")

        # Check margin summary
        margin = data.get("marginSummary", {})
        print(f"\nMargin Summary:")
        print(f"  Account Value: {margin.get('accountValue')}")
        print(f"  Total Margin Used: {margin.get('totalMarginUsed')}")
        print(f"  Total Notional: {margin.get('totalNtlPos')}")

    except Exception as e:
        print(f"\nERROR: {e}")

print("\n" + "="*80)
