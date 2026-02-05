# HIP-3 Market Maker

Automated market making bot for HyperLiquid HIP-3 pairs using a unified signal:

- WMA trend (completed candles only)
- Trend persistence (streak)
- Minimum distance from WMA
- WMA slope confirmation
- Hysteresis trend state (enter/exit thresholds)
- Optional structure bias filter

Entries are one-sided, post-only maker orders. Exits are fast taker orders, with an immediate exit on trend flip.

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

- `market_maker.py` - Main trading bot (unified signal + one-sided entries)
- `candle_builder.py` - 5s candles, WMA calc, swing detection
- `wma_tester.py` - Unified signal tester (no real orders)
- `account_monitor.py` - Position/balance monitoring
- `spread_monitor.py` - Spread analysis tool
- `lfg_config.py` - Credentials loader
- `requirements.txt` - Dependencies

---

## Strategy (Unified Signal + Fast Exits)

1. Ensure flat
   - Cancel all open orders (verified)
   - Exit any positions (taker)
   - Verify: 0 orders, 0 positions

2. Build candles and compute structure
   - 5-second OHLC candles from streaming bid/ask
   - Calculate 60-period WMA using weighted close (H+L+C+C)/4
   - Detect swing points for structure (asymmetric 180/12 + dedup)

3. Compute unified signal (completed candles only)
   - Trend state with hysteresis (enter/exit bps thresholds)
   - Trend persistence (streak)
   - Minimum WMA distance
   - Minimum WMA slope
   - Spread threshold
   - Optional structure bias filter

4. Place one-sided maker order
   - Trend UP -> place BUY (closer to ask, post-only)
   - Trend DOWN -> place SELL (closer to bid, post-only)

5. Monitor entry (~1s)
   - Order fills -> start position monitoring
   - Timeout -> cancel order

6. Monitor position (fast taker exits)
   - Trend flip exit (immediate)
   - Stop Loss: P&L <= -7 bps (after min hold time)
   - Take Profit: P&L >= +20 bps
   - Max Hold Time: > 120s

7. Repeat (5s cooldown)

Notes:
- P&L uses executable prices (bid for longs, ask for shorts) to avoid SL bleed.
- Quote freshness gate prevents stale placements.

---

## Configuration (Current)

Edit `market_maker.py` main() function:

```python
mm = MarketMaker(
    client=client,
    coin="xyz:SILVER",
    spread_threshold_bps=6.0,       # Only trade when spread > 6 bps
    position_size_usd=11.0,         # $11 per trade
    spread_position=0.2,            # 20% into spread (closer to edges)
    max_patience_ms=300,            # 300ms patience
    max_positions=1,                # One at a time
    max_trades=999999,              # No practical limit
    max_loss=5.0,                   # Stop if lose $5
    min_trade_interval=5.0,         # 5 second cooldown between trades
    dry_run=False,                  # LIVE MODE
    max_quote_age_ms=1200.0,        # Speed-prioritized freshness gate
    ws_stale_timeout_s=15.0,        # Reduce REST refresh frequency
    wma_period=60,                  # 60-period WMA (5 minutes)
    wma_price_type="weighted_close",
    wma_threshold=0.0005,           # 0.05% buffer
    candle_interval_seconds=5,
    max_candles=400,                # Enough for swing detection window
    min_trend_streak=2,
    min_wma_distance_bps=3.0,
    min_wma_slope_bps=0.8,
    trend_enter_bps=4.0,
    trend_exit_bps=8.0,
    wma_slope_shift_candles=3,
    structure_break_buffer_bps=3.0,
    signal_ttl_s=6.0
)
```

### Unified Signal Parameters

| Parameter | Current Value | Description |
|-----------|---------------|-------------|
| `wma_period` | 60 | Number of candles for WMA calculation (5 minutes) |
| `wma_price_type` | `weighted_close` | Uses (H+L+C+C)/4 for each candle |
| `wma_threshold` | 0.0005 (0.05%) | Buffer for raw trend classification |
| `min_trend_streak` | 2 | Consecutive completed candles in same trend |
| `min_wma_distance_bps` | 3.0 | Min distance from WMA to trade |
| `min_wma_slope_bps` | 0.8 | Min WMA slope (bps per candle) |
| `wma_slope_shift_candles` | 3 | Candle shift used for slope calculation |
| `trend_enter_bps` | 4.0 | Hysteresis entry threshold |
| `trend_exit_bps` | 8.0 | Hysteresis exit threshold |
| `structure_break_buffer_bps` | 3.0 | Structure bias buffer |
| Candle interval | 5 seconds | OHLC candle frequency |
| `signal_ttl_s` | 6.0 | Max age of cached signal for entry |

### Swing Point Detection (Market Structure)

| Parameter | Current Value | Description |
|-----------|---------------|-------------|
| `swing_lookback_left` | 180 | Candles before pivot to confirm high/low (15 minutes) |
| `swing_lookback_right` | 12 | Candles after pivot to confirm reversal (1 minute) |
| `swing_dedup_candles` | 36 | Skip swings within this many candles of last (3 minutes) |
| `swing_dedup_bps` | 15 | Skip swings within this many bps of last swing |
| `max_candles` | 400 | Maximum candle history stored |
| Total window | 193 candles | ~16 minutes of data needed for first swing |

### Position Management (Fast Exits)

| Parameter | Current Value | Description |
|-----------|---------------|-------------|
| `take_profit_bps` | 20 | Exit at +20 bps profit (taker exit) |
| `stop_loss_bps` | 7 | Exit at -7 bps loss (taker exit, active after 10s) |
| `min_hold_time` | 10s | SL inactive for first 10s to filter short-term noise |
| `max_hold_time` | 120s | Max seconds to hold position |
| `position_check_interval` | 0.1s | How often to check exit conditions |

Exit priority (checked in order):
1. Trend flip exit (immediate)
2. Stop Loss (-7 bps) after min hold time
3. Take Profit (+20 bps)
4. Max Hold Time (120s)

---

## Testing Unified Signal (No Real Orders)

```bash
cd LFG-bot
python wma_tester.py
```

What it shows:
- Real-time 5-second candle building
- Unified signal decisions (LONG / SHORT / NO_TRADE)
- Trend state with hysteresis
- Paper entries and exits
- Signal statistics and paper PnL

Wait time: ~16 minutes for first swing point detection (needs 193 candles).

Press `Ctrl+C` to stop and see statistics.

---

## Latency Notes

- Quote freshness is enforced before placement; stale data is skipped.
- WS staleness triggers a REST refresh as a fallback.
- For lower latency, run from a VPS closer to HyperLiquid infra (SG/Tokyo).

---

## HIP-3 DEX Configuration

HyperLiquid HIP-3 markets exist on a separate DEX from standard perps.

| Query Type | DEX Parameter | Example |
|------------|---------------|---------|
| Account balance | Default (none) | `user_state(address)` |
| xyz positions | `dex="xyz"` | `user_state(address, dex="xyz")` |
| xyz open orders | `dex="xyz"` | `open_orders(address, dex="xyz")` |

Without `dex="xyz"`:
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

Initial Setup:

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

Environment Variables (Railway Dashboard):

```
HL_WALLET_ADDRESS=0x...
HL_PRIVATE_KEY=0x...
HL_ACCOUNT_ADDRESS=0x...
```

Deployment Files:

- `Procfile` - Tells Railway to run as worker process
- `requirements.txt` - Combined Python dependencies for both LFG-bot and grid-bot

### Managing the Bot on Railway

View logs:
```bash
railway logs
```

Check status:
```bash
railway status
```

Open dashboard:
```bash
railway open
```

Making Updates:

1. Edit code locally
2. Commit and push to GitHub:
   ```bash
   git add .
   git commit -m "Update description"
   git push
   ```
3. Railway auto-deploys in ~30 seconds

Stopping the Bot:

Use Railway dashboard -> Service -> Settings -> Remove Service

Or temporarily disable by commenting out the Procfile:
```bash
# Edit Procfile, comment out the worker line
git add Procfile
git commit -m "Pause bot"
git push
```

### Region Selection

For lowest latency:
- Virginia (US-East)
- Singapore/Tokyo regions may offer lower latency if available

---

## Local Development

Run live bot locally:

```bash
cd LFG-bot
python market_maker.py
```

Test unified signal without trading:

```bash
cd LFG-bot
python wma_tester.py
```

Credentials loaded from `../grid-bot/.env.hyperliquid`
