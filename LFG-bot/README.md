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
7. Bias gate blocks only opposing bias:
`LONG` blocked when bias is `DOWN`; `SHORT` blocked when bias is `UP`.
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
    bias_wma_period=30,
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
- `stop_loss_pct = 0.06`
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

## Notes

- Live trading path uses `XYZClient` from `grid-bot/xyz_client`.
- HIP-3 XYZ is limit-order based; "market" here means aggressive taker limit.
