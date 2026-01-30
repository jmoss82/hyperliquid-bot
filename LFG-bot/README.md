# HIP-3 Market Maker

Automated market making bot for HyperLiquid HIP-3 pairs using **momentum-based** atomic BUY+SELL placement, strict flat-state gating, and quote freshness checks.

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
├── market_maker.py      # Main trading bot
├── account_monitor.py   # Position/balance monitoring
├── spread_monitor.py    # Spread analysis tool
├── lfg_config.py        # Credentials loader
└── requirements.txt     # Dependencies
```

| File | Purpose |
|------|---------|
| `market_maker.py` | Primary - momentum-based atomic two-sided market maker |
| `account_monitor.py` | Monitoring - real-time position/balance tracking |
| `spread_monitor.py` | Analysis - pre-trade spread assessment |
| `lfg_config.py` | Loads credentials from `../grid-bot/.env.hyperliquid` |

---

## Strategy (Strict Cycle)

```
1. ENSURE FLAT
   ├── Cancel all open orders (verified)
   ├── Exit any orphan positions (taker)
   └── Verify: 0 orders, 0 positions

2. DETECT OPPORTUNITY
   └── Spread > threshold? → Continue

3. PLACE ATOMIC PAIR
   └── Single API call: BUY + SELL together (momentum positioning)

4. MONITOR (~1s)
   ├── Both fill → Small spread cost (rare)
   ├── One fills → Cancel other, taker exit with favorable momentum
   └── Neither fills → Cancel both

5. REPEAT
```

Key principles:
- **Momentum strategy** (BUY near ask, SELL near bid to capture directional moves)
- Atomic ordering (BUY/SELL placed together)
- Orphan avoidance with immediate taker exits
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
    max_patience_ms=300,            # 300ms patience
    max_positions=1,                # One at a time
    max_trades=999999,              # No practical limit
    max_loss=5.0,                   # Stop if lose $5
    min_trade_interval=1.0,         # 1 second cooldown
    dry_run=False,                  # LIVE MODE
    max_quote_age_ms=1200.0,        # Speed-prioritized freshness gate
    ws_stale_timeout_s=15.0         # Reduce REST refresh frequency
)
```

Additional freshness controls (defaults):
| Parameter | Default | Notes |
|-----------|---------|-------|
| `max_quote_age_ms` | 1200 | Max age for a quote before skipping placement |
| `ws_stale_timeout_s` | 15 | WS stale threshold before REST refresh |
| `opportunity_queue_size` | 1 | Prevents backlog of stale opportunities |

### Spread Position (Momentum Strategy)

With `spread_position=0.2` and spread of $117.00 bid / $117.08 ask:
- BUY placed at: $117.08 - ($0.08 × 0.2) = **$117.064** (closer to ask - fills on upward momentum)
- SELL placed at: $117.00 + ($0.08 × 0.2) = **$117.016** (closer to bid - fills on downward momentum)

**Strategy:** Orders positioned closer to spread edges (20%) to capture cleaner directional moves. Wider spreads (6+ bps) provide better signal quality.

### Credentials

Expected environment variables (loaded by `lfg_config.py`):
- `HL_WALLET_ADDRESS` / `HL_PRIVATE_KEY`
- `HL_ACCOUNT_ADDRESS` (optional)

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

Run locally for testing:

```bash
cd LFG-bot
python market_maker.py
```

Credentials loaded from `../grid-bot/.env.hyperliquid`
