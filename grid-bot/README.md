# XYZ HIP-3 Grid Trading Bot

A leveraged grid trading bot for HyperLiquid's XYZ HIP-3 perpetual markets with three grid strategies: **Simple Grid** (BYDFi-style), **Arithmetic Grid**, and **Percent Grid**. Includes optional funding rate optimization.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure credentials
# Create .env.hyperliquid with your wallet address and private key

# 3. Run in dry-run mode (simulated trading, no real orders)
python bot.py

# 4. For live trading:
#    a. Set dry_run=False in config/settings.py
#    b. Update price_range in settings for current market price
#    c. Start with small capital ($25-100) and monitor closely
```

---

## Why XYZ HIP-3?

| Feature | Standard Perps | XYZ HIP-3 |
|---------|---------------|-----------|
| Funding Period | 8 hours | **1 hour** (24x/day!) |
| Maker Fee | 0.015% | **0.003%** |
| Taker Fee | 0.045% | **0.009%** |
| Assets | Crypto only | **Equities, Commodities, FX** |

Lower fees and hourly funding make XYZ pairs ideal for grid strategies.

**Available pairs (36 total):**
- **Indices**: XYZ100
- **Equities**: TSLA, NVDA, META, AAPL, GOOGL, AMZN, MSFT, INTC, AMD, ORCL, COIN, HOOD, PLTR, MU, SNDK, MSTR, CRCL, NFLX, COST, LLY, SKHX, TSM, RIVN, BABA, SMSN
- **Commodities**: GOLD, SILVER, COPPER, CL (Crude), NATGAS, URANIUM, ALUMINIUM, PLATINUM
- **FX**: JPY, EUR

---

## Grid Modes

The bot supports three grid trading strategies:

### 1. SIMPLE_GRID (BYDFi-Style) ⭐ DEFAULT

**How it works:**
- All grid levels are active at once (no gap)
- Independent buy/sell pairs
- Simple counter-orders: `price ± grid_step`
- Can have multiple positions simultaneously

```
SELL @ $26,463  ← Level +4
SELL @ $26,209  ← Level +3
SELL @ $25,954  ← Level +2
SELL @ $25,700  ← Level +1
--- Current: $25,445 ---
BUY  @ $25,191  ← Level -1
BUY  @ $24,937  ← Level -2
BUY  @ $24,682  ← Level -3
BUY  @ $24,427  ← Level -4
```

**Best for:** High volatility, frequent oscillations

### 2. ARITHMETIC (Fixed Price Steps)

**How it works:**
- Fixed dollar spacing within a defined price range
- One level is kept as a "gap" (no order)
- When a level fills, the gap moves to maintain balance

**Best for:** Defined trading ranges, mean reversion strategies

### 3. PERCENT (Geometric Spacing)

**How it works:**
- Percentage-based spacing from center price
- Grid follows price movements
- Rebalances when price drifts significantly

**Best for:** Trending markets, long-term holds

---

## Configuration

All settings in `config/settings.py`:

### SIMPLE_GRID Mode (Default)

**Tested Configuration (Jan 2026):**

```python
GridParameters(
    # ===== SIMPLE GRID MODE =====
    grid_spacing_mode=GridSpacingMode.SIMPLE_GRID,
    price_range_low=22392.0,     # ~12% below center ($25,446 * 0.88)
    price_range_high=28500.0,    # ~12% above center ($25,446 * 1.12)
    total_grids=4,               # 2 buys + 2 sells (conservative for $25 capital)

    # ===== POSITION SIZING =====
    total_capital_usd=25,        # Small capital for initial testing
    leverage=2.0,                # Ultra-conservative 2x leverage
    size_per_level_pct=50.0,     # NOT used in SIMPLE_GRID (kept for other modes)

    # ===== RISK =====
    max_position_pct=50.0,       # Max 50% of notional on one side
    bias=GridBias.NEUTRAL,       # True neutral grid
)
```

**Sizing Logic for SIMPLE_GRID:**
- Total notional = `capital * leverage` = $25 * 2 = $50
- **Per-order size = total notional / total_grids** = $50 / 4 = **$12.50 per order**
- NOT percentage-based (size_per_level_pct is ignored)
- Orders are evenly distributed across all grid levels

**⚠️ CRITICAL for ARITHMETIC/SIMPLE_GRID modes:**
- Always update `price_range_low` and `price_range_high` based on current price
- Bot will ERROR on startup if current price is >5% outside your range
- Check current price at https://app.hyperliquid.xyz/trade/xyz:XYZ100

### Safety Settings

```python
dry_run=True               # IMPORTANT: Start in dry run mode!
max_position_pct=50.0      # Max 50% of capital in one direction
margin_buffer_pct=30.0     # Keep 30% margin buffer
leverage=2.0               # Start with 2x (can increase after testing)
total_grids=4              # Start with 4 grids for $25-50 capital
```

**Margin Requirements:**
- With 4 grids, 2x leverage, $25 capital → $12.50 per order
- Worst case (all orders on one side fill): $25 notional = $12.50 margin required
- **Margin utilization: 50%** (safe buffer remaining)

---

## Installation & Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Set Credentials

Create `.env.hyperliquid` file:
```bash
HL_WALLET_ADDRESS=0xYourWalletAddress
HL_PRIVATE_KEY=0xYourPrivateKey
```

**⚠️ IMPORTANT:** Add `.env.hyperliquid` to `.gitignore` to avoid committing credentials!

### 3. Test in Dry Run Mode

```bash
python bot.py
```

The bot starts in `dry_run=True` mode - no real orders are placed.

### 4. Go Live

**Step 1:** In `config/settings.py`:
```python
dry_run=False  # Enable live trading
```

**Step 2:** Start with conservative settings:
```python
total_capital_usd=25    # Small capital for initial testing
leverage=2.0            # Ultra-conservative (increase after testing)
total_grids=4           # 2 buys + 2 sells (minimum for testing)
```

**Step 3:** Update price range for current market:
```python
# Get current XYZ100 price from: https://app.hyperliquid.xyz/trade/xyz:XYZ100
current_price = 25500  # Example
price_range_low = current_price * 0.88   # 12% below
price_range_high = current_price * 1.12  # 12% above
```

**⚠️ WARNING:** Live mode places REAL orders with REAL money!

---

## Technical Implementation

### XYZ HIP-3 Order Placement

XYZ orders use a custom implementation that bypasses the HyperLiquid SDK:

**Why?** The SDK doesn't include XYZ assets in its `name_to_coin` mapping.

**How it works:**
1. Uses raw API calls to `/exchange` endpoint
2. Short field names: `a` (asset), `b` (isBuy), `p` (price), `s` (size), `r` (reduceOnly), `t` (type)
3. Asset IDs calculated as: `100000 + (perp_dex_index * 10000) + index_in_meta`
4. Includes `expiresAfter` timestamp for signature validation
5. Prices automatically rounded to tick size (e.g., whole dollars for XYZ100)

**Example:**
```python
# xyz:XYZ100 order
{
    "a": 110000,              # Asset ID
    "b": True,                # Buy
    "p": "25475",             # Price (whole dollars)
    "s": "0.0004",            # Size
    "r": False,               # Not reduce-only
    "t": {"limit": {"tif": "Gtc"}}
}
```

### Fill Detection

The bot uses **REST API polling** to detect fills:

**How it works:**
1. Every **30 seconds**, bot queries HyperLiquid: `info.open_orders(wallet_address)`
2. Compares current open orders against internal grid state
3. **If order ID missing from API response → FILLED**
4. **If order size decreased → PARTIAL FILL**

**Trade-offs:**
- ✅ Simple and reliable
- ✅ No websocket complexity
- ❌ 30-second latency (not real-time)
- ❌ May miss quick price reversals

**Note:** 30 seconds is reasonable for grid trading. Orders are limit orders designed to wait for favorable fills.

### SIMPLE_GRID Counter-Order Logic (v2.1 - Jan 2026)

**How it works:**

1. **Buy fills** → Place sell counter at `buy_price + grid_step`
   - Safety check: Ensure counter-sell ≥ market × 1.001 (0.1% above market)
   - Prevents immediate fill at unfavorable price
   - Creates an "active pair" (long position waiting to close)

2. **Sell fills** → Check if closing a long pair or opening new short
   - If closing long: Calculate profit and complete pair (no counter-order)
   - If opening short: Place buy counter at `sell_price - grid_step`
   - Safety check: Ensure counter-buy ≤ market × 0.999 (0.1% below market)

3. **Pair Matching** → Match by size (more reliable than price for crossed-market fills)

4. **Grid Replenishment** → Grid levels only replenish when ALL active pairs close
   - Prevents cascade where every fill creates new positions that immediately fill
   - Grid naturally depletes as trades happen
   - Rebalance/restart bot to reset grid

**Bug Fixes (v2.0 → v2.1):**
- ✅ Fixed infinite fill loop (orders cleared from levels immediately after detection)
- ✅ Fixed order duplication (removed double replacement + counter logic)
- ✅ Fixed counter-orders crossing market (safety margins enforced)
- ✅ Fixed profit calculation (size-based matching, handles both long and short pairs)
- ✅ Fixed "Could not find level" errors (level reference passed directly to handler)
- ✅ Added exchange order count verification (heartbeat shows ACTUAL orders, not internal state)
- ✅ Improved leverage setting for XYZ pairs (uses asset ID directly)

### Asset IDs

Confirmed working asset IDs:

| Asset | Asset ID | Tick Size |
|-------|----------|-----------|
| xyz:XYZ100 | 110000 | $1.00 |
| xyz:TSLA | 110001 | $0.01 |
| xyz:NVDA | 110002 | $0.01 |
| xyz:GOLD | 110003 | $0.10 |
| xyz:SILVER | 110026 | $0.01 |

Formula: `asset_id = 100000 + (1 * 10000) + index_in_meta` where XYZ `perp_dex_index = 1`

---

## Architecture

```
grid-bot/
├── bot.py                    # Main entry point
├── trade_logger.py           # CSV trade history
├── test_bot.py               # Test suite
├── config/
│   └── settings.py           # Configuration (all grid modes, asset IDs)
├── xyz_client/
│   ├── client.py             # XYZ API client (raw API implementation)
│   └── models.py             # Data models
└── strategy/
    ├── grid_manager.py       # Grid logic (all 3 modes)
    └── funding_optimizer.py  # Funding rate harvesting
```

---

## Profit Model

### Grid Trading Profits

**SIMPLE_GRID (Tested Configuration):**
- Each pair captures: `grid_step - fees`
- 4 grids, $6,118 range, step = $1,529.50
- Fees: 0.006% round-trip (0.003% maker each way)
- **Profit per pair: ~$1,529.40** (before considering market movement)
- With $25 capital, 2x leverage: Can run 2 pairs simultaneously
- Target: 1-2 cycles per day in volatile markets

**Example:**
```
BUY @ $23,964 → SELL @ $25,494 (counter-order fills at $25,494)
Profit = ($25,494 - $23,964) * 0.0005 = $0.765
Fees = $25,494 * 0.0005 * 0.00006 = $0.0008
Net = $0.764 per cycle
```

**ARITHMETIC:**
- Each round trip: `grid_step - fees`
- 48 grids in 24% range: step ≈ 0.5%
- Profit: ~0.49% after fees

### Funding Collection (Optional)

XYZ funding settles **hourly** (24x/day):

**Example (GOLD @ 0.02%/hour):**
- $5,000 short position
- Hourly: $5,000 × 0.02% = $1.00
- Daily: $1 × 24 = $24
- APR: ~175%

*Results vary based on volatility and market conditions.*

---

## Safety Features

✅ **Dry run mode** - Default, test without risk
✅ **Price range validation** - Errors if price outside range
✅ **Position limits** - Configurable max position size
✅ **Margin buffer** - 30% buffer prevents liquidation
✅ **Order retry logic** - 3 retries with exponential backoff
✅ **Position reconciliation** - Auto-sync with exchange every 5 min
✅ **Exchange order verification** - Heartbeat shows ACTUAL orders on exchange
✅ **Order count mismatch warnings** - Alerts when internal state differs from exchange
✅ **Cascade prevention** - SIMPLE_GRID won't auto-replenish while pairs active
✅ **Trade logging** - Complete CSV history
✅ **Isolated margin** - Risk limited per position
✅ **Tick size enforcement** - Prices automatically rounded

---

## Production Status

### ✅ Confirmed Working (Jan 2026 - v2.1)

- XYZ order placement (tested with xyz:XYZ100)
- SIMPLE_GRID mode with cascade prevention
- Asset ID calculations verified (110000-110035)
- Tick size handling (whole dollars for XYZ100)
- Fill detection via REST API polling (30s interval)
- Counter-order placement with safety margins
- Exchange order count verification
- Position reconciliation
- Trade history CSV export
- Rate limiting (30 orders/min)
- Margin safety checks
- Leverage setting via asset ID (for XYZ pairs)
- **Bug fixes:** Cascade prevention, level tracking, heartbeat accuracy, balance/leverage APIs

### Before Going Live

⚠️ **Clean slate first:**
- Cancel ALL open orders on HyperLiquid
- Close any existing positions
- Start fresh

⚠️ **Update price range** for current market price:
- Get price from: https://app.hyperliquid.xyz/trade/xyz:XYZ100
- Set `price_range_low = current_price * 0.88`
- Set `price_range_high = current_price * 1.12`

⚠️ **Start conservative:**
- Capital: $25-50 for initial testing
- Leverage: **2x** (not 5-10x!)
- Grids: **4 levels** (2 buys + 2 sells)
- Set `dry_run=False` in config/settings.py

⚠️ **Monitor the logs:**
- Startup: Should show "Placed 4 initial orders" with IDs
- Heartbeat: Should show "Orders: 4 on exchange"
- If mismatch warning appears, investigate before continuing
- Kill immediately if order count exceeds expected

⚠️ **Check credentials:** `.env.hyperliquid` configured correctly

---

## Important Notes

### Trading
- XYZ pairs use **isolated margin only**
- Funding settles **every hour** (24x/day)
- Order placement uses raw API calls (not SDK)
- **Current config: dry_run=False** (live trading enabled by default)
- Fill detection: 30-second REST API polling (not real-time)

### Data & Monitoring
- REST API polling (5-second price updates)
- Order checks every 30 seconds
- Position reconciliation every 5 minutes
- Heartbeat logs every 30 seconds (shows actual exchange order count)
- Startup log shows all placed orders with IDs
- Order count mismatch warnings with specific missing IDs
- Automatic CSV trade history

### Risk Management
- Price range validation on startup
- Position limits enforced
- Margin buffer prevents liquidation
- Order retry with exponential backoff
- Position reconciliation detects missed fills

### Environment
- Credentials from `.env.hyperliquid`
- **Never commit credentials to git!**
- Add to `.gitignore`

---

## Choosing a Grid Mode

**Use SIMPLE_GRID when:**
- High volatility expected
- Want multiple simultaneous positions
- BYDFi-style independent pair tracking

**Use ARITHMETIC when:**
- Trading in a defined range
- Mean reversion strategy
- Fixed dollar spacing preferred

**Use PERCENT when:**
- Trending markets
- Long-term holds
- Want grid to follow price

---

## Changelog

### v2.1 (Jan 25, 2026) - Cascade Fix & Diagnostics

**Critical Fix:**
- **Order cascade prevention** - Grid no longer auto-replenishes while pairs are active
  - Root cause: Every fill was creating replacement + counter orders (2x multiplication)
  - Now: Only counter-orders are placed; grid replenishes when ALL pairs close
  - Prevents the 4 → 8 → 12 → 20+ order explosion

**Bug Fixes:**
1. **"Could not find level" errors** - Level reference now passed directly to `handle_fill()`
2. **Heartbeat accuracy** - Now queries exchange for ACTUAL order count (not internal state)
3. **Account balance** - Fixed API parsing for XYZ accounts (tries multiple response formats)
4. **Leverage setting** - Now uses asset ID for XYZ pairs (SDK doesn't recognize xyz: prefix)

**Diagnostic Improvements:**
- Startup log shows all placed orders with IDs
- Order count mismatch warnings show specific missing IDs
- Better fill detection logging (shows order ID and exchange state)
- Order placement logs show response status

**Behavioral Change:**
- SIMPLE_GRID will NOT auto-replenish grid levels while there are active pairs
- Grid naturally depletes as trades happen
- To reset: Stop bot, cancel all orders, restart

---

### v2.0 (Jan 24, 2026) - Initial Bug Fixes

**Major Changes:**
- Fixed SIMPLE_GRID sizing calculation (now divides evenly: `notional / total_grids`)
- Reduced default leverage: 10x → **2x** (more conservative)
- Reduced default grids: 10 → **4** (for $25-50 capital accounts)

**Bug Fixes:**
1. **Infinite fill loop** - Orders no longer detected as filled repeatedly
2. **Counter-orders crossing market** - Added safety margins (0.1% buffer)
3. **Profit calculation** - Fixed pair matching (size-based, not price-based)

**Tested Configuration:**
- Capital: $25 USD
- Leverage: 2x
- Grid: 4 levels (2 buys + 2 sells)
- Per-order size: $12.50 notional
- Margin utilization: 50%

---

## Support

For issues or questions:
- Check logs in `logs/grid_bot_*.log`
- Verify `.env.hyperliquid` credentials
- Review trade history CSV in bot directory
- Start with $25-50 capital and 2x leverage

**Remember:** This bot trades with real money. Monitor closely during first few cycles!
