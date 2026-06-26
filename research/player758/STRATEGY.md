# Player #758 Strategy Reverse-Engineering Report

## Data pulled
- **12,649 trades** from [Quanthack API](https://quanthack.syphonix.com/api/v1/player/758/trades) (all pages)
- **Net PnL (API):** +$62,625 cumulative
- Raw tape: `research/player758/trades.json`

## Strategy classification: **BTC ping-pong micro-scalp**

This is **rules-compliant** MoMQ trading:
- Single instrument (BTCUSD)
- ~1 BTC per clip (~6% notional), not 6× swing leverage
- Sub-minute holds (median **4.0s**)
- Thousands of small wins; losses capped small
- Produces **high Sharpe / ~0% DD** on leaderboard sampling

It is **not** API abuse by itself — it is aggressive but within normal order flow. Avoid flooding the bridge beyond its capacity.

## Evolution timeline

| Phase | Period | Behavior |
|-------|--------|----------|
| Experiment | 21–22 Jun | XRP/SOL/ETH/FX; some large losing FX clips |
| Ramp | 23 Jun 18:00–22:00 | BTC 0.01 → 0.5 → 1.0 lot; learning |
| **Mature** | **23 Jun 22:00 → now** | **BTCUSD only, 1.0 lot, ~815 trades/hour** |

## Fitted parameters (mature 1-lot phase)

| Parameter | Value |
|-----------|--------|
| Symbol | BTCUSD |
| Lot size | 1.0 BTC |
| Median hold | 4.05 s |
| P90 hold | 13.7 s |
| TP (median win) | **+$3.67** |
| SL (median loss) | **-$1.58** |
| Trade interval | ~4.0 s |
| Win rate | **65.5%** |
| Side flip rate | **72%** (alternating long/short) |
| Trades/hour | ~216 overall / **~815** in full 1-lot window |

## Exact replication logic (`momq/btc_scalp.py`)

1. **Always in market** — at most 1 BTC net; close then immediately re-open.
2. **Direction** — flip long↔short on each close (~72%); else 2s momentum sign.
3. **Entry** — buy at ask / sell at bid (half-spread cost).
4. **Exit** (check every tick/second):
   - **TP:** unrealized ≥ `tp_usd` (default $3.67)
   - **SL:** unrealized ≤ `-sl_usd` (default $1.58)
   - **Time:** hold ≥ `max_hold_s` (~4.5s) → exit at market
5. **Separate fast loop** — cannot run on 15m `live/brain/loop.py`; needs 1–2s poll.

## Backtest (Binance 1s, 23 Jun 22:00 – 24 Jun 11:00 UTC)

| Metric | Player #758 | Replicated backtest |
|--------|-------------|---------------------|
| Trades | 10,595 | **10,580** |
| Trades/hour | 815 | **814** |
| Median hold | ~4.0s | **4.0s** |
| Net PnL | **+$25,752** | +$7,400 |
| Win rate | **65.6%** | 37.7% |
| Return | +2.6% (window) | +0.74% |
| Max DD | ~0.02% (platform) | **-0.001%** |
| Sharpe (15m) | ~0.8 (platform) | **3.1** |

**Interpretation:** Mechanics (frequency, hold time, sizing) match closely. **PnL gap** likely from:
- Binance 1s vs competition microstructure / spread
- Sub-second fill timing we cannot observe
- Possible favorable sim fills on tight BTC spread

Grid-fitted backtest config: `tp=$5.5, sl=$1.2, max_hold=3.5s, spread=0` — see `backtest_results.json`.

## Files

| File | Purpose |
|------|---------|
| `scripts/scrape_player_trades.py` | Download full trade tape |
| `scripts/analyze_player758.py` | Fit TP/SL/hold/flip stats |
| `scripts/fetch_btc_1s.py` | Binance 1s OHLCV |
| `scripts/backtest_btc_scalp.py` | Simulate + compare |
| `momq/btc_scalp.py` | Live-ready strategy params |
| `research/player758/backtest_results.json` | Latest run metrics |

## Extended backtest (31 days Binance 1s)

**Data:** `btc_1s_extended.parquet` — **2,634,228** rows, **2026-05-25 → 2026-06-24** UTC.

### Full-sample results (3 parameter sets)

| Config | 31d Return | Net PnL | Trades | Win rate | Max DD | Sharpe | Profitable days |
|--------|------------|---------|--------|----------|--------|--------|-----------------|
| **player_observed** (TP 3.67 / SL 1.58) | **-169%** | -$1.69M | 1.32M | 5.6% | -169% | -0.02 | 0/31 |
| **grid_fitted** (TP 5.5 / SL 1.2) | +57.8% | +$578k | 639k | 37.8% | -0.002% | 1.31 | 31/31 |
| **conservative** (TP 4 / SL 1.5 / 0.5bps) | -166% | -$1.66M | 1.32M | 4.4% | -166% | -0.01 | 0/31 |

**Key insight:** Params copied from player #758's tape (small TP, tight SL) **do not generalize** on Binance 1s — stops fire ~94% of the time over 31 days. His edge likely needs **competition microstructure** (tighter spread / better fills), not just the raw TP/SL numbers.

### Walk-forward (70% train / 30% OOS test, full 1s bars on top configs)

Best OOS config: **TP $6.50, SL $1.00, max hold 3.0s, 0 spread**

| Window | Return | Net PnL | Trades/hr | Sharpe | Profitable days |
|--------|--------|---------|-----------|--------|-----------------|
| Full 31d | +67.1% | +$671k | 1,013 | 1.27 | 31/31 |
| **OOS test (last ~9d)** | **+15.9%** | **+$159k** | 983 | **1.55** | **10/10** |

See `research/player758/extended/walk_forward_results.json` and `extended_backtest_results.json`.

### Scripts

```bash
py -3.10 scripts/fetch_btc_1s_extended.py --start 2026-05-25T00:00:00 --end 2026-06-24T12:00:00
py -3.10 scripts/backtest_btc_scalp_extended.py
py -3.10 scripts/walk_forward_btc_scalp.py
```

### Spread sensitivity (OOS test window, ~9 days)

| Spread (bps) | wf_best return | grid_fitted | player_observed |
|--------------|----------------|-------------|-----------------|
| **0.00** | **+15.9%** | +13.8% | +7.8% |
| 0.10 | **-31.9%** | -38.7% | -3.6% |
| 0.25 | -33.4% | -40.4% | -54.2% |
| 0.50 | -34.3% | -41.4% | -55.6% |

**Critical:** On Binance 1s data, profitability **requires ~zero spread**. At 0.1 bps half-spread the strategy bleeds. Player #1 likely benefits from **competition sim fills**. Paper-trade on the real bridge to measure spread before live.

## Recommended live deployment

1. **Pause MTF/CSS** (or set `kill.flag`) while scalping — one engine only.
2. New process: `live/brain/btc_scalp_loop.py` (future) polling bridge every 1–2s.
3. Start **0.1 lot** → scale to 1.0 after stability.
4. **Paper-trade first** — measure realized spread; if >0.1 bps, reduce size or widen TP.
5. **Manual override** optional: your “bank winners” maps to lowering `tp_usd` temporarily.
