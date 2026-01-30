#!/usr/bin/env python3
"""
XYZ HIP-3 Neutral Grid Bot

Main entry point for the grid trading bot.
Runs neutral grids on XYZ HIP-3 pairs with funding rate optimization.
"""
import asyncio
import sys
import signal
from datetime import datetime, timezone
from typing import Dict, Optional
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger

from config import (
    BotConfig, DEFAULT_CONFIG, XYZ_PAIRS, GridBias,
    WALLET_ADDRESS, PRIVATE_KEY, validate_wallet_config,
)
from xyz_client import XYZClient, OrderStatus, Order
from strategy import GridManager, FundingOptimizer
from trade_logger import TradeLogger


class GridBot:
    """
    Main Grid Bot Controller
    
    Manages multiple grids across different pairs with:
    - Periodic order refresh
    - Funding rate optimization
    - Risk monitoring
    - Position management
    """
    
    def __init__(self, config: Optional[BotConfig] = None):
        """
        Initialize the grid bot
        
        Args:
            config: Bot configuration (uses DEFAULT_CONFIG if None)
        """
        self.config = config or DEFAULT_CONFIG
        
        # Validate wallet config for live trading
        if not self.config.dry_run:
            logger.warning("=" * 60)
            logger.warning("  LIVE TRADING MODE - REAL MONEY AT RISK!")
            logger.warning("=" * 60)
            validate_wallet_config()
        
        # Initialize client (with wallet for live mode)
        if self.config.dry_run:
            self.client = XYZClient()
        else:
            self.client = XYZClient(
                wallet_address=WALLET_ADDRESS,
                private_key=PRIVATE_KEY,
                max_orders_per_minute=self.config.max_orders_per_minute,
            )
        
        # Grid managers for each pair
        self.grids: Dict[str, GridManager] = {}

        # Funding optimizer
        self.funding_optimizer = FundingOptimizer(
            client=self.client,
            config=self.config.funding,
            pairs=self.config.pairs,
        )

        # Trade logger for CSV export (save to bot directory)
        bot_dir = Path(__file__).parent
        trade_log_filename = f"trade_history_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
        trade_log_path = bot_dir / trade_log_filename
        self.trade_logger = TradeLogger(filename=str(trade_log_path))

        # State
        self.is_running = False
        self.start_time: Optional[datetime] = None
        
        # Price tracking
        self.mid_prices: Dict[str, float] = {}

        # Statistics
        self.total_profit = 0.0
        self.total_funding = 0.0

        logger.info(f"GridBot initialized with {len(self.config.pairs)} pairs")
        logger.info(f"Mode: {'DRY RUN' if self.config.dry_run else 'LIVE TRADING'}")
    
    def setup(self):
        """Initialize all components"""
        logger.info("Setting up grid bot...")
        
        # Load XYZ metadata
        meta = self.client.get_meta()
        logger.info(f"Loaded {len(meta.get('universe', []))} XYZ assets")
        
        # Initialize grid managers for each pair
        for pair_name in self.config.pairs:
            if pair_name not in XYZ_PAIRS:
                logger.warning(f"Unknown pair: {pair_name}, skipping")
                continue
            
            pair = XYZ_PAIRS[pair_name]
            
            grid = GridManager(
                client=self.client,
                pair=pair,
                params=self.config.grid,
                funding_config=self.config.funding,
                trade_logger=self.trade_logger,
            )
            
            self.grids[pair_name] = grid
            logger.info(f"Grid manager created for {pair_name}")
        
        logger.info(f"Setup complete: {len(self.grids)} grids ready")
    
    def initialize_grids(self):
        """Initialize all grid levels and place initial orders"""
        logger.info("Initializing grid levels...")
        
        # Check margin safety for live trading
        if not self.config.dry_run:
            self._check_margin_safety()
        
        for pair_name, grid in self.grids.items():
            try:
                # Get current price
                mid_price = self.client.get_mid_price(pair_name)
                
                if mid_price is None:
                    logger.error(f"Could not get price for {pair_name}")
                    continue
                
                # Store for status display
                self.mid_prices[pair_name] = mid_price
                
                # Set leverage for live trading (XYZ uses isolated margin)
                if not self.config.dry_run:
                    pair_config = XYZ_PAIRS.get(pair_name)
                    if pair_config:
                        max_lev = min(self.config.grid.leverage, pair_config.max_leverage)
                        leverage_ok = self.client.set_leverage(pair_name, int(max_lev))
                        if not leverage_ok:
                            logger.error(f"Failed to set leverage for {pair_name}, skipping pair")
                            continue
                
                # Initialize grid
                grid.initialize_grid(mid_price)

                # Place orders
                placed_orders = grid.place_grid_orders(dry_run=self.config.dry_run, current_price=mid_price)
                
                # Log startup summary with order IDs
                if placed_orders:
                    logger.info(f"{pair_name}: Placed {len(placed_orders)} initial orders:")
                    for order in placed_orders:
                        logger.info(
                            f"  ID={order.order_id} {order.side.value.upper()} "
                            f"{order.size} @ ${order.price:,.2f}"
                        )
                
                # Print grid
                grid.print_grid()
                
            except Exception as e:
                logger.error(f"Failed to initialize grid for {pair_name}: {e}")
    
    def _check_margin_safety(self):
        """Check if we have enough margin for the configured grid across all pairs"""
        total_capital = self.config.grid.total_capital_usd
        leverage = self.config.grid.leverage
        num_pairs = len(self.config.pairs)

        if num_pairs == 0:
            logger.warning("No pairs configured, skipping margin check")
            return

        # Calculate capital allocation
        capital_per_pair = total_capital / num_pairs
        total_required_margin = total_capital / leverage

        logger.info(
            f"Margin check for {num_pairs} pairs: "
            f"${capital_per_pair:,.2f}/pair, "
            f"${total_required_margin:,.2f} total margin required"
        )

        # Check total available margin
        available_balance = self.client.get_account_balance()

        if available_balance <= 0:
            logger.warning("Could not get account balance, skipping margin check")
            return

        # Calculate margin with buffer
        buffer = available_balance * (self.config.grid.margin_buffer_pct / 100)
        available_for_trading = available_balance - buffer

        logger.info(
            f"Account balance: ${available_balance:,.2f} | "
            f"Buffer ({self.config.grid.margin_buffer_pct}%): ${buffer:,.2f} | "
            f"Available: ${available_for_trading:,.2f}"
        )

        if total_required_margin > available_for_trading:
            utilization = (total_required_margin / available_for_trading) * 100
            logger.error(
                f"INSUFFICIENT MARGIN: Need ${total_required_margin:,.2f} "
                f"but only ${available_for_trading:,.2f} available "
                f"({utilization:.0f}% utilization)"
            )
            raise ValueError(
                f"Insufficient margin: need ${total_required_margin:,.2f}, "
                f"have ${available_for_trading:,.2f} (after {self.config.grid.margin_buffer_pct}% buffer)"
            )

        utilization = (total_required_margin / available_for_trading) * 100
        logger.info(f"✓ Margin OK: {utilization:.1f}% utilization")
    
    async def run_loop(self):
        """Main bot loop"""
        logger.info("Starting main loop...")
        self.is_running = True
        self.start_time = datetime.now(timezone.utc)

        refresh_interval = self.config.order_refresh_seconds
        data_interval = self.config.data_refresh_seconds
        heartbeat_interval = 30  # Log heartbeat every 30 seconds (testing mode)
        reconciliation_interval = 300  # Reconcile positions every 5 minutes

        last_refresh = datetime.now(timezone.utc)
        last_data = datetime.now(timezone.utc)
        last_heartbeat = datetime.now(timezone.utc)
        last_reconciliation = datetime.now(timezone.utc)

        while self.is_running:
            try:
                now = datetime.now(timezone.utc)

                # Data refresh (more frequent)
                if (now - last_data).total_seconds() >= data_interval:
                    await self._refresh_data()
                    last_data = now

                # Order refresh (less frequent)
                if (now - last_refresh).total_seconds() >= refresh_interval:
                    await self._refresh_orders()
                    last_refresh = now

                # Position reconciliation (every 5 minutes, only in live mode)
                if not self.config.dry_run and (now - last_reconciliation).total_seconds() >= reconciliation_interval:
                    self._reconcile_positions()
                    last_reconciliation = now

                # Heartbeat - show we're alive
                if (now - last_heartbeat).total_seconds() >= heartbeat_interval:
                    self._log_heartbeat()
                    last_heartbeat = now

                # Check funding optimization
                self._check_funding_optimization()

                # Small sleep to prevent CPU spinning
                await asyncio.sleep(1)
                
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(5)
    
    async def _refresh_data(self):
        """Refresh market data"""
        try:
            # Get current contexts for all pairs
            contexts = self.client.get_asset_contexts()

            # Update grid managers with new prices
            for ctx in contexts:
                if ctx.coin in self.grids:
                    grid = self.grids[ctx.coin]
                    
                    # Store current price
                    self.mid_prices[ctx.coin] = ctx.mark_price

                    # Log current price at DEBUG level
                    logger.debug(f"{ctx.coin} price: ${ctx.mark_price:,.2f}")

                    # Check if rebalance needed
                    if grid.state and grid.should_rebalance(ctx.mark_price):
                        logger.info(f"Rebalance triggered for {ctx.coin} - price moved from ${grid.state.center_price:,.2f} to ${ctx.mark_price:,.2f}")
                        grid.rebalance(ctx.mark_price, dry_run=self.config.dry_run)

        except Exception as e:
            logger.error(f"Data refresh failed: {e}")
    
    async def _refresh_orders(self):
        """
        Refresh grid orders - the core order management loop

        This function:
        1. Gets open orders from exchange
        2. Detects filled orders by comparing with grid state
        3. Handles fills by placing counter-orders
        4. Ensures all grid levels have orders
        """
        for pair_name, grid in self.grids.items():
            try:
                if not grid.state:
                    logger.warning(f"No grid state for {pair_name}, skipping")
                    continue

                # In dry run mode, just log that we would check orders
                if self.config.dry_run:
                    logger.debug(f"[DRY RUN] Checked orders for {pair_name} - no fills (simulated)")
                    continue

                # === STEP 1: Get current open orders from exchange ===
                open_orders = self.client.get_open_orders(pair_name)
                open_orders_map = {order.order_id: order for order in open_orders}

                logger.debug(f"{pair_name}: {len(open_orders)} open orders on exchange")

                # === STEP 2: Verify exchange order count (sanity check) ===
                exchange_order_count = len(open_orders)
                exchange_order_ids = set(o.order_id for o in open_orders)
                internal_order_count = sum(1 for lvl in grid.state.levels if lvl.order and lvl.order.status == OrderStatus.OPEN)
                internal_order_ids = set(lvl.order.order_id for lvl in grid.state.levels if lvl.order and lvl.order.status == OrderStatus.OPEN)
                
                if exchange_order_count != internal_order_count:
                    logger.warning(
                        f"{pair_name}: ORDER COUNT MISMATCH! "
                        f"Exchange: {exchange_order_count}, Internal: {internal_order_count}"
                    )
                    # Log which IDs are missing
                    missing_from_exchange = internal_order_ids - exchange_order_ids
                    extra_on_exchange = exchange_order_ids - internal_order_ids
                    if missing_from_exchange:
                        logger.warning(f"  Orders in internal state but NOT on exchange: {missing_from_exchange}")
                    if extra_on_exchange:
                        logger.warning(f"  Orders on exchange but NOT in internal state: {extra_on_exchange}")
                
                # === STEP 3: Check for filled and partially filled orders ===
                filled_orders = []
                filled_levels = {}  # Map order_id -> level for SIMPLE_GRID replacements

                for level in grid.state.levels:
                    # Skip levels without orders
                    if not level.order:
                        continue

                    # Skip orders that aren't marked as open
                    if level.order.status != OrderStatus.OPEN:
                        continue

                    # Skip orders without a valid order ID (placement failed)
                    if level.order.order_id is None:
                        logger.warning(f"{pair_name}: Level {level.index} has order without ID, skipping")
                        continue

                    # Check order status on exchange
                    order_id = level.order.order_id
                    if order_id not in open_orders_map:
                        # Order not in open orders - could be filled or cancelled
                        logger.warning(
                            f"{pair_name}: Order FILLED (or cancelled)! "
                            f"ID={order_id} {level.order.side.value.upper()} {level.order.size} @ ${level.order.price:,.2f}"
                        )
                        logger.debug(
                            f"  Exchange has {len(open_orders)} orders: {list(open_orders_map.keys())}"
                        )

                        # Save reference to the filled order BEFORE clearing level
                        filled_order_copy = Order(
                            coin=level.order.coin,
                            order_id=level.order.order_id,
                            side=level.order.side,
                            order_type=level.order.order_type,
                            price=level.order.price,
                            size=level.order.size,
                            filled_size=level.order.size,
                            status=OrderStatus.FILLED,
                        )
                        filled_orders.append(filled_order_copy)

                        # Save level reference for replacement logic
                        filled_levels[order_id] = level
                        
                        # CRITICAL: Clear the order from level immediately
                        # This prevents re-detection in the SAME cycle and future cycles
                        level.order = None

                    else:
                        # Order still exists - check for partial fill
                        current_order = open_orders_map[order_id]
                        original_size = level.order.size

                        # If remaining size is less than original, we have a partial fill
                        if current_order.size < original_size:
                            filled_amount = original_size - current_order.size

                            logger.warning(
                                f"{pair_name}: Order PARTIALLY FILLED! "
                                f"{level.order.side.value.upper()} {filled_amount}/{original_size} @ ${level.order.price:,.2f} "
                                f"({current_order.size} remaining)"
                            )

                            # Create a new order object for the filled portion
                            partial_fill = Order(
                                coin=level.order.coin,
                                order_id=level.order.order_id,
                                side=level.order.side,
                                order_type=level.order.order_type,
                                price=level.order.price,
                                size=filled_amount,  # Only the filled amount
                                filled_size=filled_amount,
                                status=OrderStatus.FILLED,
                            )

                            # Update the level's order to reflect remaining size
                            level.order.filled_size += filled_amount
                            level.order.status = OrderStatus.PARTIALLY_FILLED
                            # Note: Keep the order marked as open in the grid so we track the remaining portion

                            # Process the partial fill
                            filled_orders.append(partial_fill)

                # === STEP 4: Handle fills and place counter-orders ===
                for filled_order in filled_orders:
                    # Pass the level to handle_fill so it can find it
                    filled_level = filled_levels.get(filled_order.order_id)
                    
                    # Get the counter-order from grid manager
                    # Note: For SIMPLE_GRID, handle_fill tracks pairs and returns counter-orders
                    counter_order = grid.handle_fill(filled_order, filled_level=filled_level)

                    # Place counter-order if one was generated
                    if counter_order:
                        logger.warning(
                            f"{pair_name}: Placing counter-order: "
                            f"{counter_order.side.value.upper()} {counter_order.size} @ ${counter_order.price:,.2f}"
                        )

                        # Place the counter-order with retry (critical for grid continuity)
                        new_order = None
                        max_retries = 3
                        for attempt in range(max_retries):
                            try:
                                new_order = self.client.place_limit_order(
                                    coin=pair_name,
                                    side=counter_order.side,
                                    price=counter_order.price,
                                    size=counter_order.size,
                                    dry_run=self.config.dry_run,
                                )

                                if new_order:
                                    if attempt > 0:
                                        logger.info(f"Counter-order placed on attempt {attempt + 1}")
                                    break
                                else:
                                    # Failed, retry with backoff
                                    if attempt < max_retries - 1:
                                        delay = 1.0 * (2 ** attempt)
                                        logger.warning(f"Counter-order failed, retrying in {delay:.1f}s...")
                                        await asyncio.sleep(delay)
                            except Exception as e:
                                if attempt < max_retries - 1:
                                    delay = 1.0 * (2 ** attempt)
                                    logger.warning(f"Exception placing counter-order: {e}, retrying in {delay:.1f}s...")
                                    await asyncio.sleep(delay)
                                else:
                                    logger.error(f"Failed to place counter-order after {max_retries} attempts: {e}")

                        if new_order:
                            # For ARITHMETIC/PERCENT grids: attach counter-order to level
                            if not grid._is_simple_grid():
                                for level in grid.state.levels:
                                    if level.index == counter_order.level_index:
                                        level.order = new_order
                                        break
                        else:
                            logger.error(
                                f"CRITICAL: Failed to place counter-order for {pair_name} after all retries. "
                                f"Grid may be incomplete!"
                            )

                # === STEP 5: Ensure all grid levels have orders ===
                # NOTE: For SIMPLE_GRID, we do NOT auto-replenish while pairs are active
                # This prevents the cascade problem where every fill creates new positions
                if grid._is_simple_grid():
                    active_pairs_count = len(grid.active_pairs)
                    if active_pairs_count > 0:
                        logger.debug(
                            f"{pair_name}: SIMPLE_GRID has {active_pairs_count} active pairs, "
                            f"not auto-replenishing grid levels"
                        )
                    else:
                        # All pairs closed - safe to check for missing orders
                        missing_orders = grid.get_orders_to_place()
                        if missing_orders:
                            logger.info(f"{pair_name}: All pairs closed, replacing {len(missing_orders)} missing orders")
                            current_price = self.mid_prices.get(pair_name)
                            grid.place_grid_orders(dry_run=self.config.dry_run, current_price=current_price)
                else:
                    # For ARITHMETIC/PERCENT grids: replenish as usual
                    missing_orders = grid.get_orders_to_place()
                    if missing_orders:
                        logger.info(f"{pair_name}: Replacing {len(missing_orders)} missing orders")
                        current_price = self.mid_prices.get(pair_name)
                        grid.place_grid_orders(dry_run=self.config.dry_run, current_price=current_price)

            except Exception as e:
                logger.error(f"Order refresh failed for {pair_name}: {e}")
                logger.exception(e)  # Full traceback for debugging

    def _reconcile_positions(self):
        """
        Reconcile internal position tracking with actual exchange positions

        Runs periodically to detect:
        - Position tracking bugs
        - Missed fills
        - Manual trades
        - State loss from restarts
        """
        logger.debug("Reconciling positions with exchange...")

        mismatches_found = 0
        for pair_name, grid in self.grids.items():
            try:
                # Only reconcile if we're actually trading this pair
                if not grid.state:
                    continue

                # Run reconciliation
                matches = grid.reconcile_position()

                if not matches:
                    mismatches_found += 1

            except Exception as e:
                logger.error(f"Position reconciliation failed for {pair_name}: {e}")

        if mismatches_found > 0:
            logger.warning(
                f"Position reconciliation complete: {mismatches_found} mismatch(es) detected and synced"
            )
        else:
            logger.debug("Position reconciliation complete: all positions match")

    def _check_funding_optimization(self):
        """
        Log funding status (bias is applied at grid initialization)
        
        Since we use a persistent bias strategy, the grid is already
        biased short/long when initialized. We just log status here.
        """
        if self.config.funding.strategy.value == "ignore":
            return
        
        minutes_left = self.funding_optimizer.minutes_until_funding()
        
        # Log when funding is about to settle (every hour)
        if minutes_left <= 1:
            logger.info(f"Funding settling in {minutes_left} min")
            
            # In DYNAMIC mode, we might want to adjust bias based on current rates
            if self.config.funding.strategy.value == "dynamic":
                for pair_name, grid in self.grids.items():
                    bias = self.funding_optimizer.get_recommended_bias(pair_name)
                    if bias != grid.params.bias:
                        logger.info(f"{pair_name}: Dynamic bias suggests {bias.value}")

    def _log_heartbeat(self):
        """Log a heartbeat to show the bot is alive and working"""
        runtime = datetime.now(timezone.utc) - self.start_time if self.start_time else None
        runtime_str = str(runtime).split('.')[0] if runtime else "unknown"

        # Get current prices for monitored pairs
        try:
            contexts = self.client.get_asset_contexts()
            price_info = []
            for ctx in contexts:
                if ctx.coin in self.grids:
                    price_info.append(f"{ctx.coin.split(':')[1]}=${ctx.mark_price:,.2f}")

            prices_str = ", ".join(price_info[:3])  # Show first 3 pairs
            if len(price_info) > 3:
                prices_str += f" (+{len(price_info)-3} more)"

            # Count ACTUAL open orders from exchange (not internal state!)
            total_exchange_orders = 0
            total_active_pairs = 0
            for pair_name, grid in self.grids.items():
                if grid.state:
                    try:
                        exchange_orders = self.client.get_open_orders(pair_name)
                        total_exchange_orders += len(exchange_orders)
                        if grid._is_simple_grid():
                            total_active_pairs += len(grid.active_pairs)
                    except Exception:
                        pass  # Don't fail heartbeat on API error

            pairs_info = f" | Pairs: {total_active_pairs} active" if total_active_pairs > 0 else ""

            logger.info(
                f"⚡ Heartbeat | Runtime: {runtime_str} | "
                f"Monitoring: {len(self.grids)} grids | "
                f"Orders: {total_exchange_orders} on exchange{pairs_info} | "
                f"Prices: {prices_str}"
            )

        except Exception as e:
            logger.warning(f"Heartbeat error: {e}")
            logger.info(f"⚡ Heartbeat | Runtime: {runtime_str} | Bot running")

    def print_status(self):
        """Print current bot status"""
        print(f"\n{'='*70}")
        print(f"XYZ Grid Bot Status | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"{'='*70}")
        print(f"Mode: {'DRY RUN' if self.config.dry_run else 'LIVE'}")
        print(f"Running since: {self.start_time}")
        print(f"\nGrids:")
        
        for pair_name, grid in self.grids.items():
            current_price = self.mid_prices.get(pair_name)
            stats = grid.get_statistics(current_price)
            print(f"  {pair_name}:")
            print(f"    Position: {stats['current_position']:+.4f} (${stats['position_value_usd']:,.2f}, {stats['position_pct_of_max']:.0f}% of max)")
            print(f"    Cycles: {stats['total_cycles']} (L:{stats['long_cycles']} S:{stats['short_cycles']})")
            print(f"    Profit: ${stats['total_profit']:.2f}")
            if stats['position_limit_hits'] > 0:
                print(f"    WARNING: Position limit hit {stats['position_limit_hits']} times")
        
        print(f"\nFunding:")
        funding_summary = self.funding_optimizer.get_funding_summary()
        print(f"  Strategy: {funding_summary['strategy']} (bias: {funding_summary['bias_pct']}%)")
        print(f"  Next funding in: {funding_summary['minutes_until_next']} min")
        print(f"  Total collected: ${funding_summary['total_collected']:.2f}")

        # Trade history summary
        if self.trade_logger:
            trade_summary = self.trade_logger.get_summary()
            print(f"\nTrade History:")
            print(f"  Total Trades: {trade_summary['total_trades']}")
            print(f"  Completed Cycles: {trade_summary['completed_cycles']}")
            print(f"  Total Fees: ${trade_summary['total_fees']:.2f}")
            print(f"  Net P/L: ${trade_summary['total_pnl']:.2f}")
            print(f"  CSV File: {self.trade_logger.filename}")

        print(f"{'='*70}\n")
    
    def stop(self):
        """Stop the bot gracefully"""
        logger.info("Stopping bot...")
        self.is_running = False

        # In live mode, we would cancel all orders here
        if not self.config.dry_run:
            for pair_name in self.grids:
                self.client.cancel_all_orders(pair_name)

        # Print final status
        self.print_status()

        # Print detailed trade summary
        if self.trade_logger:
            self.trade_logger.print_summary()

        logger.info("Bot stopped")


async def main():
    """Main entry point"""
    # Configure logging with path relative to bot.py location
    bot_dir = Path(__file__).parent
    log_dir = bot_dir / "logs"
    log_dir.mkdir(exist_ok=True)

    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        level="INFO",
    )
    logger.add(
        str(log_dir / "grid_bot_{time}.log"),
        rotation="1 day",
        retention="7 days",
        level="WARNING",  # Only log important events to file (no heartbeats)
    )
    
    print("""
    ================================================================
    |         XYZ HIP-3 NEUTRAL GRID BOT                           |
    |         Funding Rate Optimized                               |
    ================================================================
    """)
    
    # Create bot with default config
    bot = GridBot(DEFAULT_CONFIG)
    
    # Handle shutdown signals
    def signal_handler(sig, frame):
        logger.info("Shutdown signal received")
        bot.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Setup
        bot.setup()
        
        # Show funding opportunities
        print("\n--- Current Funding Opportunities ---")
        bot.funding_optimizer.print_opportunities()
        
        # Initialize grids
        bot.initialize_grids()
        
        # Print initial status
        bot.print_status()
        
        # Run main loop
        await bot.run_loop()
        
    except KeyboardInterrupt:
        bot.stop()
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        bot.stop()
        raise


if __name__ == "__main__":
    # Create logs directory
    Path("logs").mkdir(exist_ok=True)
    
    # Run bot
    asyncio.run(main())
