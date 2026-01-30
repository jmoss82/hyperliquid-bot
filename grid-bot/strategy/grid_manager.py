"""
Grid Manager - Core Grid Trading Logic

Implements a neutral grid strategy that:
1. Places buy orders below current price
2. Places sell orders above current price
3. When price moves, fills trigger and grid rebalances
4. Captures profit from price oscillation within range
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone
from loguru import logger
import time

from xyz_client import XYZClient, OrderBook, OrderSide, OrderType, Order, OrderStatus
from xyz_client.models import GridLevel, GridState
from config import GridParameters, GridBias, GridSpacingMode, TradingPair, Fees, FundingConfig
from trade_logger import TradeLogger


@dataclass
class GridOrder:
    """Represents a grid order to be placed"""
    level_index: int
    side: OrderSide
    price: float
    size: float
    
    @property
    def is_buy(self) -> bool:
        return self.side == OrderSide.BUY


class GridManager:
    """
    Manages grid trading for a single pair
    
    The grid consists of:
    - N buy levels below current price (we buy when price drops)
    - N sell levels above current price (we sell when price rises)
    
    When a buy fills, we immediately place a sell at the next level up.
    When a sell fills, we immediately place a buy at the next level down.
    
    Profit = grid_spacing - fees (per completed cycle)
    """
    
    def __init__(
        self,
        client: XYZClient,
        pair: TradingPair,
        params: GridParameters,
        funding_config: Optional[FundingConfig] = None,
        trade_logger: Optional[TradeLogger] = None,
    ):
        """
        Initialize grid manager

        Args:
            client: XYZ API client
            pair: Trading pair configuration
            params: Grid parameters
            funding_config: Optional funding config for bias settings
            trade_logger: Optional trade logger for CSV export
        """
        self.client = client
        self.pair = pair
        self.params = params
        self.funding_config = funding_config
        self.trade_logger = trade_logger

        # Grid state
        self.state: Optional[GridState] = None
        self.is_active = False

        # Position tracking
        self.current_position: float = 0.0  # Positive = long, negative = short
        self.entry_prices: List[float] = []        # For long cycles (buy first)
        self.short_entry_prices: List[float] = []  # For short cycles (sell first)

        # Statistics
        self.long_cycles = 0      # Buy -> Sell completed
        self.short_cycles = 0     # Sell -> Buy completed
        self.total_profit = 0.0
        self.total_fees_paid = 0.0
        
        # Risk limits
        self.max_position_value = (
            params.total_capital_usd * params.leverage * (params.max_position_pct / 100)
        )
        self.position_limit_hits = 0  # Track how often we hit the limit

        logger.info(f"GridManager initialized for {pair.name}")
        logger.info(f"Max position value: ${self.max_position_value:,.2f}")

        # Position reconciliation tracking
        self.last_reconciliation: Optional[datetime] = None
        self.reconciliation_mismatches = 0

        # Simple Grid pair tracking (for independent buy-sell pairs)
        self.active_pairs: List[dict] = []  # Track pending buy-sell pairs
        self.completed_pairs: List[dict] = []  # Track completed pairs for stats
        self.grid_step: float = 0.0  # Grid step size (set during initialization)

        # Validate sizing on init
        self._validate_sizing()
    
    def _validate_sizing(self) -> None:
        """
        Validate that grid sizing is reasonable and won't exceed margin limits

        Raises warnings if configuration seems risky
        """
        params = self.params
        total_grids = self._get_total_grids()
        levels_per_side = total_grids // 2 if self._is_simple_grid() else self._get_levels_per_side()

        # Calculate total notional if all orders on one side fill
        total_notional = params.total_capital_usd * params.leverage

        # For SIMPLE_GRID: divide evenly across all levels
        if self._is_simple_grid():
            notional_per_level = total_notional / total_grids
        else:
            notional_per_level = total_notional * (params.size_per_level_pct / 100)

        max_one_side_notional = notional_per_level * levels_per_side
        
        # Required margin for max one-side exposure
        required_margin = max_one_side_notional / params.leverage
        
        # Check against capital
        margin_utilization = (required_margin / params.total_capital_usd) * 100
        
        logger.info(f"Sizing validation for {self.pair.name}:")
        logger.info(f"  Capital: ${params.total_capital_usd:,.2f}")
        logger.info(f"  Leverage: {params.leverage}x")
        logger.info(f"  Notional per level: ${notional_per_level:,.2f}")
        logger.info(f"  Max one-side notional: ${max_one_side_notional:,.2f}")
        logger.info(f"  Required margin (worst case): ${required_margin:,.2f}")
        logger.info(f"  Margin utilization: {margin_utilization:.1f}%")
        
        # Warnings
        if margin_utilization > 100:
            logger.error(
                f"CRITICAL: Sizing exceeds capital! Would need ${required_margin:,.2f} "
                f"but only have ${params.total_capital_usd:,.2f}. REDUCE SIZE OR LEVELS!"
            )
        elif margin_utilization > 80:
            logger.warning(
                f"HIGH RISK: Margin utilization at {margin_utilization:.0f}%. "
                f"Consider reducing size_per_level_pct or grid levels."
            )
        elif margin_utilization > 50:
            logger.warning(
                f"Moderate risk: Margin utilization at {margin_utilization:.0f}%. "
                f"Leave room for adverse moves."
            )
        
        # Check leverage against pair max
        if params.leverage > self.pair.max_leverage:
            logger.error(
                f"CRITICAL: Requested leverage {params.leverage}x exceeds "
                f"max allowed {self.pair.max_leverage}x for {self.pair.name}!"
            )

    def _is_arithmetic_grid(self) -> bool:
        return self.params.grid_spacing_mode == GridSpacingMode.ARITHMETIC

    def _is_simple_grid(self) -> bool:
        """Check if using SIMPLE_GRID mode (BYDFi-style)"""
        return self.params.grid_spacing_mode == GridSpacingMode.SIMPLE_GRID

    def _get_total_grids(self) -> int:
        if self.params.total_grids is not None:
            return int(self.params.total_grids)
        if self._is_arithmetic_grid():
            return int(self.params.num_levels)
        return self.params.num_levels * 2

    def _get_levels_per_side(self) -> int:
        if not self._is_arithmetic_grid():
            return self.params.num_levels
        total_grids = self._get_total_grids()
        if total_grids % 2 != 0:
            logger.warning(
                f"Arithmetic grid expects an even total_grids for neutral layout; "
                f"rounding down {total_grids} -> {total_grids - 1}."
            )
            total_grids -= 1
        return max(1, total_grids // 2)

    def _get_arithmetic_range(self) -> Tuple[float, float]:
        low = self.params.price_range_low
        high = self.params.price_range_high
        if low is None or high is None:
            raise ValueError("Arithmetic grid requires price_range_low and price_range_high")
        if low >= high:
            raise ValueError("Arithmetic grid requires price_range_low < price_range_high")
        return low, high

    def _update_level_sides(self) -> None:
        if not self.state or not self._is_arithmetic_grid():
            return
        gap_index = self.state.gap_index
        if gap_index is None:
            return
        for level in self.state.levels:
            if level.index < gap_index:
                level.side = OrderSide.BUY
            elif level.index > gap_index:
                level.side = OrderSide.SELL
            else:
                level.side = OrderSide.BUY
    
    def get_position_value(self, current_price: float) -> float:
        """Get current position value in USD (absolute value)"""
        return abs(self.current_position) * current_price
    
    def _calculate_arithmetic_levels(
        self,
        center_price: float,
        bias: GridBias,
    ) -> Tuple[List[GridLevel], int]:
        low, high = self._get_arithmetic_range()
        total_grids = self._get_total_grids()
        if total_grids < 2:
            raise ValueError("Arithmetic grid requires total_grids >= 2")
        if total_grids % 2 != 0:
            logger.warning(
                f"Arithmetic grid total_grids should be even for neutral grids; "
                f"rounding down {total_grids} -> {total_grids - 1}."
            )
            total_grids -= 1
        levels_per_side = max(1, total_grids // 2)
        step = (high - low) / total_grids
        center_line = (low + high) / 2

        if center_price < low or center_price > high:
            logger.warning(
                f"Center price ${center_price:,.2f} outside range "
                f"[${low:,.2f}, ${high:,.2f}]; clamping to range."
            )
            center_price = min(max(center_price, low), high)

        gap_index = int(round((center_price - center_line) / step))
        gap_index = max(-levels_per_side, min(levels_per_side, gap_index))

        if bias in (GridBias.SHORT, GridBias.LONG):
            bias_factor = self._get_bias_factor()
            bias_steps = int(round(levels_per_side * bias_factor))
            if bias_steps > 0:
                if bias == GridBias.SHORT:
                    gap_index -= bias_steps
                else:
                    gap_index += bias_steps
                gap_index = max(-levels_per_side, min(levels_per_side, gap_index))

        total_notional = self.params.total_capital_usd * self.params.leverage

        # For ARITHMETIC: divide evenly across all levels (excluding gap)
        # total_grids represents actual number of orders
        notional_per_level = total_notional / total_grids

        levels: List[GridLevel] = []
        for i in range(-levels_per_side, levels_per_side + 1):
            price = center_line + step * i
            size = notional_per_level / price
            size = self.client.format_size(self.pair.name, size)
            side = OrderSide.BUY if i < gap_index else OrderSide.SELL
            levels.append(GridLevel(
                index=i,
                price=round(price, 6),
                size=size,
                side=side,
            ))

        return levels, gap_index

    def _calculate_simple_grid_levels(
        self,
        center_price: float,
        bias: GridBias,
    ) -> Tuple[List[GridLevel], None]:
        """
        Calculate grid levels for SIMPLE_GRID mode (BYDFi-style)

        Key differences from arithmetic:
        - NO gap - all levels always have orders
        - Returns None for gap_index
        - All levels placed, both buys and sells
        """
        low, high = self._get_arithmetic_range()
        total_grids = self._get_total_grids()

        if total_grids < 2:
            raise ValueError("Simple grid requires total_grids >= 2")
        if total_grids % 2 != 0:
            logger.warning(
                f"Simple grid total_grids should be even for neutral grids; "
                f"rounding down {total_grids} -> {total_grids - 1}."
            )
            total_grids -= 1

        levels_per_side = max(1, total_grids // 2)
        step = (high - low) / total_grids
        center_line = (low + high) / 2

        # Store grid step for later use in counter-orders
        self.grid_step = step

        if center_price < low or center_price > high:
            logger.warning(
                f"Center price ${center_price:,.2f} outside range "
                f"[${low:,.2f}, ${high:,.2f}]; clamping to range."
            )
            center_price = min(max(center_price, low), high)

        total_notional = self.params.total_capital_usd * self.params.leverage

        # For SIMPLE_GRID: divide total notional evenly across all levels
        # total_grids represents actual number of orders (center is skipped)
        notional_per_level = total_notional / total_grids

        levels: List[GridLevel] = []

        # Generate all levels - NO GAP, all levels get orders
        for i in range(-levels_per_side, levels_per_side + 1):
            if i == 0:
                # Skip exact center for clean buy/sell split
                continue

            price = center_line + step * i
            size = notional_per_level / price
            size = self.client.format_size(self.pair.name, size)

            # Buy orders below center, sell orders above
            side = OrderSide.BUY if i < 0 else OrderSide.SELL

            levels.append(GridLevel(
                index=i,
                price=round(price, 6),
                size=size,
                side=side,
            ))

        logger.info(
            f"Simple grid: {len([l for l in levels if l.side == OrderSide.BUY])} buys, "
            f"{len([l for l in levels if l.side == OrderSide.SELL])} sells, "
            f"step=${step:.2f}"
        )

        return levels, None  # No gap in simple grid

    def can_increase_position(self, side: OrderSide, size: float, current_price: float) -> bool:
        """
        Check if we can open/increase a position without exceeding limits
        
        Args:
            side: The side of the new order (BUY or SELL)
            size: Size of the new order
            current_price: Current market price
            
        Returns:
            True if position can be increased, False if limit would be exceeded
        """
        # Calculate what position would be after this order
        if side == OrderSide.BUY:
            new_position = self.current_position + size
        else:
            new_position = self.current_position - size
        
        # Calculate notional value of new position
        new_position_value = abs(new_position) * current_price
        
        # Check against limit
        if new_position_value > self.max_position_value:
            logger.warning(
                f"Position limit would be exceeded: ${new_position_value:,.2f} > ${self.max_position_value:,.2f}"
            )
            self.position_limit_hits += 1
            return False
        
        return True
    
    def calculate_grid_levels(
        self, 
        center_price: float,
        bias: Optional[GridBias] = None,
    ) -> Tuple[List[GridLevel], Optional[int]]:
        """
        Calculate grid levels based on center price
        
        Args:
            center_price: Price to center the grid around
            bias: Optional override for grid bias
            
        Returns:
            Tuple of (levels, gap_index)
        """
        bias = bias or self.params.bias

        if self._is_simple_grid():
            levels, gap_index = self._calculate_simple_grid_levels(center_price, bias)
            return sorted(levels, key=lambda x: x.price), gap_index

        if self._is_arithmetic_grid():
            levels, gap_index = self._calculate_arithmetic_levels(center_price, bias)
            return sorted(levels, key=lambda x: x.price), gap_index

        num_levels = self.params.num_levels
        spacing_pct = self.params.grid_spacing_pct / 100
        
        # Calculate size per level
        total_notional = self.params.total_capital_usd * self.params.leverage
        notional_per_level = total_notional * (self.params.size_per_level_pct / 100)
        
        levels = []
        
        # Generate buy levels (below center)
        for i in range(1, num_levels + 1):
            price = center_price * (1 - spacing_pct * i)
            size = notional_per_level / price
            size = self.client.format_size(self.pair.name, size)
            
            levels.append(GridLevel(
                index=-i,  # Negative indices for buys
                price=round(price, 6),
                size=size,
                side=OrderSide.BUY,
            ))
        
        # Generate sell levels (above center)
        for i in range(1, num_levels + 1):
            price = center_price * (1 + spacing_pct * i)
            size = notional_per_level / price
            size = self.client.format_size(self.pair.name, size)
            
            levels.append(GridLevel(
                index=i,  # Positive indices for sells
                price=round(price, 6),
                size=size,
                side=OrderSide.SELL,
            ))
        
        # Apply bias if needed
        if bias == GridBias.SHORT:
            # Shift grid down - more sell levels, fewer buy levels
            levels = self._apply_short_bias(levels, center_price)
        elif bias == GridBias.LONG:
            # Shift grid up - more buy levels, fewer sell levels
            levels = self._apply_long_bias(levels, center_price)
        
        return sorted(levels, key=lambda x: x.price), None
    
    def _get_bias_factor(self) -> float:
        """Get bias factor from funding config or use default"""
        if self.funding_config:
            return self.funding_config.bias_pct / 100
        return 0.2  # Default 20%
    
    def _apply_short_bias(
        self, 
        levels: List[GridLevel], 
        center_price: float
    ) -> List[GridLevel]:
        """
        Apply short bias to grid (favor net short exposure)
        
        Shifts all prices down, so:
        - Buy levels are lower (less likely to fill)
        - Sell levels are lower (more likely to fill)
        - Result: Net short position over time
        """
        spacing_pct = self.params.grid_spacing_pct / 100
        bias_factor = self._get_bias_factor()
        
        adjusted_levels = []
        for level in levels:
            # Shift price down by bias_factor * spacing
            new_price = level.price * (1 - spacing_pct * bias_factor)
            adjusted_levels.append(GridLevel(
                index=level.index,
                price=round(new_price, 6),
                size=level.size,
                side=level.side,
            ))
        
        logger.debug(f"Applied short bias: {bias_factor*100:.0f}% shift down")
        return adjusted_levels
    
    def _apply_long_bias(
        self, 
        levels: List[GridLevel], 
        center_price: float
    ) -> List[GridLevel]:
        """
        Apply long bias to grid (favor net long exposure)
        
        Shifts all prices up, so:
        - Buy levels are higher (more likely to fill)
        - Sell levels are higher (less likely to fill)
        - Result: Net long position over time
        """
        spacing_pct = self.params.grid_spacing_pct / 100
        bias_factor = self._get_bias_factor()
        
        adjusted_levels = []
        for level in levels:
            # Shift price up by bias_factor * spacing
            new_price = level.price * (1 + spacing_pct * bias_factor)
            adjusted_levels.append(GridLevel(
                index=level.index,
                price=round(new_price, 6),
                size=level.size,
                side=level.side,
            ))
        
        logger.debug(f"Applied long bias: {bias_factor*100:.0f}% shift up")
        return adjusted_levels
    
    def initialize_grid(self, center_price: Optional[float] = None) -> GridState:
        """
        Initialize or reset the grid
        
        Args:
            center_price: Optional price to center grid. If None, uses current mid.
            
        Returns:
            GridState with initialized levels
        """
        # Get current price if not provided
        if center_price is None:
            center_price = self.client.get_mid_price(self.pair.name)
            if center_price is None:
                raise ValueError(f"Could not get price for {self.pair.name}")
        
        logger.info(f"Initializing grid for {self.pair.name} at ${center_price:,.2f}")

        # For arithmetic grids, validate price is within range
        if self._is_arithmetic_grid():
            try:
                low, high = self._get_arithmetic_range()
                range_pct = ((high - low) / ((high + low) / 2)) * 100

                if center_price < low:
                    drift_pct = ((low - center_price) / center_price) * 100
                    logger.error(
                        f"⚠️  CRITICAL: Current price ${center_price:,.2f} is {drift_pct:.1f}% BELOW grid range "
                        f"[${low:,.2f} - ${high:,.2f}]. ALL ORDERS WILL BE ABOVE MARKET! "
                        f"Update price_range_low and price_range_high in config/settings.py"
                    )
                    if drift_pct > 5:
                        raise ValueError(
                            f"Price ${center_price:,.2f} is {drift_pct:.1f}% below grid range - "
                            f"grid would be ineffective. Please update configuration."
                        )
                elif center_price > high:
                    drift_pct = ((center_price - high) / center_price) * 100
                    logger.error(
                        f"⚠️  CRITICAL: Current price ${center_price:,.2f} is {drift_pct:.1f}% ABOVE grid range "
                        f"[${low:,.2f} - ${high:,.2f}]. ALL ORDERS WILL BE BELOW MARKET! "
                        f"Update price_range_low and price_range_high in config/settings.py"
                    )
                    if drift_pct > 5:
                        raise ValueError(
                            f"Price ${center_price:,.2f} is {drift_pct:.1f}% above grid range - "
                            f"grid would be ineffective. Please update configuration."
                        )
                else:
                    # Price is within range - check if it's near the edges
                    distance_to_low = ((center_price - low) / (high - low)) * 100
                    distance_to_high = ((high - center_price) / (high - low)) * 100

                    if distance_to_low < 20 or distance_to_high < 20:
                        logger.warning(
                            f"⚠️  Price ${center_price:,.2f} is near edge of grid range "
                            f"[${low:,.2f} - ${high:,.2f}]. Consider recentering the range for optimal performance."
                        )
                    else:
                        logger.info(
                            f"✓ Price ${center_price:,.2f} is well-centered in grid range "
                            f"[${low:,.2f} - ${high:,.2f}] ({range_pct:.1f}% wide)"
                        )
            except ValueError as e:
                # Re-raise validation errors
                raise
            except Exception as e:
                # Log but don't fail on other errors (e.g., if range not configured)
                logger.warning(f"Could not validate price range: {e}")

        # Calculate levels
        levels, gap_index = self.calculate_grid_levels(center_price)
        
        # Create state
        self.state = GridState(
            coin=self.pair.name,
            center_price=center_price,
            levels=levels,
            gap_index=gap_index,
        )

        if self._is_arithmetic_grid():
            self._update_level_sides()
        
        if gap_index is None:
            buy_count = len([l for l in levels if l.side == OrderSide.BUY])
            sell_count = len([l for l in levels if l.side == OrderSide.SELL])
        else:
            buy_count = len([l for l in levels if l.index < gap_index])
            sell_count = len([l for l in levels if l.index > gap_index])

        logger.info(f"Grid created: {buy_count} buys, {sell_count} sells")
        
        return self.state
    
    def get_orders_to_place(self) -> List[GridOrder]:
        """
        Get list of orders that need to be placed
        
        Returns orders for levels that don't have active orders.
        """
        if not self.state:
            return []
        
        orders = []
        if self._is_arithmetic_grid():
            self._update_level_sides()
            gap_index = self.state.gap_index
            for level in self.state.levels:
                if gap_index is not None and level.index == gap_index:
                    continue
                if not level.is_open:
                    orders.append(GridOrder(
                        level_index=level.index,
                        side=level.side,
                        price=level.price,
                        size=level.size,
                    ))
        else:
            for level in self.state.levels:
                if not level.is_open:
                    orders.append(GridOrder(
                        level_index=level.index,
                        side=level.side,
                        price=level.price,
                        size=level.size,
                    ))
        
        return orders

    def _place_order_with_retry(
        self,
        grid_order: GridOrder,
        dry_run: bool,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ) -> Optional[Order]:
        """
        Place an order with exponential backoff retry logic

        Args:
            grid_order: The grid order to place
            dry_run: Whether this is a dry run
            max_retries: Maximum number of retry attempts
            base_delay: Base delay in seconds (will increase exponentially)

        Returns:
            Order object if successful, None if all retries failed
        """
        for attempt in range(max_retries + 1):  # +1 for initial attempt
            try:
                # Attempt to place order
                order = self.client.place_limit_order(
                    coin=self.pair.name,
                    side=grid_order.side,
                    price=grid_order.price,
                    size=grid_order.size,
                    dry_run=dry_run,
                )

                if order:
                    # Success!
                    if attempt > 0:
                        logger.info(
                            f"Order placed successfully on attempt {attempt + 1}/{max_retries + 1}"
                        )
                    return order
                else:
                    # Order placement returned None (likely rate limit or rejection)
                    if attempt < max_retries:
                        # Calculate backoff delay: 1s, 2s, 4s, etc.
                        delay = base_delay * (2 ** attempt)
                        logger.warning(
                            f"Order placement failed (attempt {attempt + 1}/{max_retries + 1}), "
                            f"retrying in {delay:.1f}s..."
                        )
                        time.sleep(delay)
                    else:
                        logger.error(
                            f"Order placement failed after {max_retries + 1} attempts: "
                            f"{grid_order.side.value} {grid_order.size} @ ${grid_order.price:.2f}"
                        )
                        return None

            except Exception as e:
                # Catch any exceptions during order placement
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        f"Exception during order placement (attempt {attempt + 1}/{max_retries + 1}): {e}, "
                        f"retrying in {delay:.1f}s..."
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        f"Exception during order placement after {max_retries + 1} attempts: {e}"
                    )
                    return None

        return None

    def place_grid_orders(self, dry_run: bool = True, current_price: Optional[float] = None) -> List[Order]:
        """
        Place all pending grid orders

        Args:
            dry_run: If True, don't actually place orders
            current_price: Optional current price for position limit checks

        Returns:
            List of Order objects (placed or mock)
        """
        orders_to_place = self.get_orders_to_place()
        placed_orders = []
        skipped_orders = 0

        # Get current price if not provided
        if current_price is None and self.state:
            current_price = self.state.center_price  # Use center as fallback

        for grid_order in orders_to_place:
            # Check position limits before placing (if we have a price)
            if current_price and not dry_run:
                # Calculate what direction this order would take us
                if not self.can_increase_position(grid_order.side, grid_order.size, current_price):
                    # This order would exceed position limits - skip it
                    logger.debug(
                        f"Skipping {grid_order.side.value} order at ${grid_order.price:.2f} - "
                        f"would exceed position limit"
                    )
                    skipped_orders += 1
                    continue

            # Place order with retry logic (client handles dry_run internally)
            order = self._place_order_with_retry(
                grid_order=grid_order,
                dry_run=dry_run,
                max_retries=3,
                base_delay=1.0,
            )

            if order:
                placed_orders.append(order)

                # Update grid state
                for level in self.state.levels:
                    if level.index == grid_order.level_index:
                        level.order = order
                        break
            else:
                # Order failed after retries
                logger.error(
                    f"Failed to place order after retries: "
                    f"{grid_order.side.value} {grid_order.size} @ ${grid_order.price:.2f}"
                )
                skipped_orders += 1

        if skipped_orders > 0:
            logger.warning(f"Skipped {skipped_orders} orders for {self.pair.name} due to position limits")
        logger.info(f"Placed {len(placed_orders)} orders for {self.pair.name}")
        return placed_orders
    
    def handle_fill(self, filled_order: Order, filled_level: Optional[GridLevel] = None) -> Optional[GridOrder]:
        """
        Handle a filled order and determine next action
        
        When a buy fills: Place sell at next level up
        When a sell fills: Place buy at next level down
        
        Args:
            filled_order: The order that was filled
            filled_level: Optional - the grid level the order was at (if known)
            
        Returns:
            GridOrder for the counter-trade, or None
        """
        if not self.state:
            return None
        
        # Use provided level or search for it
        if filled_level is None:
            for level in self.state.levels:
                if level.order and level.order.order_id == filled_order.order_id:
                    filled_level = level
                    break
        
        if not filled_level:
            logger.warning(f"Could not find level for filled order {filled_order.order_id}")
            # For SIMPLE_GRID, we can still process without the level
            if not self._is_simple_grid():
                return None
        
        # Track cycle profit
        cycle_profit = None
        cycle_type = None
        
        # Handle BUY fill
        if filled_order.is_buy:
            self.current_position += filled_order.size
            self.state.total_buys += 1
            
            # Check if closing a short OR opening a long
            if len(self.short_entry_prices) > 0:
                # Closing a short - calculate profit (sold high, bought back lower)
                entry = self.short_entry_prices.pop(0)
                profit = (entry - filled_order.price) * filled_order.size
                fees = filled_order.price * filled_order.size * Fees.ROUND_TRIP_FEE
                net_profit = profit - fees
                cycle_profit = net_profit
                cycle_type = "SHORT"
                
                self.short_cycles += 1
                self.total_profit += net_profit
                self.total_fees_paid += fees
                
                logger.info(
                    f"SHORT cycle completed: Entry ${entry:,.2f} -> Exit ${filled_order.price:,.2f} "
                    f"| Profit: ${net_profit:.2f} (fees: ${fees:.2f})"
                )
            else:
                # Opening a long - track the entry
                self.entry_prices.append(filled_order.price)
                logger.debug(f"Long position opened at ${filled_order.price:,.2f}")
        
        # Handle SELL fill
        else:
            self.current_position -= filled_order.size
            self.state.total_sells += 1
            
            # Check if closing a long OR opening a short
            if len(self.entry_prices) > 0:
                # Closing a long - calculate profit (bought low, sold high)
                entry = self.entry_prices.pop(0)
                profit = (filled_order.price - entry) * filled_order.size
                fees = filled_order.price * filled_order.size * Fees.ROUND_TRIP_FEE
                net_profit = profit - fees
                cycle_profit = net_profit
                cycle_type = "LONG"
                
                self.long_cycles += 1
                self.total_profit += net_profit
                self.total_fees_paid += fees
                
                logger.info(
                    f"LONG cycle completed: Entry ${entry:,.2f} -> Exit ${filled_order.price:,.2f} "
                    f"| Profit: ${net_profit:.2f} (fees: ${fees:.2f})"
                )
            else:
                # Opening a short - track the entry
                self.short_entry_prices.append(filled_order.price)
                logger.debug(f"Short position opened at ${filled_order.price:,.2f}")

        # Log trade to CSV
        if self.trade_logger:
            # Calculate fee for this individual trade
            trade_fee = filled_order.price * filled_order.size * Fees.MAKER_FEE

            self.trade_logger.log_trade(
                pair=self.pair.name,
                side=filled_order.side.value.upper(),
                price=filled_order.price,
                size=filled_order.size,
                fee=trade_fee,
                cycle_profit=cycle_profit,
                position_after=self.current_position,
                notes="",  # Can add "DRY RUN" if needed
            )
        
        # Check position limits
        position_value = self.get_position_value(filled_order.price)
        position_pct = (position_value / self.max_position_value) * 100 if self.max_position_value > 0 else 0

        # Check if we're over the limit
        if position_value > self.max_position_value:
            # Determine if the counter-order reduces or maintains risk
            counter_reduces_exposure = (
                (self.current_position > 0 and not filled_order.is_buy) or  # Long position, sell counter
                (self.current_position < 0 and filled_order.is_buy)          # Short position, buy counter
            )

            if counter_reduces_exposure:
                # ALLOW the counter-order - it reduces our over-exposure
                logger.warning(
                    f"POSITION LIMIT EXCEEDED but placing counter-order to reduce exposure: "
                    f"${position_value:,.2f} ({position_pct:.0f}% of max) | Net: {self.current_position:+.4f}"
                )
                self.position_limit_hits += 1
                # Continue to place counter-order below
            else:
                # BLOCK the counter-order - it would maintain/increase exposure
                # (This case is unusual but could happen with complex fills)
                logger.error(
                    f"POSITION LIMIT EXCEEDED and counter-order would not reduce exposure: "
                    f"${position_value:,.2f} ({position_pct:.0f}% of max) | Net: {self.current_position:+.4f}"
                )
                self.position_limit_hits += 1
                return None
        elif position_pct > 80:
            logger.warning(
                f"Position at {position_pct:.0f}% of limit: ${position_value:,.2f} / ${self.max_position_value:,.2f}"
            )

        # ========== SIMPLE GRID MODE (BYDFi-style) ==========
        if self._is_simple_grid():
            # Get current market price to avoid crossing the market
            current_price = self.client.get_mid_price(self.pair.name) or filled_order.price

            # Simple counter-order logic: price ± grid_step
            if filled_order.is_buy:
                # Check if this buy is closing a short pair
                matching_pair = None
                for i, pair in enumerate(self.active_pairs):
                    # Match by size and type (more reliable than price for crossed-market fills)
                    if pair['type'] == 'short' and abs(pair['size'] - filled_order.size) < 0.0001:
                        # Found matching pair!
                        matching_pair = self.active_pairs.pop(i)
                        break

                if matching_pair:
                    # Closing a short pair
                    profit = (matching_pair['sell_price'] - filled_order.price) * filled_order.size
                    fees = filled_order.price * filled_order.size * Fees.ROUND_TRIP_FEE
                    net_profit = profit - fees

                    matching_pair['buy_time'] = datetime.now(timezone.utc)
                    matching_pair['profit'] = net_profit
                    self.completed_pairs.append(matching_pair)

                    logger.success(
                        f"PAIR COMPLETED: SELL ${matching_pair['sell_price']:.2f} → "
                        f"BUY ${filled_order.price:.2f} | "
                        f"Profit: ${net_profit:.2f} | Active pairs: {len(self.active_pairs)}"
                    )

                    # Don't place counter-order for closing buys
                    return None
                else:
                    # Opening a new long - place sell counter
                    counter_price = filled_order.price + self.grid_step

                    # Safety: ensure sell is above current market (avoid immediate fill)
                    min_safe_price = current_price * 1.001  # 0.1% above market
                    if counter_price < min_safe_price:
                        logger.warning(
                            f"Counter-sell ${counter_price:,.2f} would cross market ${current_price:,.2f}, "
                            f"adjusting to ${min_safe_price:,.2f}"
                        )
                        counter_price = min_safe_price

                    counter_side = OrderSide.SELL

                    # Track this as a new open pair (buy waiting for sell)
                    pair = {
                        'type': 'long',
                        'buy_price': filled_order.price,
                        'sell_price': counter_price,
                        'size': filled_order.size,
                        'buy_order_id': filled_order.order_id,  # Track the buy that opened this
                        'buy_time': datetime.now(timezone.utc),
                        'sell_time': None,
                        'profit': None,
                    }
                    self.active_pairs.append(pair)

                    logger.info(
                        f"BUY filled @ ${filled_order.price:,.2f} → "
                        f"Placing SELL counter @ ${counter_price:,.2f} | "
                        f"Active pairs: {len(self.active_pairs)}"
                    )
            else:
                # Sell filled → Check if closing a pair OR opening new short
                matching_pair = None
                for i, pair in enumerate(self.active_pairs):
                    # Match by size and type (more reliable than price for crossed-market fills)
                    if pair['type'] == 'long' and abs(pair['size'] - filled_order.size) < 0.0001:
                        # Found matching pair!
                        matching_pair = self.active_pairs.pop(i)
                        break

                if matching_pair:
                    # Closing a long pair
                    profit = (filled_order.price - matching_pair['buy_price']) * filled_order.size
                    fees = filled_order.price * filled_order.size * Fees.ROUND_TRIP_FEE
                    net_profit = profit - fees

                    matching_pair['sell_time'] = datetime.now(timezone.utc)
                    matching_pair['profit'] = net_profit
                    self.completed_pairs.append(matching_pair)

                    logger.success(
                        f"PAIR COMPLETED: BUY ${matching_pair['buy_price']:.2f} → "
                        f"SELL ${filled_order.price:.2f} | "
                        f"Profit: ${net_profit:.2f} | Active pairs: {len(self.active_pairs)}"
                    )

                    # Don't place counter-order for closing sells
                    return None
                else:
                    # Opening a new short - place buy counter
                    counter_price = filled_order.price - self.grid_step

                    # Safety: ensure buy is below current market (avoid immediate fill)
                    max_safe_price = current_price * 0.999  # 0.1% below market
                    if counter_price > max_safe_price:
                        logger.warning(
                            f"Counter-buy ${counter_price:,.2f} would cross market ${current_price:,.2f}, "
                            f"adjusting to ${max_safe_price:,.2f}"
                        )
                        counter_price = max_safe_price

                    counter_side = OrderSide.BUY

                    # Track this as a new short pair
                    pair = {
                        'type': 'short',
                        'sell_price': filled_order.price,
                        'buy_price': counter_price,
                        'size': filled_order.size,
                        'sell_order_id': filled_order.order_id,  # Track the sell that opened this
                        'sell_time': datetime.now(timezone.utc),
                        'buy_time': None,
                        'profit': None,
                    }
                    self.active_pairs.append(pair)

                    logger.info(
                        f"SELL filled @ ${filled_order.price:,.2f} → "
                        f"Placing BUY counter @ ${counter_price:,.2f} | "
                        f"Active pairs: {len(self.active_pairs)}"
                    )

                    # Return the counter-order
                    return GridOrder(
                        level_index=0,
                        side=counter_side,
                        price=round(counter_price, 6),
                        size=filled_order.size,
                    )

            # Return the counter-order (for buy fills)
            return GridOrder(
                level_index=0,  # Not used in simple grid
                side=counter_side,
                price=round(counter_price, 6),
                size=filled_order.size,  # Same size as filled order
            )

        # ========== ARITHMETIC GRID MODE (Original) ==========
        if self._is_arithmetic_grid():
            if self.state.gap_index is None:
                logger.warning("Arithmetic grid has no gap index; skipping counter-order.")
                return None

            old_gap_index = self.state.gap_index
            if old_gap_index == filled_level.index:
                logger.warning("Filled level equals gap index; skipping counter-order.")
                return None

            # Move the gap to the filled level, place counter at old gap
            self.state.gap_index = filled_level.index
            self._update_level_sides()

            counter_level = next(
                (level for level in self.state.levels if level.index == old_gap_index),
                None,
            )
            if not counter_level:
                logger.warning(f"Counter level {old_gap_index} not found; skipping counter-order.")
                return None
            if counter_level.is_open:
                logger.warning(
                    f"Counter level {old_gap_index} already has an open order; skipping counter-order."
                )
                return None

            counter_side = OrderSide.SELL if filled_order.is_buy else OrderSide.BUY
            counter_level.side = counter_side
            return GridOrder(
                level_index=old_gap_index,
                side=counter_side,
                price=counter_level.price,
                size=filled_order.size,
            )

        # Determine counter-order with bounds checking
        spacing_pct = self.params.grid_spacing_pct / 100
        num_levels = self.params.num_levels
        
        if filled_order.is_buy:
            # Buy filled -> place sell one level up
            new_index = filled_level.index + 1

            # Check if we're at the top edge of the grid
            if new_index > num_levels:
                logger.warning(
                    f"At grid edge (top): Cannot place sell above level {filled_level.index}. "
                    f"Consider rebalancing or widening grid."
                )
                return None

            # Find the theoretical grid level for the counter-order
            counter_level = next(
                (level for level in self.state.levels if level.index == new_index),
                None
            )

            if counter_level:
                # Use the theoretical grid price (prevents drift)
                sell_price = counter_level.price
            else:
                # Fallback: calculate from fill price (shouldn't happen if grid is properly initialized)
                logger.warning(f"Counter level {new_index} not found, calculating from fill price")
                sell_price = filled_order.price * (1 + spacing_pct)

            return GridOrder(
                level_index=new_index,
                side=OrderSide.SELL,
                price=round(sell_price, 6),
                size=filled_order.size,
            )
        else:
            # Sell filled -> place buy one level down
            new_index = filled_level.index - 1

            # Check if we're at the bottom edge of the grid
            if new_index < -num_levels:
                logger.warning(
                    f"At grid edge (bottom): Cannot place buy below level {filled_level.index}. "
                    f"Consider rebalancing or widening grid."
                )
                return None

            # Find the theoretical grid level for the counter-order
            counter_level = next(
                (level for level in self.state.levels if level.index == new_index),
                None
            )

            if counter_level:
                # Use the theoretical grid price (prevents drift)
                buy_price = counter_level.price
            else:
                # Fallback: calculate from fill price (shouldn't happen if grid is properly initialized)
                logger.warning(f"Counter level {new_index} not found, calculating from fill price")
                buy_price = filled_order.price * (1 - spacing_pct)

            return GridOrder(
                level_index=new_index,
                side=OrderSide.BUY,
                price=round(buy_price, 6),
                size=filled_order.size,
            )
    
    def should_rebalance(self, current_price: float) -> bool:
        """
        Check if grid should be rebalanced
        
        Rebalance if price has drifted too far from center
        """
        if not self.state:
            return False

        if self._is_arithmetic_grid():
            try:
                low, high = self._get_arithmetic_range()
            except ValueError:
                return False
            return current_price < low or current_price > high
        
        drift_pct = abs(current_price - self.state.center_price) / self.state.center_price
        return drift_pct > (self.params.rebalance_threshold_pct / 100)
    
    def rebalance(
        self, 
        new_center: float, 
        dry_run: bool = True,
        flatten_position: bool = False,
    ) -> GridState:
        """
        Rebalance grid around a new center price
        
        This will:
        1. Warn if there's an existing position
        2. Optionally flatten (market close) the position
        3. Cancel all existing orders
        4. Calculate new grid levels
        5. Place new orders
        
        Args:
            new_center: New center price for the grid
            dry_run: If True, don't place real orders
            flatten_position: If True, market close any existing position
        """
        old_center = self.state.center_price if self.state else 0
        
        logger.info(
            f"Rebalancing grid from ${old_center:,.2f} to ${new_center:,.2f}"
        )
        
        # Check for existing position
        if self.current_position != 0:
            position_value = abs(self.current_position) * new_center
            open_longs = len(self.entry_prices)
            open_shorts = len(self.short_entry_prices)
            
            logger.warning(
                f"Rebalancing with open position: {self.current_position:+.4f} "
                f"(${position_value:,.2f}) | Open longs: {open_longs}, Open shorts: {open_shorts}"
            )
            
            if flatten_position:
                logger.info("Flattening position before rebalance...")
                # TODO: Implement market close via client
                # For now, just clear tracking (position will need manual close)
                if not dry_run:
                    # This would be: self.client.market_close(self.pair.name)
                    pass
                
                # Clear position tracking
                self.current_position = 0.0
                self.entry_prices.clear()
                self.short_entry_prices.clear()
                logger.info("Position tracking cleared (manual close may be needed)")
            else:
                # Keep position tracking - these entries will still close when hit
                logger.info(
                    f"Keeping position tracking. Grid will still close positions when "
                    f"sell orders at entry+spread fill (longs) or buy orders at entry-spread fill (shorts)"
                )
        
        # Cancel existing orders
        if not dry_run:
            self.client.cancel_all_orders(self.pair.name)
        else:
            logger.info("[DRY RUN] Would cancel all existing orders")
        
        # Reinitialize grid
        self.initialize_grid(new_center)
        
        # Place new orders
        self.place_grid_orders(dry_run=dry_run)
        
        return self.state
    
    def get_statistics(self, current_price: Optional[float] = None) -> Dict:
        """Get grid statistics"""
        # Calculate position value if we have a price
        position_value = 0.0
        position_pct = 0.0
        if current_price and self.current_position != 0:
            position_value = self.get_position_value(current_price)
            position_pct = (position_value / self.max_position_value) * 100 if self.max_position_value > 0 else 0
        
        stats = {
            "pair": self.pair.name,
            "is_active": self.is_active,
            "center_price": self.state.center_price if self.state else None,
            "current_position": self.current_position,
            "position_value_usd": position_value,
            "position_pct_of_max": position_pct,
            "max_position_value": self.max_position_value,
            "position_limit_hits": self.position_limit_hits,
            "long_cycles": self.long_cycles,
            "short_cycles": self.short_cycles,
            "total_cycles": self.long_cycles + self.short_cycles,
            "total_profit": self.total_profit,
            "total_fees": self.total_fees_paid,
            "net_profit": self.total_profit,
            "open_longs": len(self.entry_prices),       # Buys waiting for sell
            "open_shorts": len(self.short_entry_prices), # Sells waiting for buy
            "open_buy_orders": self.state.open_buy_orders if self.state else 0,
            "open_sell_orders": self.state.open_sell_orders if self.state else 0,
        }

        # Add SIMPLE_GRID specific stats
        if self._is_simple_grid():
            total_pair_profit = sum(p['profit'] for p in self.completed_pairs if p['profit'] is not None)
            stats.update({
                "active_pairs": len(self.active_pairs),
                "completed_pairs": len(self.completed_pairs),
                "total_pair_profit": total_pair_profit,
            })

        return stats

    def reconcile_position(self, force: bool = False) -> bool:
        """
        Reconcile internal position tracking with actual exchange position

        This helps detect:
        - Position tracking bugs
        - Missed fills
        - Manual trades
        - Bot restart state loss

        Args:
            force: Force reconciliation even if recently done

        Returns:
            True if positions match, False if mismatch detected
        """
        # Check if we should reconcile (not too frequently)
        now = datetime.now(timezone.utc)
        if not force and self.last_reconciliation:
            time_since_last = (now - self.last_reconciliation).total_seconds()
            if time_since_last < 300:  # Don't reconcile more than every 5 minutes
                return True

        self.last_reconciliation = now

        # Get actual position from exchange
        try:
            exchange_position = self.client.get_position(self.pair.name)

            if exchange_position is None:
                # No position on exchange
                actual_size = 0.0
            else:
                actual_size = exchange_position.size

            # Compare with internal tracking
            internal_size = self.current_position

            # Allow small tolerance for rounding errors
            tolerance = 0.0001
            if abs(actual_size - internal_size) < tolerance:
                logger.debug(
                    f"Position reconciliation OK for {self.pair.name}: "
                    f"Internal={internal_size:+.4f}, Exchange={actual_size:+.4f}"
                )
                return True
            else:
                # Mismatch detected!
                difference = actual_size - internal_size
                self.reconciliation_mismatches += 1

                logger.error(
                    f"⚠️  POSITION MISMATCH for {self.pair.name}! "
                    f"Internal={internal_size:+.4f}, Exchange={actual_size:+.4f}, "
                    f"Difference={difference:+.4f} ({self.reconciliation_mismatches} total mismatches)"
                )

                # Option 1: Update internal tracking to match exchange (safer)
                logger.warning(f"Syncing internal position to exchange: {internal_size:+.4f} -> {actual_size:+.4f}")
                self.current_position = actual_size

                # Also need to adjust entry tracking to prevent incorrect P&L calculations
                # This is a best-effort - we can't reconstruct exact entry prices
                if abs(actual_size) > abs(internal_size):
                    # We have more position than expected - might have missed fills
                    logger.warning(
                        "Position larger than expected - may have missed fill notifications. "
                        "Entry price tracking may be inaccurate."
                    )
                elif abs(actual_size) < abs(internal_size):
                    # We have less position than expected - might have manual close or missed close fills
                    logger.warning(
                        "Position smaller than expected - may have manual trades or missed closes. "
                        "Adjusting entry tracking."
                    )
                    # Try to adjust entry lists proportionally
                    if actual_size == 0:
                        # Position completely closed - clear all entries
                        self.entry_prices.clear()
                        self.short_entry_prices.clear()
                        logger.warning("Cleared all entry tracking as position is flat on exchange")

                return False

        except Exception as e:
            logger.error(f"Failed to reconcile position for {self.pair.name}: {e}")
            return True  # Assume OK on error to avoid false alarms

    def print_grid(self):
        """Print current grid state"""
        if not self.state:
            print("Grid not initialized")
            return
        
        print(f"\n{'='*60}")
        range_info = ""
        if self._is_arithmetic_grid():
            try:
                low, high = self._get_arithmetic_range()
                range_info = f" | Range: ${low:,.2f} - ${high:,.2f}"
            except ValueError:
                pass
        print(f"Grid: {self.pair.name} | Center: ${self.state.center_price:,.2f}{range_info}")
        print(f"{'='*60}")
        print(f"{'Level':<8} {'Side':<6} {'Price':<15} {'Size':<12} {'Status':<10}")
        print(f"{'-'*60}")
        
        gap_index = self.state.gap_index if self._is_arithmetic_grid() else None
        for level in sorted(self.state.levels, key=lambda x: -x.price):
            if gap_index is not None and level.index == gap_index:
                status = "GAP"
            else:
                status = "OPEN" if level.is_open else "PENDING"
            if level.is_filled:
                status = "FILLED"
            
            print(
                f"{level.index:<8} {level.side.value.upper():<6} "
                f"${level.price:<14,.2f} {level.size:<12.4f} {status:<10}"
            )
        
        print(f"{'='*60}")
        total_cycles = self.long_cycles + self.short_cycles
        print(f"Position: {self.current_position:+.4f} | Cycles: {total_cycles} (L:{self.long_cycles} S:{self.short_cycles}) | Profit: ${self.total_profit:.2f}")

        # Different display for SIMPLE_GRID mode
        if self._is_simple_grid():
            total_pair_profit = sum(p['profit'] for p in self.completed_pairs if p['profit'] is not None)
            print(f"Active pairs: {len(self.active_pairs)} | Completed pairs: {len(self.completed_pairs)} | Pair profit: ${total_pair_profit:.2f}")
        else:
            print(f"Open positions: {len(self.entry_prices)} longs waiting, {len(self.short_entry_prices)} shorts waiting")

        print()
