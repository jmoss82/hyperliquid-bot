# LFG Bot (HIP-3)

Directional trend bot for HyperLiquid HIP-3 pairs (`xyz:*`) using completed-candle WMA streaks, higher-timeframe bias gating, and taker-style execution.

## Quick Start

Run live bot:
```bash
python market_maker.py
```

Run directional monitor (no orders):
```bash
python directional_trend_tester.py
```

## Live Strategy (Current)

1. Build completed 5-second candles from streaming bid/ask.
2. Compute fast trend from WMA distance + slope.
3. Build separate 60-second candles for higher-timeframe bias.
4. Compute sticky bias state from bias WMA distance + slope + confirmation counters.
5. Track consecutive fast-trend streaks.
6. Trigger desired direction after 5-in-a-row (`LONG` on UP streak, `SHORT` on DOWN streak).
7. Bias gate REQUIRES match (one direction at a time):
`LONG` only when bias is `UP`; `SHORT` only when bias is `DOWN`.
No trading when bias is `FLAT` or `UNKNOWN`.
8. Place taker-style entries/exits (market emulated via aggressive limit).
9. Exit priority:
stop loss -> trailing TP -> opposite 5-streak -> bias-flip.

## Position-Safety Guards (Current)

- Universal post-exit cooldown:
`post_exit_cooldown_s=120.0` blocks all new entries for 2 minutes after any confirmed close.
- Exchange-truth entry gate:
before entry, bot checks live position via API and blocks if non-flat.
- Exit confirmation:
after sending reduce-only exit, bot waits for live position to be flat before clearing internal state.
- If exit is acknowledged but position remains open, bot keeps monitoring and retries on next loop.

## Current Runtime Config (`market_maker.py` main)

```python
mm = MarketMaker(
    client=client,
    coin="xyz:SILVER",
    position_size_usd=11.0,
    max_positions=1,
    max_trades=999999,
    max_loss=5.0,
    min_trade_interval=0.0,
    post_exit_cooldown_s=120.0,
    dry_run=False,
    max_quote_age_ms=1200.0,
    ws_stale_timeout_s=15.0,
    wma_period=60,
    wma_price_type="weighted_close",
    wma_threshold=0.0005,
    candle_interval_seconds=5,
    max_candles=400,
    trend_enter_bps=4.0,
    trend_exit_bps=8.0,
    bias_candle_interval_seconds=60,
    bias_max_candles=2000,
    bias_wma_period=120,
    bias_price_type="weighted_close",
    bias_enter_bps=4.0,
    bias_exit_bps=12.0,
    bias_slope_shift_candles=6,
    bias_min_slope_bps=0.4,
    bias_confirm_candles=4,
    trailing_tp_activation_bps=20.0,
    trailing_tp_trail_bps=25.0,
    exit_on_bias_flip=True,
)
```

Additional defaults currently used by the class:
- `required_streak = 5`
- `stop_loss_pct = 0.04` (4% notional, was 6%)
- `position_check_interval = 0.1`
- `wma_slope_shift_candles = 3`
- `min_wma_slope_bps = 0.8`

## Files

- `market_maker.py` - live strategy and execution loop
- `directional_trend_tester.py` - trend monitor only (no orders)
- `directional_trend_tester_original.py` - legacy reference monitor
- `candle_builder.py` - candle construction + WMA
- `account_monitor.py` - periodic balance/position monitor
- `debug_positions.py` - raw user-state position diagnostics
- `lfg_config.py` - credential/env loading
- `requirements.txt` - Python dependencies

## Recent Changes (2026-02-11)

**Problem:** Bot was flip-flopping between LONG/SHORT too frequently, creating whipsaw losses.

**Root Cause Analysis:**
1. 30-min bias window too short to identify macro trends
2. Bias gate allowed trades when bias was FLAT/UNKNOWN (too permissive)
3. 6% stop loss too wide, allowed runaway losses (e.g., 9.5 hour hold at -$0.14)

**Solutions Implemented:**
1. **Bias lookback: 30min → 2 hours** (`bias_wma_period=30` → `120`)
   - Smooths out intra-session chop
   - Identifies true macro directional movement
2. **Bias gate: block opposing → require match**
   - OLD: Allowed LONG when bias=FLAT/UNKNOWN, only blocked when bias=DOWN
   - NEW: Only LONG when bias=UP, only SHORT when bias=DOWN
   - Forces one-directional trading for hours at a time
3. **Stop loss: 6% → 4%** (`stop_loss_pct=0.06` → `0.04`)
   - Cuts losing trades faster
   - Max loss per trade: ~$0.44 on $11 position
4. **Enhanced monitoring logs**
   - Added diagnostic logging to confirm position monitoring starts correctly
   - Tracks monitoring loop health

## Notes

- Live trading path uses `XYZClient` from `grid-bot/xyz_client`.
- HIP-3 XYZ is limit-order based; "market" here means aggressive taker limit.
