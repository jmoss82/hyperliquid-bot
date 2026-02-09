# LFG Bot (HIP-3)

Directional trend bot for HyperLiquid HIP-3 pairs using a WMA trend streak.

This README reflects the current live strategy:
- WMA trend state (UP/DOWN/FLAT) from completed 5s candles
- Trend gating uses WMA distance + slope
- Higher-timeframe bias gate using WMA-50 (distance + slope + confirmation)
- 5-in-a-row streaks to trigger entries/exits
- Market-style entries and exits (taker limit)

---

## Quick Start

Run the bot:
```bash
python market_maker.py
```

Directional tester (matches live trend logic):
```bash
python directional_trend_tester.py
```

---

## Strategy (Trend Streak + Market-Style Orders)

1. Build 5-second OHLC candles from live bid/ask
2. Calculate 60-period WMA using weighted close (H+L+C+C)/4
3. Determine trend state using WMA distance + slope (UP/DOWN/FLAT)
4. Compute higher-timeframe bias using WMA-50 + slope + confirmation
5. Count consecutive UP/DOWN streaks
6. Enter after 5 consecutive UP (LONG) or DOWN (SHORT), gated by bias
7. Market-style entries and exits (taker limit)
8. Exit on opposite 5-in-a-row or stop loss

Notes:
- We only exit on an opposite 5-in-a-row or stop loss.
- Bias gate: only LONG when bias is UP, only SHORT when bias is DOWN.
- HIP-3 XYZ orders are limit-only; "market" is implemented as a taker limit priced through the spread.

---

## Configuration (Current)

Edit `market_maker.py` main() if needed:

```python
mm = MarketMaker(
    client=client,
    coin="xyz:SILVER",
    position_size_usd=11.0,
    max_positions=1,
    max_trades=999999,
    max_loss=5.0,
    min_trade_interval=0.0,
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
    min_wma_slope_bps=0.8,
    bias_wma_period=50,
    bias_price_type="weighted_close",
    bias_enter_bps=4.0,
    bias_exit_bps=8.0,
    bias_slope_shift_candles=3,
    bias_min_slope_bps=0.6,
    bias_confirm_candles=2,
)
```

### Risk/Exit Parameters (live)

- Stop loss: `stop_loss_pct = 0.06` (6% of position notional, immediate)
- Exit trigger: Opposite 5-in-a-row streak OR stop loss

---

## Files

- `market_maker.py` - Live trading bot (trend streak strategy)
- `directional_trend_tester.py` - Manual signal monitor
- `directional_trend_tester_original.py` - Original reference logic
- `candle_builder.py` - 5s candles + WMA calc
- `account_monitor.py` - Position/balance monitoring
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

```bash
cd LFG-bot
python market_maker.py
```
