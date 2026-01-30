# HIP-3 Market Maker

Automated market making bot for HyperLiquid HIP-3 pairs using atomic BUY+SELL placement, strict flat-state gating, and quote freshness checks.

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
| `market_maker.py` | Primary - atomic two-sided market maker |
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
   └── Single API call: BUY + SELL together

4. MONITOR (~1s)
   ├── Both fill → Profit
   ├── One fills → Cancel other, taker exit
   └── Neither fills → Cancel both

5. REPEAT
```

Key principles:
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
    spread_threshold_bps=5.0,       # Only trade when spread > 5 bps
    position_size_usd=11.0,         # $11 per trade
    spread_position=0.3,            # 30% into spread
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

### Spread Position

With `spread_position=0.2` and spread of $117.00 bid / $117.08 ask:
- BUY placed at: $117.00 + ($0.08 × 0.2) = **$117.016**
- SELL placed at: $117.08 - ($0.08 × 0.2) = **$117.064**

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
