"""
Market Making Economics Model for HIP-3 Pairs

Fee Structure (HIP-3 Growth Mode = 90% reduction):
- Base taker fee: 0.045% → 0.0045% (HIP-3)
- Maker rebates (based on maker volume %):
  - >0.5% maker: -0.001% → -0.0001% (HIP-3)
  - >1.5% maker: -0.002% → -0.0002% (HIP-3)
  - >3.0% maker: -0.003% → -0.0003% (HIP-3)

Market Making Strategy:
1. Place limit buy at bid (or slightly above)
2. Place limit sell at ask (or slightly below)
3. Capture spread when both fill
4. Collect maker rebate on both sides
"""

class MarketMakingModel:
    def __init__(self, maker_rebate_pct=0.0001):
        """
        Args:
            maker_rebate_pct: Maker rebate as decimal (0.0001 = 0.01%)
                            Default assumes low tier: -0.0001% (we GET paid this)
        """
        self.maker_rebate_pct = maker_rebate_pct

    def calculate_profit(self, entry_price, exit_price, position_size, spread_pct=None):
        """
        Calculate profit for one market making round trip.

        Args:
            entry_price: Buy price (at or near bid)
            exit_price: Sell price (at or near ask)
            position_size: Size in base asset (e.g., 1.0 oz of Gold)
            spread_pct: Spread as percentage (optional, calculated if not provided)

        Returns:
            dict with breakdown
        """
        # Calculate spread
        if spread_pct is None:
            spread_pct = (exit_price - entry_price) / entry_price

        # Notional values
        buy_notional = entry_price * position_size
        sell_notional = exit_price * position_size

        # Spread profit
        spread_profit = sell_notional - buy_notional

        # Maker rebates (negative fees = we get paid)
        buy_rebate = buy_notional * self.maker_rebate_pct
        sell_rebate = sell_notional * self.maker_rebate_pct
        total_rebate = buy_rebate + sell_rebate

        # Total profit
        total_profit = spread_profit + total_rebate
        total_profit_pct = total_profit / buy_notional

        return {
            "entry_price": entry_price,
            "exit_price": exit_price,
            "position_size": position_size,
            "spread_pct": spread_pct * 100,
            "spread_profit": spread_profit,
            "buy_rebate": buy_rebate,
            "sell_rebate": sell_rebate,
            "total_rebate": total_rebate,
            "total_profit": total_profit,
            "total_profit_pct": total_profit_pct * 100,
            "profit_per_round_trip_bps": total_profit_pct * 10000
        }

    def min_profitable_spread(self):
        """
        Calculate minimum spread needed to break even.
        With maker rebates, we actually profit even at 0 spread!
        """
        # We need spread to overcome... nothing, since rebates are positive
        # But in practice, we need SOME spread to account for risk
        return 0.0

    def calculate_inventory_loss(self, entry_price, adverse_move_bps, position_size,
                                  exit_as_taker=False):
        """
        Calculate loss when only one side fills and price moves against us.

        Args:
            entry_price: Price we got filled at (one side)
            adverse_move_bps: How many bps price moved against us
            position_size: Size in base asset
            exit_as_taker: If True, pay taker fee to exit (0.0045% for HIP-3)
                          If False, wait for maker fill (get rebate but hold inventory longer)

        Returns:
            dict with loss breakdown
        """
        # HIP-3 taker fee if we need to exit fast
        taker_fee_pct = 0.000045 if exit_as_taker else 0.0

        # Adverse move
        adverse_move_pct = adverse_move_bps / 10000
        exit_price = entry_price * (1 - adverse_move_pct)  # Price moved against us

        # Calculate P&L
        buy_notional = entry_price * position_size
        sell_notional = exit_price * position_size

        position_loss = sell_notional - buy_notional

        # Entry rebate (we got paid for the first leg)
        entry_rebate = buy_notional * self.maker_rebate_pct

        # Exit fee/rebate
        if exit_as_taker:
            exit_fee = sell_notional * taker_fee_pct
            exit_rebate = 0
        else:
            exit_fee = 0
            exit_rebate = sell_notional * self.maker_rebate_pct

        # Total loss
        total_loss = position_loss + entry_rebate + exit_rebate - exit_fee
        total_loss_pct = total_loss / buy_notional

        return {
            "entry_price": entry_price,
            "exit_price": exit_price,
            "adverse_move_bps": adverse_move_bps,
            "position_loss": position_loss,
            "entry_rebate": entry_rebate,
            "exit_rebate": exit_rebate,
            "exit_fee": exit_fee,
            "total_loss": total_loss,
            "total_loss_pct": total_loss_pct * 100,
            "exit_as_taker": exit_as_taker
        }

    def expected_value(self, spread_bps, prob_both_fill, adverse_move_bps,
                       position_size_usd, avg_price):
        """
        Calculate expected value accounting for both scenarios:
        1. Both sides fill (profit)
        2. One side fills, adverse move (loss)

        Args:
            spread_bps: Expected spread capture in bps
            prob_both_fill: Probability both sides fill (0-1)
            adverse_move_bps: Average adverse move when only one side fills
            position_size_usd: Position size in USD
            avg_price: Average price per unit
        """
        position_size = position_size_usd / avg_price

        # Scenario 1: Both fill (profit)
        spread_pct = spread_bps / 10000
        profit_result = self.calculate_profit(
            entry_price=avg_price * (1 - spread_pct/2),
            exit_price=avg_price * (1 + spread_pct/2),
            position_size=position_size
        )
        profit_both_fill = profit_result["total_profit"]

        # Scenario 2: One fills, adverse move (loss)
        loss_result = self.calculate_inventory_loss(
            entry_price=avg_price,
            adverse_move_bps=adverse_move_bps,
            position_size=position_size,
            exit_as_taker=True  # Exit fast to minimize risk
        )
        loss_one_fill = loss_result["total_loss"]

        # Expected value
        prob_one_fill = 1 - prob_both_fill
        ev = (prob_both_fill * profit_both_fill) + (prob_one_fill * loss_one_fill)
        ev_pct = (ev / position_size_usd) * 100

        return {
            "spread_bps": spread_bps,
            "prob_both_fill": prob_both_fill,
            "prob_one_fill": prob_one_fill,
            "profit_both_fill": profit_both_fill,
            "loss_one_fill": loss_one_fill,
            "expected_value": ev,
            "expected_value_pct": ev_pct,
            "ev_per_trade_bps": ev_pct * 100,
            "breakeven": ev >= 0
        }

    def breakeven_fill_rate(self, spread_bps, adverse_move_bps, position_size_usd, avg_price):
        """
        Calculate minimum probability of both-side fills needed to break even.
        """
        position_size = position_size_usd / avg_price

        # Profit when both fill
        spread_pct = spread_bps / 10000
        profit_result = self.calculate_profit(
            entry_price=avg_price * (1 - spread_pct/2),
            exit_price=avg_price * (1 + spread_pct/2),
            position_size=position_size
        )
        profit = profit_result["total_profit"]

        # Loss when one fills
        loss_result = self.calculate_inventory_loss(
            entry_price=avg_price,
            adverse_move_bps=adverse_move_bps,
            position_size=position_size,
            exit_as_taker=True
        )
        loss = abs(loss_result["total_loss"])

        # Breakeven: p * profit = (1-p) * loss
        # p * profit = loss - p * loss
        # p * (profit + loss) = loss
        # p = loss / (profit + loss)

        if profit + loss == 0:
            return None  # Can't break even

        breakeven_prob = loss / (profit + loss)

        return {
            "profit_both_fill": profit,
            "loss_one_fill": -loss,
            "breakeven_fill_rate": breakeven_prob,
            "breakeven_fill_rate_pct": breakeven_prob * 100
        }

    def daily_profit_estimate(self, avg_spread_pct, trades_per_day, position_size_usd, avg_price=1.0):
        """
        Estimate daily profit from market making.

        Args:
            avg_spread_pct: Average spread captured (as decimal, e.g., 0.001 = 0.1%)
            trades_per_day: Number of round trips per day
            position_size_usd: Position size in USD
            avg_price: Average price per unit
        """
        position_size = position_size_usd / avg_price

        # Single round trip
        result = self.calculate_profit(
            entry_price=avg_price * (1 - avg_spread_pct/2),
            exit_price=avg_price * (1 + avg_spread_pct/2),
            position_size=position_size
        )

        # Daily totals
        daily_profit = result["total_profit"] * trades_per_day
        daily_profit_pct = (daily_profit / position_size_usd) * 100

        return {
            "profit_per_round_trip": result["total_profit"],
            "profit_per_round_trip_pct": result["total_profit_pct"],
            "trades_per_day": trades_per_day,
            "daily_profit": daily_profit,
            "daily_profit_pct": daily_profit_pct,
            "monthly_profit": daily_profit * 30,
            "monthly_profit_pct": daily_profit_pct * 30
        }


def print_model():
    """Run scenarios for market making on HIP-3 pairs"""

    print("=" * 80)
    print("MARKET MAKING MODEL - HIP-3 PAIRS (Gold, Silver, XYZ100)")
    print("=" * 80)
    print()

    # Scenario 1: Conservative (low maker tier)
    print("SCENARIO 1: Low Maker Tier (-0.0001% rebate)")
    print("-" * 80)
    model = MarketMakingModel(maker_rebate_pct=0.000001)  # -0.0001%

    # Example: Gold at ~$2700, 0.01 oz position = ~$27
    result = model.calculate_profit(
        entry_price=2700.00,
        exit_price=2700.50,
        position_size=0.01  # 0.01 oz Gold
    )

    print(f"Entry: ${result['entry_price']:.2f}")
    print(f"Exit:  ${result['exit_price']:.2f}")
    print(f"Size:  {result['position_size']:.4f} oz (~${result['entry_price'] * result['position_size']:.2f})")
    print(f"Spread: {result['spread_pct']:.4f}% (${result['spread_profit']:.4f})")
    print(f"Maker rebates: ${result['total_rebate']:.6f}")
    print(f"Total profit: ${result['total_profit']:.4f} ({result['total_profit_pct']:.4f}%)")
    print(f"Profit: {result['profit_per_round_trip_bps']:.2f} bps")
    print()

    # Daily estimates
    print("Daily Profit Estimates (varying frequency):")
    print("-" * 80)
    for trades in [10, 20, 50, 100]:
        daily = model.daily_profit_estimate(
            avg_spread_pct=0.0002,  # 0.02% average spread (2 bps)
            trades_per_day=trades,
            position_size_usd=27,
            avg_price=2700
        )
        print(f"{trades:3d} trades/day: ${daily['daily_profit']:.2f}/day "
              f"({daily['daily_profit_pct']:.2f}%/day, "
              f"${daily['monthly_profit']:.2f}/month)")
    print()

    # Scenario 2: Higher maker tier
    print("SCENARIO 2: High Maker Tier (-0.0003% rebate)")
    print("-" * 80)
    model2 = MarketMakingModel(maker_rebate_pct=0.000003)  # -0.0003%

    result2 = model2.calculate_profit(
        entry_price=2700.00,
        exit_price=2700.50,
        position_size=0.01
    )

    print(f"Same trade with better rebate tier:")
    print(f"Total profit: ${result2['total_profit']:.4f} ({result2['total_profit_pct']:.4f}%)")
    print(f"Profit: {result2['profit_per_round_trip_bps']:.2f} bps")
    print(f"Improvement: +${result2['total_profit'] - result['total_profit']:.6f} per trade")
    print()

    # Scenario 3: Break-even analysis
    print("BREAK-EVEN ANALYSIS")
    print("-" * 80)
    print(f"Minimum spread needed: {model.min_profitable_spread():.4f}%")
    print("(With maker rebates, even 0 spread is profitable!)")
    print()

    # Risk scenarios
    print("RISK SCENARIOS")
    print("-" * 80)

    # Adverse selection (price moves against us)
    adverse = model.calculate_profit(
        entry_price=2700.00,
        exit_price=2699.50,  # Price moved down, we lose on position
        position_size=0.01
    )
    print(f"Adverse move (-$0.50): ${adverse['total_profit']:.4f} loss")
    print()

    # Different spread scenarios
    print("SPREAD SENSITIVITY")
    print("-" * 80)
    for spread_bps in [1, 2, 5, 10, 20]:
        spread_pct = spread_bps / 10000
        r = model.calculate_profit(
            entry_price=2700.00,
            exit_price=2700.00 * (1 + spread_pct),
            position_size=0.01
        )
        print(f"{spread_bps:2d} bps spread: ${r['total_profit']:.4f} profit "
              f"({r['total_profit_pct']:.4f}%, {r['profit_per_round_trip_bps']:.2f} bps)")
    print()

    # NEW: Inventory risk analysis
    print("\n")
    print("=" * 80)
    print("INVENTORY RISK ANALYSIS")
    print("=" * 80)
    print("What happens when only ONE side fills and price moves against us?")
    print("-" * 80)

    # Example: Silver at $32, 0.3125 oz = $10 position
    silver_price = 32.00
    silver_size = 10 / silver_price  # 0.3125 oz for $10 position

    for adverse_bps in [5, 10, 20, 50]:
        loss = model.calculate_inventory_loss(
            entry_price=silver_price,
            adverse_move_bps=adverse_bps,
            position_size=silver_size,
            exit_as_taker=True
        )
        print(f"{adverse_bps:2d} bps adverse move: ${loss['total_loss']:.4f} loss "
              f"({loss['total_loss_pct']:.3f}%)")
    print()

    # Expected value scenarios
    print("=" * 80)
    print("EXPECTED VALUE ANALYSIS")
    print("=" * 80)
    print("Does this strategy have positive EV?")
    print("Assumptions: 2 bps spread, 10 bps average adverse move")
    print("-" * 80)

    for fill_rate in [0.50, 0.60, 0.70, 0.80, 0.90, 0.95]:
        ev = model.expected_value(
            spread_bps=2,
            prob_both_fill=fill_rate,
            adverse_move_bps=10,
            position_size_usd=10,
            avg_price=silver_price
        )
        status = "[+] POSITIVE" if ev["breakeven"] else "[-] NEGATIVE"
        print(f"Fill rate {fill_rate*100:5.1f}%: EV = ${ev['expected_value']:+.4f} "
              f"({ev['expected_value_pct']:+.3f}%) {status}")
    print()

    # Breakeven analysis
    print("=" * 80)
    print("BREAKEVEN FILL RATE")
    print("=" * 80)
    print("Minimum % of time both sides must fill to be profitable")
    print("-" * 80)

    for spread_bps in [1, 2, 5, 10]:
        for adverse_bps in [5, 10, 20]:
            be = model.breakeven_fill_rate(
                spread_bps=spread_bps,
                adverse_move_bps=adverse_bps,
                position_size_usd=10,
                avg_price=silver_price
            )
            print(f"Spread {spread_bps:2d}bps, Adverse {adverse_bps:2d}bps: "
                  f"Need {be['breakeven_fill_rate_pct']:5.1f}% both-side fills")
    print()

    print("=" * 80)
    print("KEY INSIGHTS")
    print("=" * 80)
    print("1. Maker rebates make even tight spreads profitable (when both fill)")
    print("2. Inventory risk is the REAL challenge - one-side fills can wipe out profits")
    print("3. Need 70-90% both-side fill rate to be profitable (depends on spread)")
    print("4. Wider spreads = lower breakeven fill rate needed")
    print("5. Fast repricing is CRITICAL to minimize adverse moves")
    print("6. Consider: place orders INSIDE spread to increase both-side fill rate")
    print("=" * 80)


if __name__ == "__main__":
    print_model()
