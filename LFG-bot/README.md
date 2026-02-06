# LFG Bot (HIP-3)

Directional trend bot for HyperLiquid HIP-3 pairs using a WMA trend streak.

This README reflects the current live strategy (Phase 4):
- WMA trend state (UP/DOWN/FLAT) from completed 5s candles
- 5-in-a-row streaks to trigger entries
- Market (taker) entries and exits
- Exit on opposite 5-in-a-row or stop loss

---

## Quick Start

Run the bot:
```bash
python market_maker.py
```

Directional tester (manual signal):
```bash
python directional_trend_tester.py
```

---

## Strategy (Phase 4: Trend Streak + Market Orders)

1. Build 5-second OHLC candles from live bid/ask
2. Calculate 60-period WMA using weighted close (H+L+C+C)/4
3. Determine trend state with hysteresis (UP/DOWN/FLAT)
4. Count consecutive trend streaks
5. Enter after 5 consecutive UP (LONG) or DOWN (SHORT)
6. Market entries and exits (taker)
7. Exit on opposite 5-in-a-row or stop loss

Notes:
- We ignore FLAT/UNKNOWN for entries.
- We only exit on an opposite 5-in-a-row or stop loss.

---

## Configuration (Current)

Edit `market_maker.py` main() if needed:

```python
mm = MarketMaker(
    client=client,
    coin="xyz:SILVER",
    position_size_usd=11.0,
    min_trade_interval=5.0,
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
    wma_slope_shift_candles=3,
    signal_ttl_s=6.0
)
```

### Risk/Exit Parameters (live)

- Stop loss: `stop_loss_pct = 0.06` (6% of position notional, immediate)
- Take profit: disabled (to avoid clipping runners)
- Max hold: 120s failsafe

---

## Files

- `market_maker.py` - Live trading bot (trend streak strategy)
- `directional_trend_tester.py` - Manual signal monitor (5-in-a-row)
- `candle_builder.py` - 5s candles, WMA calc, swing detection (legacy)
- `wma_tester.py` - Unified signal tester (legacy)
- `account_monitor.py` - Position/balance monitoring
- `spread_monitor.py` - Spread analysis tool
- `lfg_config.py` - Credentials loader
- `requirements.txt` - Dependencies

---

## Deployment (Railway)

View logs:
```bash
railway logs
```

Update live bot:
```bash
git add LFG-bot/market_maker.py LFG-bot/README.md
git commit -m "Update trend streak strategy"
git push
```

---

## Local Development

Run live bot locally:
```bash
cd LFG-bot
python market_maker.py
```

Test directional signal (no orders):
```bash
cd LFG-bot
python directional_trend_tester.py
```
