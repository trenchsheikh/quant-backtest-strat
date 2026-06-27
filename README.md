# MoMQ — Quant Backtest & Live Trading Stack

A complete quantitative trading system built for the **MoMQ MT5 trading competition**:
a multi-strategy backtest engine, a high-frequency BTC scalper, and a production
live-execution stack that turns backtested signals into real MetaTrader 5 orders.

Everything is driven by the official competition rules in [`rules.md`](rules.md) and the
leaderboard scoring formula below.

```
Final Score = 70% Return Rank + 15% Drawdown Rank + 10% Sharpe Rank + 5% Risk Discipline
```

Sharpe is computed from **15-minute equity returns** (non-annualized). Risk Discipline
starts at 100 and loses points for sustained leverage / margin / concentration breaches.

---

## Table of contents

- [What's inside](#whats-inside)
- [Trading strategies](#trading-strategies)
- [High-frequency trading (HFT)](#high-frequency-trading-hft)
- [Backtest results](#backtest-results)
- [Technologies used](#technologies-used)
- [Quick start](#quick-start)
- [Live execution](#live-execution)
- [Repository layout](#repository-layout)
- [Testing](#testing)
- [Disclaimer](#disclaimer)

---

## What's inside

This repo has three pillars that share a single signal library so there is **no logic
duplication** between research and production:

| Pillar | What it does | Entry point |
|--------|--------------|-------------|
| **Backtest engine** | Replays historical tick data through the strategies and scores each round exactly like the competition judge. | [`run_tournament.py`](run_tournament.py) |
| **HFT scalp research** | Reverse-engineers the leaderboard leader, then searches for a higher win-rate BTC micro-scalp. | [`scripts/`](scripts), [`research/`](research) |
| **Live execution** | Runs the strategies in real time against MT5 with a safety bridge, watchdog, and kill-switch. | [`live/`](live/README.md) |

---

## Trading strategies

The core engine runs **three independent "sleeves"**, each targeting a different market
regime. Signals live in [`momq/signals.py`](momq/signals.py); all parameters live in
[`momq/tournament_config.py`](momq/tournament_config.py).

| Sleeve | Name | Market | Idea | Leverage |
|--------|------|--------|------|----------|
| **COC** | Contrarian Open Carry | Forex + gold | At round open, rank the 12h pre-round return across the majors. Long the 2 weakest, short the 1 strongest — a mean-reversion bet, **inverse-vol sized** so every leg contributes equal expected P&L. | 22x |
| **MTF** | Momentum Trend Follow | Crypto (BTC/ETH/SOL/XRP) + metals | EMA(4) vs EMA(16) crossover on 15m bars. Crypto *trends* within a round while forex *reverts*. Opens after warmup, exits on stop-loss, EMA reversal, take-profit, trailing stop, or max hold. | 6x |
| **CSS** | Correlation Spread | Forex pairs | Dollar-neutral log-spread z-score mean-reversion between correlated pairs (e.g. EURUSD/USDCHF). Enter on `|z| > 1.6`, exit on `|z| < 0.10`. | 3x |

**Total gross leverage is capped at 25x** — comfortably under the 28x Risk Discipline
penalty threshold, enforced by the engine on every bar.

**Signal flow per bar:**
1. **COC** fires at the round open (bar 0), then has a catch-up path if the system started late.
2. **MTF** fires after warmup, taking up to N concurrent momentum legs.
3. **CSS** fires from bar 4 onward, checking the z-score on each round's correlation pair.
4. The engine sorts intents by priority (`COC > MTF > CSS`) and executes within the leverage cap.

---

## High-frequency trading (HFT)

Separate from the 15-minute brain, the repo includes a **sub-second BTC scalper** built
from research on the live leaderboard.

**1. Reverse-engineering the leader ([`research/player758/`](research/player758/STRATEGY.md))**
Pulled 12,649 trades from the competition API and classified the #1 player as a
**BTC ping-pong micro-scalp**: single instrument, ~1 BTC clips, ~4-second median holds,
thousands of small wins. Replicated in [`momq/btc_scalp.py`](momq/btc_scalp.py).

**2. Searching for a better edge ([`research/player758/hft_search/`](research/player758/hft_search/HFT_STRATEGY.md))**
Applied microstructure research (Cartea & Jaimungal on short-horizon momentum and
adverse-selection avoidance; Mahdavi-Damghani on microprice / order-flow proxies) to lift
win rate from ~45% to **~61%**. The champion `mom_micro` mode only enters when 2s + 5s
momentum agree *and* a close-in-range microprice filter confirms.

**Key honest finding:** on public Binance 1s data these scalps only stay profitable at
near-zero spread — the edge depends on the competition's tight sim fills. The HFT research
is **gated behind a feature flag and is not live by default**; paper-trade and measure
real spread first.

---

## Backtest results

Aggregated over 19 proxy rounds (May–Jun 2026 tick data, $1M base):

| Round type | Avg return | Avg Sharpe (15m) | Risk Discipline |
|------------|------------|------------------|-----------------|
| **R1** (Sun→Mon) | +2.72% | +0.043 | 100/100 |
| **R2** (Mon→Tue) | +2.82% | +0.070 | 100/100 |
| **R3** (Tue→Wed) | 0.00%* | 0.000 | 100/100 |
| **Finals** (48h) | +3.78% | +0.065 | 100/100 |

- Average return / round: **+2.31%**
- Best single round: **+8.99%**
- Best 4-round weekly path: **+17.41%**
- Risk Discipline: **100/100 on every round**

\* R3 shows 0% in backtest because the historical tick data has no crypto — MTF (the R3
alpha) has nothing to trade. It fires on crypto live.

Run the numbers yourself with the commands below; raw per-round CSVs land in the output
directory.

---

## Technologies used

| Area | Tools |
|------|-------|
| **Language** | Python 3.10 |
| **Data / compute** | `pandas`, `numpy`, `pyarrow`, `duckdb` (tick parquet → 15m panel resampling) |
| **Live bridge / API** | `FastAPI` + `uvicorn` (local order bridge), `httpx` |
| **Brokerage integration** | `MetaTrader5` (MT5 terminal API) |
| **Config / validation** | `pydantic`, `pydantic-settings`, `python-dotenv` |
| **Reliability** | `filelock` (state/singleton locks), watchdog process supervision, atomic state journaling |
| **Observability** | `logfire` (optional) |
| **Data sources** | Competition tick parquets, Binance 1s OHLCV (HFT research), competition trade API |
| **Testing** | `pytest` |

Backtest dependencies are in [`requirements.txt`](requirements.txt); the live stack adds
its own in [`live/requirements.txt`](live/requirements.txt).

---

## Quick start

### 1. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 2. Run the full tournament backtest

Runs all four round types (R1 + R2 + R3 + Finals) over the proxy windows:

```bash
python run_tournament.py --data ./backtest-data --panel ./panel --skip-resample --out ./bt_results
```

Outputs `rounds_summary.csv`, `trades.csv`, and per-round equity CSVs to `bt_results/`.

### 3. Fast R1-only run

```bash
python run_tournament.py --data ./backtest-data --panel ./panel --skip-resample --mode r1 --out ./bt_r1
```

### 4. First run (no pre-built panel)

If `panel/` is empty, drop `--skip-resample` to resample the raw ticks first (~2–3 min):

```bash
python run_tournament.py --data ./backtest-data --panel ./panel --out ./bt_results
```

> Tip: tuning lives entirely in [`momq/tournament_config.py`](momq/tournament_config.py) —
> leverage budgets, EMA periods, z-score thresholds, stops, and round scheduling.

---

## Live execution

The [`live/`](live/README.md) package runs the strategies against a real MT5 account
through a safety-first architecture:

```
brain (15m loop) ──▶ FastAPI bridge ──▶ MetaTrader 5
       ▲                   │
   watchdog            kill-switch
 (supervises)        (HTTP / file / PID)
```

- **Bridge** — local FastAPI service that talks to the MT5 terminal and exposes health,
  account, positions, and quote endpoints.
- **Brain** — the 15-minute bar loop that imports the *same* `momq` signals used in
  backtest, with a separate fast loop for the BTC scalp.
- **Watchdog** — supervises the bridge and brain, restarting on crash.
- **Risk engine** — enforces the 25x cap and pre-emptively blocks entries before any
  Risk Discipline penalty threshold.
- **Kill-switch** — three independent ways to halt and flatten instantly.
- **State persistence** — atomic journaling with reconcile-on-restart (MT5 is ground truth).

Ships in **paper mode by default** (`ENTRY_ENABLED=false`) — no real orders until you
explicitly enable it. Full setup, boot sequence, monitoring, and go-live checklist are in
**[`live/README.md`](live/README.md)**.

---

## Repository layout

```
run_tournament.py        Tournament backtest CLI (R1/R2/R3/Finals)
run.py                   Single-window backtest CLI
rules.md                 Official competition rules (authoritative)
requirements.txt         Backtest dependencies

momq/                    Strategy + engine core
  signals.py             COC / MTF / CSS signal generators (the algorithm)
  tournament_engine.py   Bar loop, position lifecycle, telemetry
  tournament_config.py   All parameters and sleeve budgets
  judge.py               Exact rules.md scoring (return/DD/Sharpe/RD ranks)
  risk_monitor.py        Breach tracker (margin, leverage, concentration)
  metrics.py             Scorecard computation
  resample.py            Tick parquet -> 15m panel (DuckDB)
  btc_scalp.py           BTC ping-pong scalp (player #758 replica)
  hft_scalp_research.py  HFT entry modes (microprice + momentum, research)

live/                    Live execution stack (see live/README.md)
  bridge/                FastAPI <-> MT5 order bridge
  brain/                 Bar loop, scalp loop, risk, reconcile
  watchdog.py            Process supervisor
  preflight.py           Pre-launch safety checks

scripts/                 Research + backtest tooling (HFT search, fetchers, dashboard)
research/                Player reverse-engineering + strategy notes
tests/                   pytest suite (RD compliance, finals playbook, order utils)
```

---

## Testing

```bash
python -m pytest tests/ -v
```

Covers Risk Discipline compliance, the Finals bar-split playbook, and order utilities.

---

## Disclaimer

This is competition / research software, not financial advice. Backtested results are not
a promise of live performance:

- **No crypto in the historical tick data** — R3 reads 0% in backtest; live crypto returns
  come from MTF.
- **Proxy windows** — the backtest rounds approximate the live schedule; don't overfit
  parameters to them.
- **Spread sensitivity** — the HFT scalps depend on very tight fills and can bleed at
  realistic spreads. Always paper-trade first.

Trade at your own risk.
