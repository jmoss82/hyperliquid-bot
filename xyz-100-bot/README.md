# XYZ-100 Bot (HIP-3)

Directional trend bot for HyperLiquid HIP-3 pairs using 30-second candles, WMA streaks, higher-timeframe bias gating, and taker-style execution.

**A/B test variant of [LFG-bot](../LFG-bot/).** Same safety fixes and strategy structure, different signal timeframe.

## Key Differences from LFG-bot

| Parameter | LFG-bot (SILVER) | XYZ-100 (this bot) |
|-----------|------------------|---------------------|
| Fast candle | 5s | **30s** |
| Required streak | 6 | **3** |
| Entry confirmation | 30s (6 x 5s) | **90s (3 x 30s)** |
| WMA(60) lookback | 5 min | **30 min** |
| Bias candle | 60s | **180s (3 min)** |
| Bias WMA period | 45 | **30** |
| Default coin | xyz:SILVER | **xyz:XYZ100** |

The hypothesis: longer candles filter noise better, so fewer false entries in chop, and trades that do trigger capture more of the move.

## Quick Start

```bash
python market_maker.py
```

## Live Strategy

1. Build completed 30-second candles from streaming bid/ask.
2. Compute fast trend from WMA distance + slope.
3. Build separate 180-second candles for higher-timeframe bias.
4. Compute sticky bias state from bias WMA distance + slope + confirmation counters.
5. Track consecutive fast-trend streaks.
6. Trigger desired direction after 3-in-a-row (`LONG` on UP streak, `SHORT` on DOWN streak).
7. Bias gate REQUIRES match:
   `LONG` only when bias is `UP`; `SHORT` only when bias is `DOWN`.
   No trading when bias is `FLAT` or `UNKNOWN`.
8. Place taker-style entries/exits (market emulated via aggressive limit).
9. Exit priority:
   max hold time (60 min) -> stop loss -> trailing TP -> opposite 3-streak -> bias-flip.

## Position-Safety Guards

All safety guards from LFG-bot are preserved:

- **WS crash emergency exit** on disconnect
- **Auto-reconnect with backoff** (2s -> 4s -> 8s ... 30s max)
- **Startup orphan recovery** on launch
- **Universal post-exit cooldown** (120s default)
- **Loss streak extended cooldown** (30 min after 2+ consecutive losses)
- **Exchange-truth entry gate** (API check before every entry)
- **Exit confirmation** (wait for confirmed flat before clearing state)
- **Max hold time** (60 min default)

## Environment Variables

All parameters configurable via env vars for Railway deployment:

### Core
- `COIN` - Trading pair (default: `xyz:XYZ100`)
- `POSITION_SIZE` - Position size in USD (default: `11.0`)
- `DRY_RUN` - Set to `true` for paper trading (default: `false`)
- `MAX_LOSS` - Stop bot if net loss exceeds this (default: `5.0`)

### Strategy
- `CANDLE_INTERVAL` - Fast candle interval in seconds (default: `30`)
- `WMA_PERIOD` - WMA calculation period (default: `60`)
- `REQUIRED_STREAK` - Consecutive trends to trigger entry (default: `3`)
- `STOP_LOSS_PCT` - Stop loss as % of notional (default: `0.04` = 4%)
- `TREND_ENTER_BPS` - Entry threshold in bps (default: `4.0`)
- `TREND_EXIT_BPS` - Exit threshold in bps (default: `8.0`)
- `POST_EXIT_COOLDOWN` - Seconds to wait after exit (default: `120.0`)

### Bias Gate
- `BIAS_CANDLE_INTERVAL` - Bias candle interval in seconds (default: `180`)
- `BIAS_WMA_PERIOD` - Bias WMA period (default: `30`)

### Trailing Take-Profit
- `TRAILING_TP_ACTIVATION_BPS` - Trail activates after this profit (default: `20.0`)
- `TRAILING_TP_TRAIL_BPS` - Trail width from high-water mark (default: `25.0`)

### Max Hold
- `MAX_HOLD_SECONDS` - Force exit after this time (default: `3600.0`)

## Files

- `market_maker.py` - Live strategy and execution loop
- `candle_builder.py` - Candle construction + WMA (shared with LFG-bot)
- `lfg_config.py` - Credential/env loading
- `requirements.txt` - Python dependencies

**Shared dependency:** Uses `xyz_client` from `../grid-bot/`

## Railway Deployment

```
COIN=xyz:XYZ100
POSITION_SIZE=11.0
CANDLE_INTERVAL=30
REQUIRED_STREAK=3
DRY_RUN=false
```
