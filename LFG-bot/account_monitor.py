"""
Periodic account monitor for HyperLiquid (XYZ HIP-3).

Usage examples:
  python account_monitor.py
  python account_monitor.py --coin xyz:SILVER --interval 15
  python account_monitor.py --all-positions --interval 30
"""
import argparse
import time
from datetime import datetime, timezone
from pathlib import Path
import sys

# Import local config (renamed to avoid conflict with grid-bot/config package)
from lfg_config import WALLET_ADDRESS, PRIVATE_KEY, ACCOUNT_ADDRESS, validate_credentials

# Add grid-bot to path for XYZ client import
sys.path.insert(0, str(Path(__file__).parent.parent / "grid-bot"))

from xyz_client import XYZClient


def get_all_positions(client: XYZClient):
    """Return non-zero positions from user_state."""
    positions = []
    if not client._info:
        return positions

    try:
        # Check account address with xyz dex for HIP-3 positions
        print(f"[DEBUG] Querying account_address: {client.account_address} (dex=xyz)", flush=True)
        user_state = client._info.user_state(client.account_address, dex=client.DEX_NAME)

        # Dump all top-level keys
        print(f"[DEBUG] user_state keys: {list(user_state.keys())}", flush=True)

        # Check assetPositions
        asset_positions = user_state.get("assetPositions", [])
        print(f"[DEBUG] Account address - assetPositions length: {len(asset_positions)}", flush=True)

        # Dump full assetPositions structure if not empty
        if asset_positions:
            import json
            print(f"[DEBUG] Full assetPositions: {json.dumps(asset_positions, indent=2)}", flush=True)
        else:
            # If empty, check marginSummary for clues
            margin_summary = user_state.get("marginSummary", {})
            print(f"[DEBUG] marginSummary: {margin_summary}", flush=True)

        # Also check wallet address with xyz dex
        print(f"[DEBUG] Querying wallet_address: {client.wallet_address} (dex=xyz)", flush=True)
        wallet_state = client._info.user_state(client.wallet_address, dex=client.DEX_NAME)
        wallet_positions = wallet_state.get("assetPositions", [])
        print(f"[DEBUG] Wallet address - assetPositions length: {len(wallet_positions)}", flush=True)

        # Use whichever address has positions
        if len(asset_positions) == 0 and len(wallet_positions) > 0:
            print(f"[DEBUG] Using wallet_address positions (account had none)", flush=True)
            asset_positions = wallet_positions

        for i, pos_data in enumerate(asset_positions):
            pos = pos_data.get("position", {})
            coin = pos.get("coin", "")
            size = float(pos.get("szi", 0))

            print(f"[DEBUG] Position {i}: coin={coin}, size={size}", flush=True)

            if size == 0:
                continue
            positions.append({
                "coin": coin,
                "size": size,
                "entry": float(pos.get("entryPx", 0)) if pos.get("entryPx") else 0.0,
                "mark": float(pos.get("markPx", 0)) if pos.get("markPx") else 0.0,
                "pnl": float(pos.get("unrealizedPnl", 0)),
                "liq": float(pos.get("liquidationPx", 0)) if pos.get("liquidationPx") else None,
            })
    except Exception as e:
        print(f"[ERROR] Failed to read user_state positions: {e}", flush=True)

    return positions


def print_open_orders(orders):
    print(f"Open orders: {len(orders)}", flush=True)
    for order in orders:
        print(
            f"  - {order.side.value.upper()} {order.size} {order.coin} @ {order.price} "
            f"(id={order.order_id})",
            flush=True,
        )


def print_position(pos):
    if not pos or pos.size == 0:
        print("Position: none", flush=True)
        return

    print(
        f"Position: {pos.coin} size={pos.size} entry={pos.entry_price} "
        f"mark={pos.mark_price} pnl={pos.unrealized_pnl} liq={pos.liquidation_price}",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description="HyperLiquid account monitor")
    parser.add_argument("--coin", default="xyz:SILVER", help="Coin to monitor")
    parser.add_argument("--interval", type=int, default=15, help="Polling interval (seconds)")
    parser.add_argument("--all-positions", action="store_true", help="Show all non-zero positions")
    args = parser.parse_args()

    try:
        validate_credentials()
    except Exception as e:
        print(f"[ERROR] {e}", flush=True)
        return

    client = XYZClient(
        wallet_address=WALLET_ADDRESS,
        private_key=PRIVATE_KEY,
        account_address=ACCOUNT_ADDRESS,
        testnet=False,
    )

    print("=" * 80, flush=True)
    print("ACCOUNT MONITOR STARTED", flush=True)
    print(f"Coin: {args.coin} | Interval: {args.interval}s | All positions: {args.all_positions}", flush=True)
    if WALLET_ADDRESS:
        print(f"Wallet (signer): ...{WALLET_ADDRESS[-4:]}", flush=True)
    if ACCOUNT_ADDRESS:
        print(f"Account (query): ...{ACCOUNT_ADDRESS[-4:]}", flush=True)
    print("=" * 80, flush=True)

    try:
        while True:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            balance = client.get_account_balance()
            print(f"\n[{now}] Balance: ${balance:.2f}", flush=True)

            orders = client.get_open_orders()
            print_open_orders(orders)

            if args.all_positions:
                positions = get_all_positions(client)
                if not positions:
                    print("Positions: none", flush=True)
                else:
                    print("Positions:", flush=True)
                    for pos in positions:
                        print(
                            f"  - {pos['coin']} size={pos['size']} entry={pos['entry']} "
                            f"mark={pos['mark']} pnl={pos['pnl']} liq={pos['liq']}",
                            flush=True,
                        )
            else:
                pos = client.get_position(args.coin)
                print_position(pos)

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[STOPPED] Monitor interrupted", flush=True)


if __name__ == "__main__":
    main()
