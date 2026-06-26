# HFT Scalp Research — 60%+ Win Rate (NOT LIVE)

**Status:** Research complete. **Live `btc_scalp_loop.py` unchanged.**

## Papers applied

### Cartea & Jaimungal (SSRN-2010417)
- HF profits from **short-horizon momentum** — trade with the tick, don't fade informed flow.
- **Adverse-selection buffer** — skip entries when the last bar moved against your side.
- **Inventory discipline** — always flat quickly; round-trips only.

### Mahdavi-Damghani (Oxford-Man HFTE)
- **Microprice proxy** on 1s bars: **close-in-range (CIR)** = `(close-low)/(high-low)`.
- High CIR ≈ buy pressure; low CIR ≈ sell pressure (order-book imbalance idea).
- Combine with **multi-scale momentum** (2s + 5s) to filter chop.

---

## Champion strategy: `mom_micro`

| Parameter | Baseline (live now) | **Champion (research)** |
|-----------|---------------------|-------------------------|
| Entry | Blind flip after close | **2s+5s momentum agree + CIR confirms** |
| TP / lot | $3.67 | **$2.00** |
| SL / lot | $1.585 | **$1.20** |
| Max hold | 4.5s | **3.0s** |
| CIR threshold | — | **≥0.58 long / ≤0.42 short** |
| Min bar range | — | **$0.30** (skip flat ticks) |

### Entry rules (`mom_micro`)
1. **Long** if: `mom_2s > 0` AND `mom_5s > 0` AND `CIR ≥ 0.58`
2. **Short** if: `mom_2s < 0` AND `mom_5s < 0` AND `CIR ≤ 0.42`
3. Else: **stay flat** (skip bar — key difference vs always-in-market flip)
4. Exit: same TP / SL / time as current loop

---

## Backtest results (Binance 1s BTC, zero spread)

Window: **2026-06-17 → 2026-06-24** (7 days)

| Strategy | Trades | Win rate | Net PnL | Return | Sharpe |
|----------|--------|----------|---------|--------|--------|
| **Baseline flip** (player #758 style) | 59,980 | **45.1%** | +$41,151 | +4.1% | 1.27 |
| **Champion mom_micro** | 62,977 | **61.1%** | +$48,934 | +4.9% | 2.02 |
| Cartea variant | 23,863 | 59.5% | +$17,512 | +1.8% | 1.66 |

### Validation
| Split | Win rate | Net PnL |
|-------|----------|---------|
| 5 days | **60.6%** | +$32,118 |
| 7 days | **61.1%** | +$48,934 |
| Train (~4d) | **61.1%** | +$26,571 |
| Test (~3d) | **61.0%** | +$22,362 |

### Spread sensitivity (critical for live)
| Spread | Win rate | Return |
|--------|----------|--------|
| **0.0 bps** | **61.1%** | +4.9% |
| 0.05 bps | 46.7% | +2.7% |
| **0.1 bps** | **2.8%** | **-8.1%** |

**Live implication:** Strategy only works if competition fills are **very tight** (~0 bps effective). At 0.1 bps the edge vanishes — same lesson as original player #758 research.

---

## Why WR jumps 45% → 61%

1. **Selective entries** — skip ~50% of bars where momentum and microprice disagree (no blind flip).
2. **Closer TP** ($2 vs $3.67) — easier to hit before SL on a short hold.
3. **Tighter SL** ($1.20) — smaller loss per loser; combined with momentum filter, fewer SLs.
4. **Shorter hold** (3s) — less time for adverse drift.

Trade-off: still profitable with **higher WR** and **similar $/trade** ($0.78 vs $0.69 baseline).

---

## Files (research only)

| File | Purpose |
|------|---------|
| `momq/hft_scalp_research.py` | Entry modes + config (not imported by live loop) |
| `scripts/backtest_hft_scalp.py` | Backtest engine |
| `scripts/finalize_hft_60wr.py` | Validation runner |
| `research/player758/hft_search/best_60wr_strategy.json` | Full metrics JSON |

---

## To deploy live (when you approve)

Changes needed in a **new code path** or feature flag (do not break running scalp):

1. Add `SCALP_ENTRY_MODE=mom_micro` to `.env`
2. Compute on each tick: `mom_2s`, `mom_5s`, CIR from last N quotes (or 1s mid history)
3. Replace blind `flip_dir` entry with `entry_direction()` from `hft_scalp_research.py`
4. Update TP/SL defaults: `SCALP_TP_USD_PER_LOT=2.0`, `SCALP_SL_USD_PER_LOT=1.2`, `SCALP_MAX_HOLD_S=3.0`
5. **Measure live spread** — if effective spread > 0.05 bps, WR will drop sharply

Optional A/B: run champion on paper for 30 min alongside current flip strategy and compare realized WR.
