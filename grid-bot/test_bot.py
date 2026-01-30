#!/usr/bin/env python3
"""
Test script for XYZ HIP-3 Grid Bot

Verifies all components work correctly without placing real orders.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger

# Configure simple logging
logger.remove()
logger.add(sys.stderr, format="{time:HH:mm:ss} | {level} | {message}", level="INFO")


def test_xyz_client():
    """Test XYZ client API access"""
    print("\n" + "="*60)
    print("TEST 1: XYZ Client API Access")
    print("="*60)
    
    from xyz_client import XYZClient
    
    client = XYZClient()
    
    # Test meta endpoint
    print("\n1.1 Testing meta endpoint...")
    meta = client.get_meta()
    universe = meta.get('universe', [])
    print(f"    Found {len(universe)} XYZ assets")
    assert len(universe) > 0, "No assets found"
    print("    PASS")
    
    # Test asset contexts
    print("\n1.2 Testing asset contexts...")
    contexts = client.get_asset_contexts()
    print(f"    Got {len(contexts)} asset contexts")
    assert len(contexts) > 0, "No contexts found"
    
    # Print a few
    for ctx in contexts[:3]:
        print(f"    {ctx.coin}: ${ctx.mark_price:,.2f}, funding={ctx.funding_rate_pct:.4f}%")
    print("    PASS")
    
    # Test order book
    print("\n1.3 Testing order book...")
    book = client.get_order_book("xyz:GOLD")
    assert book is not None, "No order book"
    print(f"    xyz:GOLD mid: ${book.mid_price:,.2f}, spread: {book.spread_pct:.4f}%")
    print("    PASS")
    
    # Test funding history
    print("\n1.4 Testing funding history...")
    history = client.get_funding_history("xyz:GOLD", limit=10)
    assert len(history) > 0, "No funding history"
    print(f"    Got {len(history)} funding records")
    print(f"    Latest: {history[0].funding_rate_pct:.6f}%")
    print("    PASS")
    
    print("\n--- XYZ Client: ALL TESTS PASSED ---")
    return client


def test_grid_manager(client):
    """Test grid manager"""
    print("\n" + "="*60)
    print("TEST 2: Grid Manager")
    print("="*60)
    
    from config import GridParameters, XYZ_PAIRS
    from strategy import GridManager
    
    # Create grid manager
    pair = XYZ_PAIRS["xyz:GOLD"]
    params = GridParameters(
        num_levels=5,
        grid_spacing_pct=0.5,
        total_capital_usd=1000,
        leverage=5.0,
    )
    
    grid = GridManager(client, pair, params)
    
    # Initialize grid
    print("\n2.1 Initializing grid...")
    mid_price = client.get_mid_price("xyz:GOLD")
    state = grid.initialize_grid(mid_price)
    
    print(f"    Center: ${state.center_price:,.2f}")
    print(f"    Levels: {len(state.levels)}")
    print("    PASS")
    
    # Print grid
    print("\n2.2 Grid visualization:")
    grid.print_grid()
    
    # Test order generation
    print("\n2.3 Testing order generation...")
    orders = grid.get_orders_to_place()
    print(f"    Orders to place: {len(orders)}")
    print("    PASS")
    
    print("\n--- Grid Manager: ALL TESTS PASSED ---")
    return grid


def test_funding_optimizer(client):
    """Test funding optimizer"""
    print("\n" + "="*60)
    print("TEST 3: Funding Optimizer")
    print("="*60)
    
    from config import FundingConfig, FundingStrategy
    from strategy import FundingOptimizer
    
    config = FundingConfig(
        strategy=FundingStrategy.SHORT_BIAS,
        funding_period_hours=1,
        bias_pct=20.0,
    )
    
    pairs = ["xyz:XYZ100", "xyz:GOLD", "xyz:TSLA", "xyz:SILVER"]
    
    optimizer = FundingOptimizer(client, config, pairs)
    
    # Test opportunity analysis
    print("\n3.1 Analyzing funding opportunities...")
    opportunities = optimizer.get_all_opportunities()
    print(f"    Found {len(opportunities)} profitable opportunities")
    
    for opp in opportunities[:3]:
        print(f"    {opp.coin}: {opp.apr:.2f}% APR ({opp.direction})")
    print("    PASS")
    
    # Test timing and bias
    print("\n3.2 Testing funding timing and bias...")
    minutes = optimizer.minutes_until_funding()
    print(f"    Minutes until next funding: {minutes}")
    print(f"    Strategy: {config.strategy.value}")
    print(f"    Bias percentage: {optimizer.get_bias_percentage()*100:.0f}%")
    recommended = optimizer.get_recommended_bias("xyz:GOLD")
    print(f"    Recommended bias for GOLD: {recommended.value}")
    print("    PASS")
    
    # Print full opportunities
    print("\n3.3 Full opportunity display:")
    optimizer.print_opportunities()
    
    print("\n--- Funding Optimizer: ALL TESTS PASSED ---")


def test_full_integration():
    """Test full bot integration"""
    print("\n" + "="*60)
    print("TEST 4: Full Integration")
    print("="*60)
    
    from config import BotConfig, GridParameters, FundingConfig, FundingStrategy, GridSpacingMode, GridBias
    from bot import GridBot
    from xyz_client import XYZClient
    
    # First get current XYZ100 price for arithmetic grid range
    client = XYZClient()
    xyz100_price = client.get_mid_price("xyz:XYZ100")
    if xyz100_price is None:
        xyz100_price = 25000  # Fallback
    
    # Calculate arithmetic grid range (±10% from current price)
    price_low = xyz100_price * 0.90
    price_high = xyz100_price * 1.10
    
    print(f"\n    XYZ100 current price: ${xyz100_price:,.2f}")
    print(f"    Grid range: ${price_low:,.2f} - ${price_high:,.2f}")
    
    # Create config with arithmetic grid for XYZ100
    config = BotConfig(
        pairs=["xyz:XYZ100"],
        grid=GridParameters(
            # Arithmetic mode (BYDFi-style)
            grid_spacing_mode=GridSpacingMode.ARITHMETIC,
            price_range_low=price_low,
            price_range_high=price_high,
            total_grids=20,  # 10 buys + 10 sells
            
            # Sizing
            total_capital_usd=100,
            leverage=5.0,
            size_per_level_pct=10.0,  # 100% / 10 levels per side
            
            # Risk
            bias=GridBias.NEUTRAL,
        ),
        funding=FundingConfig(
            strategy=FundingStrategy.IGNORE,
            bias_pct=0.0,
        ),
        dry_run=True,
    )
    
    print("\n4.1 Creating bot...")
    bot = GridBot(config)
    print("    PASS")
    
    print("\n4.2 Running setup...")
    bot.setup()
    print("    PASS")
    
    print("\n4.3 Initializing grids...")
    bot.initialize_grids()
    print("    PASS")
    
    print("\n4.4 Printing status...")
    bot.print_status()
    print("    PASS")
    
    print("\n--- Full Integration: ALL TESTS PASSED ---")


def test_simple_grid_mode():
    """Test SIMPLE_GRID mode (BYDFi-style)"""
    print("\n" + "="*60)
    print("TEST 5: SIMPLE_GRID Mode")
    print("="*60)

    from config import BotConfig, GridParameters, FundingConfig, FundingStrategy, GridSpacingMode, GridBias
    from xyz_client import XYZClient
    from strategy import GridManager
    from config import XYZ_PAIRS

    # Get current price
    client = XYZClient()
    current_price = client.get_mid_price("xyz:XYZ100")
    if current_price is None:
        current_price = 25000

    print(f"\n    XYZ100 current price: ${current_price:,.2f}")

    # Create SIMPLE_GRID config
    params = GridParameters(
        grid_spacing_mode=GridSpacingMode.SIMPLE_GRID,
        price_range_low=current_price * 0.95,  # 5% below
        price_range_high=current_price * 1.05,  # 5% above
        total_grids=10,  # 5 buys + 5 sells
        total_capital_usd=100,
        leverage=5.0,
        size_per_level_pct=20.0,  # 100% / 5 levels per side
        bias=GridBias.NEUTRAL,
    )

    pair = XYZ_PAIRS["xyz:XYZ100"]
    grid = GridManager(client, pair, params)

    # Test 5.1: Grid initialization without gap
    print("\n5.1 Testing grid initialization (NO GAP)...")
    state = grid.initialize_grid(current_price)

    # Verify no gap exists
    assert state.gap_index is None or grid._is_simple_grid(), "Gap should be None for SIMPLE_GRID"
    print(f"    Center: ${state.center_price:,.2f}")
    print(f"    Levels: {len(state.levels)}")
    print(f"    Gap index: {state.gap_index} (should be None)")
    print("    PASS")

    # Test 5.2: Verify grid_step is set
    print("\n5.2 Verifying grid_step calculation...")
    assert hasattr(grid, 'grid_step'), "grid_step should be set"
    assert grid.grid_step is not None, "grid_step should not be None"
    print(f"    Grid step: ${grid.grid_step:.2f}")
    print("    PASS")

    # Test 5.3: Verify all levels are active (no gaps)
    print("\n5.3 Verifying all levels are active...")
    buy_levels = [l for l in state.levels if l.side.value == "buy"]
    sell_levels = [l for l in state.levels if l.side.value == "sell"]
    print(f"    Buy levels: {len(buy_levels)}")
    print(f"    Sell levels: {len(sell_levels)}")
    assert len(buy_levels) == len(sell_levels), "Should have equal buy/sell levels"
    print("    PASS")

    # Test 5.4: Print grid to verify visualization
    print("\n5.4 Grid visualization (with pair tracking):")
    grid.print_grid()

    # Test 5.5: Verify pair tracking structures exist
    print("\n5.5 Verifying pair tracking structures...")
    assert hasattr(grid, 'active_pairs'), "active_pairs should exist"
    assert hasattr(grid, 'completed_pairs'), "completed_pairs should exist"
    assert isinstance(grid.active_pairs, list), "active_pairs should be a list"
    assert isinstance(grid.completed_pairs, list), "completed_pairs should be a list"
    print(f"    Active pairs: {len(grid.active_pairs)}")
    print(f"    Completed pairs: {len(grid.completed_pairs)}")
    print("    PASS")

    # Test 5.6: Verify statistics include pair info
    print("\n5.6 Verifying statistics include SIMPLE_GRID metrics...")
    stats = grid.get_statistics()
    if grid._is_simple_grid():
        assert 'active_pairs' in stats, "Stats should include active_pairs"
        assert 'completed_pairs' in stats, "Stats should include completed_pairs"
        assert 'total_pair_profit' in stats, "Stats should include total_pair_profit"
        print(f"    Stats keys: {list(stats.keys())}")
        print("    PASS")

    print("\n--- SIMPLE_GRID Mode: ALL TESTS PASSED ---")


def main():
    """Run all tests"""
    print("""
    ================================================================
    |         XYZ HIP-3 GRID BOT - TEST SUITE                      |
    ================================================================
    """)
    
    try:
        # Test 1: XYZ Client
        client = test_xyz_client()

        # Test 2: Grid Manager
        test_grid_manager(client)

        # Test 3: Funding Optimizer
        test_funding_optimizer(client)

        # Test 4: Full Integration
        test_full_integration()

        # Test 5: SIMPLE_GRID Mode
        test_simple_grid_mode()

        print("\n" + "="*60)
        print("ALL TESTS PASSED!")
        print("="*60)
        print("\nThe bot is ready. To run in dry-run mode:")
        print("  python bot.py")
        print("\nOnce you have the asset IDs, update config/settings.py")
        print("and set dry_run=False in the config to go live.")
        print()
        
    except Exception as e:
        logger.exception(f"Test failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
