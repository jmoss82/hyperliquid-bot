# HIP-3 Market Maker

Automated market making bot for HyperLiquid HIP-3 pairs using **WMA trend-based one-sided entries**, momentum positioning, strict flat-state gating, and immediate fast exits.

---

## Quick Start

### 1. Check Spread Viability

```bash
python spread_monitor.py
```

### 2. Run the Bot

```bash
python market_maker.py
```

---

## Files

```
LFG-bot/
├── market_maker.py      # Main trading bot (WMA trend-based)
├── candle_builder.py    # 5-second candle builder and WMA calculator
├── wma_tester.py        # Live WMA signal testing (no real orders)
├── account_monitor.py   # Position/balance monitoring
├── spread_monitor.py    # Spread analysis tool
├── lfg_config.py        # Credentials loader
└── requirements.txt     # Dependencies
```

| File | Purpose |
|------|---------|
| `market_maker.py` | Primary - WMA trend-based one-sided market maker |
| `candle_builder.py` | Core - Builds 5s candles from tick data, calculates WMA |
| `wma_tester.py` | Testing - Live trend signal monitoring without real trades |
| `account_monitor.py` | Monitoring - real-time position/balance tracking |
| `spread_monitor.py` | Analysis - pre-trade spread assessment |
| `lfg_config.py` | Loads credentials from `../grid-bot/.env.hyperliquid` |

---

## Strategy (WMA Trend-Based with Fast Exits)

```
1. ENSURE FLAT
   ├── Cancel all open orders (verified)
   ├── Exit any positions (taker)
   └── Verify: 0 orders, 0 positions

2. BUILD CANDLES & DETECT TREND
   ├── 5-second OHLC candles from streaming bid/ask
   ├── Calculate 10-period WMA using (H+L+C+C)/4
   └── Determine trend: UP / DOWN / FLAT / UNKNOWN

3. DETECT OPPORTUNITY
   ├── Spread > 6 bps? → Continue
   ├── Trend = UP or DOWN? → Continue
   └── Trend = FLAT or UNKNOWN? → Skip

4. PLACE ONE-SIDED MAKER ORDER
   ├── Trend UP → Place BUY (closer to ask, post-only)
   └── Trend DOWN → Place SELL (closer to bid, post-only)

5. MONITOR ENTRY (~1s)
   ├── Order fills → Start position monitoring
   └── Timeout → Cancel order

6. MONITOR POSITION (Fast Exits - ALL TAKER)
   ├── Stop Loss: P&L <= -7 bps → Taker exit (immediate)
   ├── Take Profit: P&L >= +20 bps → Taker exit (immediate)
   └── Max Hold Time: > 120s → Taker exit (immediate)

7. REPEAT (5s cooldown)
```

Key principles:
- **WMA trend detection** (10-period on 5-second candles using weighted close)
- **One-sided directional entries** (LONG on uptrend, SHORT on downtrend)
- **Momentum positioning** (BUY near ask, SELL near bid)
- **Maker entries** (post-only to collect rebates)
- **Fast taker exits** for all conditions (immediate execution)
- Verified cancels (poll until gone)
- Quote freshness gate to prevent stale placements

---

## Configuration (Current)

Edit `market_maker.py` main() function:

```python
mm = MarketMaker(
    coin="xyz:SILVER",
    spread_threshold_bps=6.0,       # Only trade when spread > 6 bps
    position_size_usd=11.0,         # $11 per trade
    spread_position=0.2,            # 20% into spread (closer to edges)
    max_patience_ms=300,            # 300ms patience (legacy, not used)
    max_positions=1,                # One at a time
    max_trades=999999,              # No practical limit
    max_loss=5.0,                   # Stop if lose $5
    min_trade_interval=5.0,         # 5 second cooldown between trades
    dry_run=False,                  # LIVE MODE
    max_quote_age_ms=1200.0,        # Speed-prioritized freshness gate
    ws_stale_timeout_s=15.0         # Reduce REST refresh frequency
)
```

### WMA Trend Detection (Configured in __init__)

| Parameter | Current Value | Description |
|-----------|---------------|-------------|
| `wma_period` | 10 | Number of candles for WMA calculation |
| `wma_price_type` | `weighted_close` | Uses (H+L+C+C)/4 for each candle |
| `wma_threshold` | 0.0005 (0.05%) | Buffer to avoid whipsaw on WMA line |
| Candle interval | 5 seconds | OHLC candle frequency |

**Trend Rules:**
- **UP:** Price > WMA + 0.05% threshold → Enter LONG
- **DOWN:** Price < WMA - 0.05% threshold → Enter SHORT
- **FLAT:** Price within ±0.05% of WMA → Skip (no clear trend)
- **UNKNOWN:** Less than 10 candles → Skip (insufficient data)

### Position Management (Fast Exits)

| Parameter | Current Value | Description |
|-----------|---------------|-------------|
| `take_profit_bps` | 20 | Exit at +20 bps profit (taker exit) |
| `stop_loss_bps` | 7 | Exit at -7 bps loss (taker exit) |
| `max_hold_time` | 120s | Max seconds to hold position |
| `position_check_interval` | 0.5s | How often to check exit conditions |

**Exit Priority (checked in order):**
1. **Stop Loss** (-7 bps) → Immediate taker exit
2. **Take Profit** (+20 bps) → Immediate taker exit
3. **Max Hold Time** (120s) → Immediate taker exit

**Exit Method:**
- **All exits are taker:** Cross spread immediately for fast execution (0.2% beyond bid/ask to guarantee fill)

### Additional Controls

| Parameter | Default | Notes |
|-----------|---------|-------|
| `max_quote_age_ms` | 1200 | Max age for a quote before skipping placement |
| `ws_stale_timeout_s` | 15 | WS stale threshold before REST refresh |
| `opportunity_queue_size` | 1 | Prevents backlog of stale opportunities |

### Spread Position (One-Sided Momentum)

With `spread_position=0.2` and spread of $117.00 bid / $117.08 ask:

**When Trend = UP (Enter LONG):**
- BUY placed at: $117.08 - ($0.08 × 0.2) = **$117.064** (20% from ask edge)
- Positioned to fill on upward momentum

**When Trend = DOWN (Enter SHORT):**
- SELL placed at: $117.00 + ($0.08 × 0.2) = **$117.016** (20% from bid edge)
- Positioned to fill on downward momentum

**Strategy:** One-sided orders positioned 20% into the spread from the edge. Closer to edges (20% vs 30%) reduces post-only rejections while still capturing momentum. Wider minimum spread (6+ bps) provides better signal quality and breathing room for post-only orders.

### Credentials

Expected environment variables (loaded by `lfg_config.py`):
- `HL_WALLET_ADDRESS` / `HL_PRIVATE_KEY`
- `HL_ACCOUNT_ADDRESS` (optional)

---

## Testing WMA Signals (No Real Orders)

Use `wma_tester.py` to observe live trend signals without placing real orders:

```bash
cd LFG-bot
python wma_tester.py
```

**What it shows:**
- Real-time 5-second candle building
- WMA calculations and current trend (UP/DOWN/FLAT/UNKNOWN)
- Entry signals that would trigger (🟢 LONG / 🔴 SHORT)
- Trend changes with price vs WMA values
- Signal statistics (LONG vs SHORT distribution)

**Useful for:**
- Validating WMA settings before live trading
- Observing signal quality during different market conditions
- Testing new WMA periods or thresholds
- Understanding when the bot would enter

Press `Ctrl+C` to stop and see statistics.

---

## Latency Notes

- Quote freshness is enforced before placement; stale data is skipped.
- WS staleness triggers a REST refresh as a fallback.
- For lower latency, run from a VPS closer to HyperLiquid infra (SG/Tokyo).

---

## HIP-3 DEX Configuration

**IMPORTANT:** HyperLiquid HIP-3 markets exist on a separate DEX from standard perps.

| Query Type | DEX Parameter | Example |
|------------|---------------|---------|
| Account balance | Default (none) | `user_state(address)` |
| xyz positions | `dex="xyz"` | `user_state(address, dex="xyz")` |
| xyz open orders | `dex="xyz"` | `open_orders(address, dex="xyz")` |

**Without `dex="xyz"`:**
- Positions appear empty
- Open orders appear empty
- Cancels fail silently

---

## Deployment

### GitHub Repository

Repository: `https://github.com/jmoss82/hyperliquid-bot`

The bot is version controlled with Git and deployed via GitHub to Railway for low-latency execution.

### Railway Deployment

The bot runs on Railway to minimize latency compared to local execution (~200ms vs ~700ms).

**Initial Setup:**

1. Install Railway CLI:
   ```bash
   npm install -g @railway/cli
   ```

2. Login to Railway:
   ```bash
   railway login
   ```

3. Link to project (from repo root):
   ```bash
   cd "Project Money 2"
   railway link
   ```
   Select: `hyperliquid-bot`

**Environment Variables (Railway Dashboard):**

Set these in Railway → Project → Variables:
```
HL_WALLET_ADDRESS=0x...
HL_PRIVATE_KEY=0x...
HL_ACCOUNT_ADDRESS=0x...
```

**Deployment Files:**

- `Procfile` - Tells Railway to run as worker process
- `requirements.txt` - Combined Python dependencies for both LFG-bot and grid-bot

### Managing the Bot on Railway

**View logs:**
```bash
railway logs
```

**Check status:**
```bash
railway status
```

**Open dashboard:**
```bash
railway open
```

**Making Updates:**

1. Edit code locally
2. Commit and push to GitHub:
   ```bash
   git add .
   git commit -m "Update description"
   git push
   ```
3. Railway auto-deploys in ~30 seconds

**Stopping the Bot:**

Use Railway dashboard → Service → Settings → Remove Service

Or temporarily disable by commenting out the Procfile:
```bash
# Edit Procfile, comment out the worker line
git add Procfile
git commit -m "Pause bot"
git push
```

### Region Selection

For lowest latency:
- **Virginia (US-East)** - Currently ~500ms execution time
- Singapore/Tokyo regions may offer lower latency if available

---

## Local Development

**Run live bot locally:**

```bash
cd LFG-bot
python market_maker.py
```

**Test WMA signals without trading:**

```bash
cd LFG-bot
python wma_tester.py
```

Credentials loaded from `../grid-bot/.env.hyperliquid`

**Testing new WMA settings:**

Edit `wma_tester.py` to try different configurations:
```python
tester = WMATesterV2(
    wma_period=15,              # Try different periods
    spread_threshold_bps=8.0,   # Test wider spreads
    price_type='mid_price',     # Try (H+L)/2 instead
    threshold=0.001,            # Adjust FLAT buffer
)
```
