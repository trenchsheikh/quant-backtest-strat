# MoMQ Live Execution System

Live trading execution system for the MoMQ MT5 competition.
Translates backtested three-sleeve signals into real-time MT5 orders via a
local FastAPI bridge, with watchdog process management and full kill-switch
safety.

---

## 1. System Overview

| Sleeve | Instruments | Strategy | Leverage |
|--------|-------------|----------|----------|
| **COC** | EURUSD GBPUSD USDCAD USDJPY AUDUSD USDCHF | Contrarian carry â€” vol-parity sized, reversion to prior-range extremes | 22x |
| **MTF** | BTCUSD ETHUSD SOLUSD XRPUSD | Crypto EMA momentum (4/16 crossover, M15 bars) | 6x |
| **CSS** | (EURUSD,USDCHF) and (AUDUSD,USDCAD) | Log-spread z-score mean reversion | 3x |

Total gross leverage cap: **25x** (enforced by `LiveRiskEngine`).
Signal logic is imported directly from the `momq` backtest package â€” no duplication.

---

## 2. Installation

Run all commands from the **repo root** (`D:\quant-backtest-strat`).

```powershell
# Install dependencies
py -3.10 -m pip install -r live/requirements.txt

# Verify momq signals are importable
py -3.10 -c "from momq.signals import signal_coc_open; print('OK')"
```

---

## 3. Configure Credentials

Credentials live in `live/.env`. Copy the example and fill it in:

```powershell
copy live\.env.example live\.env
```

Edit `live\.env`:

```ini
# MT5 connection â€” from competition portal
MT5_LOGIN=12345678
MT5_PASSWORD=your_password_here
MT5_SERVER=CompetitionBroker-Live

# Bridge (do not change)
BRIDGE_HOST=127.0.0.1
BRIDGE_PORT=8765

# SAFETY: must be true to send real orders (default: paper mode)
ENTRY_ENABLED=false

# Optional Logfire token
LOGFIRE_TOKEN=

# State directory
STATE_DIR=./live_state

# Historical bars to seed on startup (300 = 75h of M15 history)
HISTORY_BARS=300
```

**Do NOT commit `live/.env` to git â€” it contains credentials.**

---

## 4. Boot Sequence

Open **two PowerShell windows**, both in the repo root.

### Window 1 — MT5 terminal (do this FIRST)

1. Open **MetaTrader 5** manually and log into the competition account
2. **Tools → Options → Expert Advisors** → enable **Allow algorithmic trading**
3. Click **Algo Trading** in the toolbar until it is **green (ON)**
4. Leave MT5 running — do not close it

> **Important:** Do not run standalone `mt5.initialize(path=...)` test scripts while
> live. They can spawn a second terminal or call `shutdown()` and flip Algo Trading OFF.
> Use `curl http://127.0.0.1:8765/health` instead — check `mt5_trade_allowed: true`.

### Window 2 — Pre-flight check

```powershell
py -3.10 -m live.preflight
```

Expected output with MT5 connected:
```
[PASS] Kill switch clear
[PASS] Bridge connectivity: HTTP 200
[PASS] MT5 connected
[PASS] Account equity >= 900k
...
[PASS] Entry gate: ENTRY_ENABLED=false â€” paper mode
PRE-FLIGHT PASSED.
```

### Window 3 — Start the watchdog (after preflight passes)

```powershell
py -3.10 live\watchdog.py
```

The watchdog:
1. Starts the bridge (FastAPI + MT5) on port 8765
2. Waits up to 60s for bridge to become healthy
3. Starts the brain (15-min bar loop)
4. Supervises both processes, restarting on crash
5. Sits idle between rounds â€” no orders placed

**Paper mode** (default, `ENTRY_ENABLED=false`): signal intents are logged as
`PAPER:` lines but no MT5 orders are sent.

---

## 5. Manual Bridge Start (for testing only)

If you want to run just the bridge without the watchdog:

```powershell
py -3.10 -m uvicorn live.bridge.app:app --host 127.0.0.1 --port 8765
```

Check it responds:
```powershell
curl http://127.0.0.1:8765/health
```

---

## 6. Kill Switch

Three ways to stop all trading immediately.

### Method 1 â€” HTTP POST (fastest, also closes all positions)
```powershell
curl -X POST http://127.0.0.1:8765/kill
```

### Method 2 â€” Touch file (brain halts on next bar, max ~15 min lag)
```powershell
# PowerShell
New-Item live_state\kill.flag -ItemType File -Force
```

### Method 3 â€” Kill the watchdog process
```powershell
# Find the PID
Get-Content live_state\watchdog.pid
# Then stop it
Stop-Process -Id <pid>
```

### To clear the kill switch after investigation:
```powershell
Remove-Item live_state\kill.flag -ErrorAction SilentlyContinue
curl http://127.0.0.1:8765/health
# Verify kill_switch=false, then restart watchdog
```

---

## 6b. Strategy switch (bar brain vs BTC scalp)

Two strategies share the bridge but only one should run at a time:

| Mode | Process | Module |
|------|---------|--------|
| `bar` | 15m COC/MTF/CSS brain | `live.brain.loop` |
| `scalp` | BTC ping-pong micro-scalp | `live.brain.btc_scalp_loop` |

State file: `live_state/strategy.mode` (`bar` or `scalp`).

### Check status
```powershell
py -3.10 -m live.strategy_ctl status
```

### Switch to BTC scalp (stops bar brain, starts scalp)
```powershell
py -3.10 -m live.strategy_ctl scalp --start
```

### Switch back to 15m bar brain
```powershell
py -3.10 -m live.strategy_ctl bar --start
```

### Emergency halt (kill flag + stop both loops)
```powershell
py -3.10 -m live.strategy_ctl kill on
```

### Resume trading (clear kill flag only — does not auto-start a loop)
```powershell
py -3.10 -m live.strategy_ctl kill off
py -3.10 -m live.strategy_ctl scalp --start   # or bar --start
```

### Stop both without changing mode
```powershell
py -3.10 -m live.strategy_ctl stop
```

`scalp --start` / `bar --start` briefly set `kill.flag` while stopping the other process (use `--no-safe` to skip). Flatten MTF/CSS metals before switching to scalp — scalp only trades BTC.

Scalp defaults (override in `live/.env`): `SCALP_LOT_BTC=0.1`, auto TP/SL **$0.37 / $0.16** (player #758 medians × lot), `SCALP_MAX_HOLD_S=4.5`, `SCALP_POLL_S=1`, `SCALP_MOMENTUM_AFTER_SL=true`. Set `SCALP_TP_USD` / `SCALP_SL_USD` explicitly to override auto-scale.

---

## 7. Round Calendar

All times UTC (competition runs BST = UTC+1, so 22:00 BST = 21:00 UTC).

| Round | Start (UTC) | End (UTC) | Duration | Active sleeves |
|-------|-------------|-----------|----------|----------------|
| R1 | Jun 21 21:00 | Jun 22 21:00 | 24h | COC + MTF + CSS |
| R2 | Jun 22 21:00 | Jun 23 09:00 | 12h | COC + MTF + CSS (Stage 2 ends 10:00 BST) |
| R3 | Jun 23 09:00 | Jun 24 21:00 | ~36h | R2 comeback: CSS + MTF (COC off) |
| Finals | Jun 24 21:00 | Jun 26 21:00 | 48h | COC bars 0â€“95, MTF bars 96+ |

**Finals bar split:**
- Bars 0â€“95 (first 24h): COC active, MTF suppressed (avoids 31x RD violation)
- Bar 96+: COC banks all positions, MTF activates

The brain detects the active round automatically.

---

## 8. Monitoring

### Live status
```powershell
# Health + MT5 connection status
curl http://127.0.0.1:8765/health

# Account equity / balance
curl http://127.0.0.1:8765/account

# Open positions
curl http://127.0.0.1:8765/positions

# Live quote for a symbol
curl http://127.0.0.1:8765/quote/EURUSD
```

### Heartbeat file (written every 60s)
```powershell
Get-Content live_state\heartbeat.json
```

### Saved state inspection
```powershell
py -3.10 -c "from live.state.store import StateStore; from pathlib import Path; s=StateStore(Path('live_state')); st=s.load(); print(f'round={st.round_kind} bar={st.bar_i} positions={len(st.positions)} pnl=${st.realized_pnl:,.0f}')"
```

### Trade journal (`live_state/trades.json`)

Every open and close is appended atomically by the brain:

```powershell
Get-Content live_state\trades.json
py -3.10 -c "import json; d=json.load(open('live_state/trades.json')); print(len(d['events']),'events'); [print(e['event'],e['symbol'],e.get('pnl')) for e in d['events'][-5:]]"
```

Events include: `open` / `close`, ticket, symbol, lots, price, sleeve, `round_kind`, `bar_i`, `exit_reason`, and `pnl` (when known).

### Read-only dashboard (port 8799 — does not trade)

Separate terminal from bridge/brain. Reads `live_state/*.json` and `GET /account`, `GET /positions` only.

```powershell
py -3.10 scripts/live_dashboard.py
```

Open http://127.0.0.1:8799 — auto-refresh every 30s in the browser. Shows **estimated live Final Score** (rules §11: 70% return + 15% DD + 10% Sharpe + 5% RD ranks vs benchmark field).

### Risk penalty thresholds

| Metric | Warn at | Hard limit | Penalty |
|--------|---------|------------|---------|
| Gross leverage | 24x | 28x for 30min | âˆ’20 pts |
| Gross leverage | â€” | 29x for 1 bar | âˆ’30 pts |
| Single instrument | 80% | 90% for 30min | âˆ’10 pts |
| Net directional | 90% | 95% for 30min | âˆ’10 pts |
| Margin utilisation | 85% | 90% for 30min | âˆ’20 pts |

---

## 9. State Persistence and Reconcile-on-Restart

`live_state/live_state.json` is written atomically after every bar. On restart,
the brain reconciles saved state against live MT5 positions â€” MT5 is the
ground truth for what's actually open.

| Situation | Action |
|-----------|--------|
| Position in state + in MT5, lots match | Keep saved metadata |
| Position in state + in MT5, lots differ | Trust MT5 lots |
| Position in state, NOT in MT5 | Remove (closed externally) |
| Position in MT5, NOT in state | Add as `sleeve=unknown` |

### R1 recovery mode (late round start)

If the system missed COC at bar 0, the brain now:

1. **COC catch-up** — deploys vol-parity COC on the next bar (budget scaled by bars remaining). Skips symbols already held (e.g. CSS legs).
2. **CSS z-stop** — closes spread pairs if |z| diverges ≥0.8 past entry |z| (protects equity on open positions).
3. **CSS recovery entries** — when COC is not deployed: stricter entry (|z|≥1.6), half budget, single best pair only.

**Restart brain only** (keeps bridge + MT5 session; does not close open trades):

```powershell
# Find brain PID from watchdog logs, then:
Stop-Process -Id <brain_pid> -Force
# Watchdog restarts brain automatically within ~30s
```

Or restart watchdog if brain PID is unclear. Do **not** kill the bridge while CSS legs are open unless you intend to flatten.

Config lives in `momq/tournament_config.py` (`coc_catchup_enabled`, `spread_z_stop`, etc.).

### Rules compliance (rules.md §13)

The brain enforces **Risk Discipline** on every 15m bar:

| Rule | Penalty threshold | Our pre-emptive block |
|------|-------------------|------------------------|
| Gross leverage | >28x for 30min (−20), >29x for 15min (−30) | Block new entries ≥**27x**; strategy cap **25x** |
| Margin usage | >90% for 30min (−20), >95% for 15min (−30) | Block new entries ≥**85%** |
| Single-instrument | >90% gross for 30min (−10) | Block projected ≥**85%** |
| Net directional | >95% gross for 30min (−10) | Block projected ≥**92%** |
| Stop-out | Margin level **30%** (forced liquidation) | Block entries if margin level ≤**80%** |

Metrics use **live MT5 equity and margin** (not init equity). RD score and streaks persist in `live_state.json` (`rd_score`, `rd_streaks`, `rd_violations`).

```powershell
py -3.10 -c "import json; s=json.load(open('live_state/live_state.json')); print('RD', s.get('rd_score'), s.get('rd_last_metrics'))"
```

**Exits always run** (CSS z-stop, COC TP, MTF profit protection, etc.) even when `rd_entries_blocked` is true — only **new entries** are gated.

### MTF profit protection (`momq/tournament_config.py`)

MTF legs (crypto + metals) bank winners without breaking RD rules — closes **reduce** leverage:

| Rule | Default | When it fires |
|------|---------|----------------|
| **Hard TP** | +2.0% price (`mtf_tp_pct`) | Leg MTM reaches target at bar close |
| **Trailing stop** | Arm at +1.0%, trail 0.4% (`mtf_trail_*`) | After peak, giveback from high |
| **Profit EMA** | Arm at +0.8% (`mtf_profit_reversal_arm_pct`) | EMA signal weakens below +0.03% while in profit |
| **Stop / max-hold / EMA** | unchanged | −2.5% leg, 6h, or full reversal |

`peak_mtm_pct` per position is persisted in `live_state.json` for trailing. **Restart brain only** after deploy (bridge/CSS legs preserved).

---

## 10. OPEN QUESTIONS â€” Verify Before Going Live

> Do this before setting `ENTRY_ENABLED=true`. Wrong values here cause order
> rejections, wrong sizing, or missed fills.

### 10.1 MT5 Symbol Names
Competition servers often suffix symbols: `EURUSDm`, `EURUSD.r`, `BTCUSDT` etc.

- **Action:** Open MT5 terminal â†’ Market Watch â†’ confirm exact symbol names.
  Update `SYMBOL_MAP` in `live/config.py`.

### 10.2 Crypto Contract Sizes
Current assumption: 1 coin per lot. Common but not universal.

- **Action:** MT5 â†’ View â†’ Symbols â†’ select crypto â†’ check "Contract size".
  Update `CONTRACT_SIZES` in `live/config.py`.

### 10.3 BAR Token
`BARUSD` may not exist on the competition server.

- **Action:** Check Market Watch. If absent, remove from `MTF_SYMBOLS` in `live/config.py`.

### 10.4 Order Filling Mode
Current code uses `ORDER_FILLING_IOC`. Some brokers require `FOK` or `RETURN`.

- **Action:** If orders are rejected with retcode 10014, change `type_filling`
  in `MT5Client.place_market_order` in `live/bridge/mt5_client.py`.

### 10.5 Minimum Lot Size / Lot Step
Crypto lot step is sometimes 0.001, not 0.01.

- **Action:** MT5 â†’ View â†’ Symbols â†’ "Volume min" and "Volume step".
  Update rounding in `LiveRiskEngine.notional_to_lots` in `live/brain/risk.py`.

### 10.6 Historical Bar Depth
Signals need â‰¥200 M15 bars for warmup.

- **Action:** Run preflight and check "Historical bar depth" section.
  If short, reduce `HISTORY_BARS` in `live/.env`.

### 10.7 Round Time Confirmation
Times above assume BST = UTC+1.

- **Action:** Confirm with organiser. Update `ROUND_SCHEDULE` in `live/config.py`
  if times differ.
