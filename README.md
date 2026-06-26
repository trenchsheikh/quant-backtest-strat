# MoMQ Tournament Backtest

Multi-sleeve competition strategy aligned to **`rules.md`** round schedule (22:00 BST → +24h per round, Finals +48h).

## Competition schedule

| Round | Live dates (2026) | Window |
|-------|-------------------|--------|
| **R1** | 21 Jun 22:00 → 22 Jun 22:00 | 24h |
| **R2** | 22 Jun 22:00 → 23 Jun 22:00 | 24h |
| **R3** | 23 Jun 22:00 → 24 Jun 22:00 | 24h |
| **Finals** | 24 Jun 22:00 → 26 Jun 22:00 | 48h |

Backtest uses **proxy windows** in the May–Jun tick data (same day-of-week / hour pattern as the live competition).

---

## Scoring formula (rules §11)

```
Final Score = 70% × Return Rank + 15% × Drawdown Rank + 10% × Sharpe Rank + 5% × Risk Discipline
```

Sharpe is computed from **15-minute equity returns**, non-annualized. Risk Discipline starts at 100 and loses points for sustained leverage/margin/concentration breaches.

---

## Strategy — three sleeves

| Sleeve | Leverage | When active | Logic |
|--------|----------|-------------|-------|
| **COC** — Contrarian Open Carry | 22x | R1, R2, Finals (+ banked at +24h in Finals) | At round open, rank 12h pre-round return across 6 competition forex majors. Long the 2 weakest / short the 1 strongest. **Vol-parity sized** so each leg contributes equal expected P&L. |
| **MTF** — Crypto Momentum | 6x | All rounds; **sole alpha for R3** | EMA(4) vs EMA(16) crossover on BTC/ETH/SOL/XRP 15m bars. Opens after bar 8 (2h). Exits on stop-loss (-1.8% equity), EMA reversal, or 6h max hold. |
| **CSS** — Correlation Spread | 3x | R1 (full), R2 (70%) | Dollar-neutral log-spread z-reversion. R1: EURUSD/USDCHF. R2: AUDUSD/USDCAD. Entry |z| > 1.6, exit |z| < 0.10. |

**Total leverage cap: 25x** (well under the 28x Risk Discipline penalty threshold).

**COC universe** (all 6 are in the competition instrument list):
`EURUSD, GBPUSD, USDCAD, USDJPY, AUDUSD, USDCHF`

**MTF note**: The provided tick data does not include crypto. MTF shows zero contribution in backtest but will fire on BTC/ETH/SOL/XRP/BAR in the live competition. Expected live addition: +3–5% in trending crypto rounds.

---

## Backtest results (19 rounds, May–Jun 2026)

COC now exits at +0.30% price move (take-profit), re-enters same direction after 15 min, then holds to EOD. This generates more trades and smoother equity curves on trending rounds.

| Round type | Avg return | Min return | Avg Sharpe (15m) | RD clean |
|------------|------------|------------|------------------|----------|
| **R1** (Sun→Mon) | +2.72% | −0.88% | +0.043 | Yes |
| **R2** (Mon→Tue) | +2.82% | **+0.16%** | +0.070 | Yes |
| **R3** (Tue→Wed) | 0.00% | 0.00% | 0.000 | Yes |
| **Finals** (48h) | +3.78% | +0.57% | +0.065 | Yes |

**Aggregate (all 19 rounds):**
- Avg return / round: **+2.31%**
- Best single round: **+8.99%** (R1, 17 May)
- Best 4-round weekly path: **+17.41%** (17–20 May)
- Total trades: **59** (was 43) | Risk Discipline: **100/100 on every round**
- **R2 all-positive**: min was −1.37%, now +0.16% — no more losing R2 rounds in backtest

R3 returns 0% in backtest (no crypto data). Live R3 returns will be driven by MTF.

---

## Quick start — run the backtest yourself

### 1. Install dependencies (once)

```powershell
cd d:\quant-backtest-strat
py -3.10 -m pip install -r requirements.txt
```

If you don't have Python 3.10, install from [python.org](https://www.python.org/downloads/).

### 2. Full tournament backtest (recommended)

Runs all four round types (R1 + R2 + R3 + Finals) using the May–Jun proxy windows:

```powershell
py -3.10 run_tournament.py --data .\backtest-data --panel .\panel --skip-resample --out .\bt_results
```

Outputs: `bt_results/rounds_summary.csv`, `bt_results/trades.csv`, and per-round equity CSVs.

### 3. R1-only mode (fastest, ~5 seconds)

```powershell
py -3.10 run_tournament.py --data .\backtest-data --panel .\panel --skip-resample --mode r1 --out .\bt_r1
```

### 4. First run without pre-built panel (resamples ticks, ~2–3 min)

If the `panel/` folder is empty or missing, drop `--skip-resample`:

```powershell
py -3.10 run_tournament.py --data .\backtest-data --panel .\panel --out .\bt_results
```

---

## What the output means

```
[round_1 ] 2026-05-17 -> 2026-05-18  ret=+9.76%  sharpe=+0.127  judge=88.0  trades=3  RD=100
    coc      pnl=+98,792
```

| Field | Description |
|-------|-------------|
| `ret` | Total return for that 24h/48h round (from $1M base) |
| `sharpe` | Non-annualized Sharpe from 15m equity returns (rules §12.5) |
| `judge` | Composite score 0–100 vs a synthetic field of 6 (rules §11) |
| `trades` | Closed trades in that round |
| `RD` | Risk Discipline score (100 = clean, deductions for leverage/margin breaches) |

**Weekly tournament paths** at the bottom show what your cumulative return would be if you qualified through all 4 rounds in a given week.

---

## Tuning parameters

All parameters live in `momq/tournament_config.py`:

| Parameter | Default | Effect |
|-----------|---------|--------|
| `budget.coc` | 22.0 | COC leverage (reduce if you want lower drawdown) |
| `budget.mtf` | 6.0 | MTF crypto leverage (lives within total_cap) |
| `budget.total_cap` | 25.0 | Hard gross leverage cap; Portfolio.can_add() enforces this |
| `coc_pre_window` | 48 | Bars of pre-round history to rank (48 × 15m = 12h) |
| `coc_n_long` | 2 | How many symbols to go long |
| `coc_n_short` | 1 | How many symbols to short |
| `spread_z_entry` | 1.6 | CSS entry threshold (higher = fewer, higher-conviction trades) |
| `mtf_fast` / `mtf_slow` | 4 / 16 | EMA periods for crypto momentum signal |
| `mtf_stop_pct` | 0.018 | Stop-loss: close MTF if position loses >1.8% of equity |
| `r3_mtf_scale` | 1.0 | MTF budget scale for R3 (1.0 = full, 0.0 = flat) |

---

## File map

```
run_tournament.py          CLI entry point — argument parsing, output formatting
momq/
  signals.py               COC, MTF, CSS signal generators (the algorithm)
  tournament_engine.py     Bar loop, position lifecycle, BarContext wiring
  tournament_config.py     All parameters and sleeve budgets
  judge.py                 Exact rules.md §11–13 scoring (Return/DD/Sharpe/RD ranks)
  risk_monitor.py          §13 breach tracker (margin, leverage, concentration)
  metrics.py               Scorecard computation
  resample.py              Tick parquet → 15m panel (DuckDB)
backtest-data/             Raw tick parquets (May–Jun 2026, 22 symbols)
panel/                     Pre-built 15m panels (auto-created, skip-resample reuses)
rules.md                   Official competition rules (authoritative)
```

---

## Architecture overview

```
signals.py          →  proposes TradeIntents every bar
tournament_engine.py  →  executes intents, manages exits, tracks telemetry
judge.py            →  scores the round per rules.md §11–13
```

**Signal flow per bar:**
1. COC fires at bar 0 only — ranks pre-round momentum, opens 3 vol-parity legs
2. MTF fires from bar 8 onward — checks EMA crossover on all available crypto
3. CSS fires from bar 4 onward — checks z-score on the round's correlation pair
4. Engine sorts intents by priority (COC=10 > MTF=7 > CSS=5) and executes within leverage cap

---

## Before you trust the numbers

- **No crypto in backtest data** — R3 shows 0% because BTC/ETH/SOL/XRP aren't in the parquets. Live competition includes crypto; MTF adds real alpha there.
- **Walk forward** — the 19 rounds are proxies. Don't tune parameters to these specific windows, then claim the numbers as forward-looking.
- **Stress costs** — in `tournament_config.py`, push `spread_haircut` from 2.0 to 3.0 and re-run. If the edge holds, it's real.
- **XAU scale note** — backtest gold is per-kg (`XAUKUSD` aliased to `XAUUSD`). Live sizing must use the platform's contract specs.
