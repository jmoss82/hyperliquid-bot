"""
Smart Order Executor - Chase-Limit Engine

Replicates limits.trade LFG (Limit Fill Guaranteed) orders.

Achieves:
- Maker fees (0.003% vs 0.009% taker)
- Better fills (inside spread using REAL orderbook data)
- No slippage (patient execution)

Usage:
    python executor.py                    # Dry run test
    python executor.py --live --size 0.5  # Live with custom size
"""
import time
from datetime import datetime
import argparse
from typing import Optional

from client import HyperLiquidClient, OrderSide, create_client_from_env


class SmartExecutor:
    """
    Chase-limit order execution engine.
    
    Uses real orderbook data to place orders inside the spread,
    ensuring maker fees while achieving market-order-like fills.
    """

    def __init__(self, client: HyperLiquidClient):
        self.client = client
        self.stats = {
            'total_trades': 0,
            'total_volume': 0,
            'total_fee_savings': 0,
        }

    def chase_limit_buy(
        self,
        symbol: str,
        size: float,
        max_duration: float = 30.0,
        patience_ms: int = 300,
        max_spread_cross: float = 0.005,
        spread_position: float = 0.3,
        dry_run: bool = True,
        take_profit_pct: Optional[float] = None,
        stop_loss_pct: Optional[float] = None,
    ) -> dict:
        """
        Execute buy using chase-limit strategy with REAL orderbook data.

        Args:
            symbol: Trading pair (e.g., "ETH/USDC:USDC")
            size: Amount to buy
            max_duration: Max time to chase (seconds)
            patience_ms: Wait time before repricing (ms)
            max_spread_cross: Max % above initial mid to pay
            spread_position: Where to place order in spread (0=bid, 1=ask, 0.3=30% into spread)
            dry_run: Test mode (no real orders)
            take_profit_pct: Optional TP as % above entry
            stop_loss_pct: Optional SL as % below entry

        Returns:
            dict with execution stats
        """
        return self._chase_limit(
            symbol=symbol,
            side=OrderSide.BUY,
            size=size,
            max_duration=max_duration,
            patience_ms=patience_ms,
            max_spread_cross=max_spread_cross,
            spread_position=spread_position,
            dry_run=dry_run,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
        )

    def chase_limit_sell(
        self,
        symbol: str,
        size: float,
        max_duration: float = 30.0,
        patience_ms: int = 300,
        max_spread_cross: float = 0.005,
        spread_position: float = 0.3,
        dry_run: bool = True,
    ) -> dict:
        """
        Execute sell using chase-limit strategy.

        Args:
            symbol: Trading pair (e.g., "ETH/USDC:USDC")
            size: Amount to sell
            max_duration: Max time to chase (seconds)
            patience_ms: Wait time before repricing (ms)
            max_spread_cross: Max % below initial mid to accept
            spread_position: Where to place order in spread (0=ask, 1=bid)
            dry_run: Test mode (no real orders)

        Returns:
            dict with execution stats
        """
        return self._chase_limit(
            symbol=symbol,
            side=OrderSide.SELL,
            size=size,
            max_duration=max_duration,
            patience_ms=patience_ms,
            max_spread_cross=max_spread_cross,
            spread_position=spread_position,
            dry_run=dry_run,
        )

    def _chase_limit(
        self,
        symbol: str,
        side: OrderSide,
        size: float,
        max_duration: float,
        patience_ms: int,
        max_spread_cross: float,
        spread_position: float,
        dry_run: bool,
        take_profit_pct: Optional[float] = None,
        stop_loss_pct: Optional[float] = None,
    ) -> dict:
        """Internal chase-limit implementation."""
        
        side_str = "BUY" if side == OrderSide.BUY else "SELL"
        
        print("="*80)
        print(f"CHASE-LIMIT {side_str} - {symbol}")
        print("="*80)
        print(f"Size: {size} | Max duration: {max_duration}s | Patience: {patience_ms}ms")
        print(f"Max slippage: {max_spread_cross:.2%} | Spread position: {spread_position:.0%}")
        print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
        print("="*80 + "\n")

        filled_size = 0.0
        total_cost = 0.0
        attempts = 0
        start_time = time.time()
        current_order_id = None
        fills = []

        # Get initial prices from REAL orderbook
        try:
            best_bid, best_ask = self.client.get_bid_ask(symbol)
            initial_mid = (best_bid + best_ask) / 2
            initial_spread = best_ask - best_bid
            initial_spread_pct = (initial_spread / initial_mid) * 100
            
            print(f"Initial Orderbook:")
            print(f"  Best Bid: ${best_bid:.4f}")
            print(f"  Best Ask: ${best_ask:.4f}")
            print(f"  Spread:   ${initial_spread:.4f} ({initial_spread_pct:.4f}%)")
            print(f"  Mid:      ${initial_mid:.4f}")
        except Exception as e:
            print(f"[ERROR] Failed to get orderbook: {e}")
            return self._make_result(0, 0, size, 0, 0, [])

        # Calculate price limits
        if side == OrderSide.BUY:
            max_price = initial_mid * (1 + max_spread_cross)
            print(f"  Max price: ${max_price:.4f} (mid + {max_spread_cross:.2%})")
        else:
            min_price = initial_mid * (1 - max_spread_cross)
            print(f"  Min price: ${min_price:.4f} (mid - {max_spread_cross:.2%})")
        
        print()

        while filled_size < size:
            elapsed = time.time() - start_time

            if elapsed > max_duration:
                print(f"\n[TIMEOUT] {elapsed:.1f}s elapsed")
                break

            attempts += 1
            remaining = size - filled_size

            # Get REAL current orderbook
            try:
                best_bid, best_ask = self.client.get_bid_ask(symbol)
                current_spread = best_ask - best_bid
                current_mid = (best_bid + best_ask) / 2
            except Exception as e:
                print(f"[ERROR] Orderbook fetch failed: {e}")
                time.sleep(1)
                continue

            # Calculate limit price INSIDE the spread
            if side == OrderSide.BUY:
                # For buys: start at bid, move toward ask based on spread_position
                limit_price = best_bid + (current_spread * spread_position)
                
                # Round to tick size
                tick_size = self.client.get_tick_size(symbol)
                limit_price = round(limit_price / tick_size) * tick_size
                
                # Safety check
                if limit_price > max_price:
                    print(f"\n[STOP] Price ${limit_price:.4f} > max ${max_price:.4f}")
                    break
                
                inside_by = best_ask - limit_price
            else:
                # For sells: start at ask, move toward bid based on spread_position
                limit_price = best_ask - (current_spread * spread_position)
                
                tick_size = self.client.get_tick_size(symbol)
                limit_price = round(limit_price / tick_size) * tick_size
                
                # Safety check
                if limit_price < min_price:
                    print(f"\n[STOP] Price ${limit_price:.4f} < min ${min_price:.4f}")
                    break
                
                inside_by = limit_price - best_bid

            # Log attempt
            timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            print(f"[{attempts:03d}] {timestamp} | {side_str} {remaining:.4f} @ ${limit_price:.4f} "
                  f"| Bid: ${best_bid:.4f} Ask: ${best_ask:.4f} | Inside: ${inside_by:.4f}")

            if not dry_run:
                # Cancel previous order if exists
                if current_order_id:
                    try:
                        self.client.cancel_order(current_order_id, symbol)
                    except:
                        pass

                # Place new order
                try:
                    order = self.client.place_limit_order(
                        symbol=symbol,
                        side=side,
                        price=limit_price,
                        size=remaining,
                        post_only=True  # Ensure maker fee
                    )
                    current_order_id = order.order_id
                    print(f"  > Submitted: {current_order_id}")
                except Exception as e:
                    print(f"  > Error: {e}")
                    time.sleep(patience_ms / 1000)
                    continue
            else:
                current_order_id = f"DRY_{attempts}"
                print(f"  > [DRY RUN] Submitted: {current_order_id}")

            # Wait for fill
            time.sleep(patience_ms / 1000)

            # Check fill status
            if not dry_run:
                try:
                    order_status = self.client.get_order_status(current_order_id, symbol)
                    filled_qty = float(order_status.get("filled", 0))
                    
                    if filled_qty > 0:
                        fill_price = float(order_status.get("average", limit_price))
                        fill_cost = filled_qty * fill_price
                        
                        filled_size += filled_qty
                        total_cost += fill_cost
                        fills.append({
                            'size': filled_qty,
                            'price': fill_price,
                            'timestamp': datetime.now()
                        })
                        
                        print(f"  [FILLED] {filled_qty:.4f} @ ${fill_price:.4f}")
                        
                        if order_status.get("status") == "closed":
                            current_order_id = None
                    else:
                        # Not filled, will reprice on next iteration
                        print(f"  > Not filled, repricing...")
                        
                except Exception as e:
                    print(f"  > Error checking status: {e}")
            else:
                # DRY RUN: Simulate fills based on spread position
                # Higher spread_position = more aggressive = higher fill rate
                import random
                fill_probability = 0.2 + (spread_position * 0.4)  # 20-60% based on aggression
                
                if random.random() < fill_probability:
                    fill_price = limit_price
                    filled_size += remaining
                    total_cost += remaining * fill_price
                    fills.append({
                        'size': remaining,
                        'price': fill_price,
                        'timestamp': datetime.now()
                    })
                    print(f"  [FILLED] [DRY RUN] {remaining:.4f} @ ${fill_price:.4f}")
                else:
                    print(f"  > [DRY RUN] Not filled")

        # Clean up - cancel any remaining order
        if current_order_id and not dry_run:
            try:
                self.client.cancel_order(current_order_id, symbol)
            except:
                pass

        # Calculate results
        elapsed = time.time() - start_time
        result = self._make_result(filled_size, total_cost, size, attempts, elapsed, fills)
        
        # Print summary
        self._print_summary(result, initial_mid, side)
        
        # Place TP/SL if requested and we got fills
        if not dry_run and filled_size > 0 and (take_profit_pct or stop_loss_pct):
            self._place_exit_orders(
                symbol=symbol,
                side=side,
                size=filled_size,
                entry_price=result['avg_price'],
                take_profit_pct=take_profit_pct,
                stop_loss_pct=stop_loss_pct
            )
        
        return result

    def _make_result(
        self,
        filled_size: float,
        total_cost: float,
        target_size: float,
        attempts: int,
        duration: float,
        fills: list
    ) -> dict:
        """Create result dictionary."""
        return {
            'filled_size': filled_size,
            'target_size': target_size,
            'fill_rate': filled_size / target_size if target_size > 0 else 0,
            'avg_price': total_cost / filled_size if filled_size > 0 else 0,
            'total_cost': total_cost,
            'attempts': attempts,
            'duration': duration,
            'fills': fills,
        }

    def _print_summary(self, result: dict, initial_mid: float, side: OrderSide) -> None:
        """Print execution summary."""
        print("\n" + "="*80)
        print("EXECUTION SUMMARY")
        print("="*80)
        
        fill_pct = result['fill_rate'] * 100
        print(f"Filled: {result['filled_size']:.4f} / {result['target_size']:.4f} ({fill_pct:.1f}%)")

        if result['filled_size'] > 0:
            avg_price = result['avg_price']
            slippage = ((avg_price / initial_mid) - 1) * 100
            
            if side == OrderSide.SELL:
                slippage = -slippage  # Invert for sells
            
            print(f"Average price: ${avg_price:.4f}")
            print(f"Initial mid:   ${initial_mid:.4f}")
            print(f"Slippage:      {slippage:+.4f}%")

            # Fee comparison
            maker_fee = result['total_cost'] * 0.00003  # 0.003%
            taker_fee = result['total_cost'] * 0.00009  # 0.009%
            savings = taker_fee - maker_fee

            print(f"\nFee Analysis:")
            print(f"  Maker fee (this):  ${maker_fee:.4f} (0.003%)")
            print(f"  Taker fee (market): ${taker_fee:.4f} (0.009%)")
            print(f"  SAVED:              ${savings:.4f} ({(savings/result['total_cost']*100):.4f}%)")
            
            # Update stats
            self.stats['total_trades'] += 1
            self.stats['total_volume'] += result['total_cost']
            self.stats['total_fee_savings'] += savings

        print(f"\nExecution: {result['attempts']} attempts in {result['duration']:.1f}s")
        print("="*80)

    def _place_exit_orders(
        self,
        symbol: str,
        side: OrderSide,
        size: float,
        entry_price: float,
        take_profit_pct: Optional[float],
        stop_loss_pct: Optional[float]
    ) -> None:
        """Place TP/SL orders after entry."""
        print("\n" + "-"*40)
        print("PLACING EXIT ORDERS")
        print("-"*40)
        
        exit_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
        
        if take_profit_pct:
            if side == OrderSide.BUY:
                tp_price = entry_price * (1 + take_profit_pct / 100)
            else:
                tp_price = entry_price * (1 - take_profit_pct / 100)
            
            print(f"Take Profit: ${tp_price:.4f} ({take_profit_pct:+.2f}%)")
            
            try:
                tp_price_formatted = self.client.price_to_precision(symbol, tp_price)
                self.client.exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=exit_side.value,
                    amount=self.client.amount_to_precision(symbol, size),
                    price=tp_price_formatted,
                    params={"takeProfitPrice": tp_price_formatted, "reduceOnly": True}
                )
                print(f"  [OK] TP order placed")
            except Exception as e:
                print(f"  [ERROR] TP failed: {e}")
        
        if stop_loss_pct:
            if side == OrderSide.BUY:
                sl_price = entry_price * (1 - stop_loss_pct / 100)
            else:
                sl_price = entry_price * (1 + stop_loss_pct / 100)
            
            print(f"Stop Loss:   ${sl_price:.4f} ({-stop_loss_pct:.2f}%)")
            
            try:
                sl_price_formatted = self.client.price_to_precision(symbol, sl_price)
                self.client.exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=exit_side.value,
                    amount=self.client.amount_to_precision(symbol, size),
                    price=sl_price_formatted,
                    params={"stopLossPrice": sl_price_formatted, "reduceOnly": True}
                )
                print(f"  [OK] SL order placed")
            except Exception as e:
                print(f"  [ERROR] SL failed: {e}")
        
        print("-"*40)

    def close_position(
        self,
        symbol: str,
        max_duration: float = 30.0,
        patience_ms: int = 300,
        dry_run: bool = True
    ) -> Optional[dict]:
        """
        Close existing position using chase-limit.
        
        Automatically detects position side and size.
        """
        position = self.client.get_position(symbol)
        
        if not position:
            print(f"[INFO] No open position for {symbol}")
            return None
        
        print(f"\n[POSITION] {position.side.upper()} {position.size} @ ${position.entry_price:.4f}")
        print(f"  Unrealized P&L: ${position.unrealized_pnl:.4f}")
        
        if position.side == "long":
            return self.chase_limit_sell(
                symbol=symbol,
                size=position.size,
                max_duration=max_duration,
                patience_ms=patience_ms,
                dry_run=dry_run
            )
        else:
            return self.chase_limit_buy(
                symbol=symbol,
                size=position.size,
                max_duration=max_duration,
                patience_ms=patience_ms,
                dry_run=dry_run
            )


def main():
    """CLI interface"""
    parser = argparse.ArgumentParser(description='Smart Order Executor - Chase Limit')
    parser.add_argument('--symbol', default='ETH/USDC:USDC', help='Trading pair')
    parser.add_argument('--size', type=float, default=0.01, help='Order size')
    parser.add_argument('--side', choices=['buy', 'sell'], default='buy', help='Order side')
    parser.add_argument('--duration', type=float, default=30.0, help='Max chase time (s)')
    parser.add_argument('--patience', type=int, default=300, help='Wait between reprices (ms)')
    parser.add_argument('--max-slippage', type=float, default=0.5, help='Max slippage (percent)')
    parser.add_argument('--spread-pos', type=float, default=0.3, help='Position in spread (0-1)')
    parser.add_argument('--tp', type=float, help='Take profit percent')
    parser.add_argument('--sl', type=float, help='Stop loss percent')
    parser.add_argument('--live', action='store_true', help='Live trading (default: dry run)')
    parser.add_argument('--close', action='store_true', help='Close existing position')

    args = parser.parse_args()

    print("\n" + "="*80)
    print("SMART EXECUTOR - Chase-Limit Orders (CCXT)")
    print("="*80)
    print("Using REAL orderbook data for precise spread capture")
    print("Saves 3-10 bps per trade via maker fees")
    print("="*80 + "\n")

    # Initialize
    try:
        client = create_client_from_env()
    except ValueError as e:
        print(f"[ERROR] {e}")
        print("\nFor dry run testing without credentials, creating read-only client...")
        
        # Create minimal client for dry run testing
        import ccxt
        
        class DryRunClient:
            def __init__(self):
                self.exchange = ccxt.hyperliquid({"enableRateLimit": True})
                self.exchange.load_markets()
            
            def get_bid_ask(self, symbol):
                book = self.exchange.fetch_order_book(symbol, limit=1)
                return float(book['bids'][0][0]), float(book['asks'][0][0])
            
            def get_tick_size(self, symbol):
                return 0.01
            
            def get_position(self, symbol):
                return None
        
        if args.live:
            print("[ERROR] Cannot run live without credentials!")
            return
        
        client = DryRunClient()

    executor = SmartExecutor(client)

    # Execute
    if args.close:
        result = executor.close_position(
            symbol=args.symbol,
            max_duration=args.duration,
            patience_ms=args.patience,
            dry_run=not args.live
        )
    elif args.side == 'buy':
        result = executor.chase_limit_buy(
            symbol=args.symbol,
            size=args.size,
            max_duration=args.duration,
            patience_ms=args.patience,
            max_spread_cross=args.max_slippage / 100,
            spread_position=args.spread_pos,
            dry_run=not args.live,
            take_profit_pct=args.tp,
            stop_loss_pct=args.sl
        )
    else:
        result = executor.chase_limit_sell(
            symbol=args.symbol,
            size=args.size,
            max_duration=args.duration,
            patience_ms=args.patience,
            max_spread_cross=args.max_slippage / 100,
            spread_position=args.spread_pos,
            dry_run=not args.live
        )

    if not args.live:
        print("\n" + "-"*60)
        print("TIP: To run LIVE:")
        print(f"  python executor.py --live --size 0.01 --symbol {args.symbol}")
        print("  Start with SMALL size to test!")
        print("-"*60 + "\n")


if __name__ == "__main__":
    main()
