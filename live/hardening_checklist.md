# MoMQ Live System — Hardening Checklist

> Work through this checklist from top to bottom before every round.
> Items marked **[BLOCKING]** must pass before the system can go live.

---

## 1. Before First Boot

- [ ] **[BLOCKING]** `.env` file created from `.env.example` with real credentials
- [ ] **[BLOCKING]** `.env` file permissions set to owner-read-only
  - Linux/Mac: `chmod 600 .env`
  - Windows: Remove all non-owner access via Properties > Security
- [ ] **[BLOCKING]** `ENTRY_ENABLED=false` in `.env` for dry-run (change to `true` only after confirming paper mode works)
- [ ] **[BLOCKING]** Kill switch file does NOT exist at `live_state/kill.flag`
- [ ] **[BLOCKING]** `python -m live.preflight` passes with 0 failures
- [ ] `live_state/` directory exists and has write permissions
- [ ] Python 3.11+ installed: `python --version`
- [ ] All dependencies installed: `pip install -r live/requirements.txt`
- [ ] `momq` package importable from repo root: `python -c "from momq.signals import signal_coc_open"`
- [ ] MT5 terminal installed and logged in manually at least once (confirms credentials)
- [ ] Competition MT5 server name confirmed (check portal — may differ from `CompetitionBroker-Live`)
- [ ] All 15 symbol names verified on competition server (see OPEN QUESTIONS in README.md)
- [ ] Contract sizes for crypto verified with competition admin

---

## 2. Kill Switch Test Procedure

**Run this MANDATORY test before any live round.**

### Method A — HTTP (preferred)

```bash
# Step 1: Start bridge only (not brain)
uvicorn live.bridge.app:app --host 127.0.0.1 --port 8765

# Step 2: Open a test position (paper mode — ENTRY_ENABLED=false)
curl -X POST http://127.0.0.1:8765/order \
  -H "Content-Type: application/json" \
  -d '{"symbol":"EURUSD","side":1,"lots":0.01,"comment":"kill_test"}'

# Step 3: Activate kill switch
curl -X POST http://127.0.0.1:8765/kill

# Step 4: Verify response shows kill_switch=true and positions_closed
# Step 5: Check kill.flag file exists: ls live_state/kill.flag
# Step 6: Remove kill flag to restore: rm live_state/kill.flag
echo "Kill switch test PASSED"
```

### Method B — File touch

```bash
touch live_state/kill.flag
# Brain detects file on next iteration and halts entries
# Bridge detects on startup
```

### Method C — SIGTERM to watchdog

```bash
# Find watchdog PID (it logs its PID on startup)
kill -TERM <watchdog_pid>
# Watchdog sends SIGTERM to brain (exits) then bridge (closes all positions on shutdown)
```

**After kill switch test: remove `live_state/kill.flag` before starting live.**

---

## 3. Network & Security

- [ ] Bridge bound to `127.0.0.1` only — NOT `0.0.0.0` (confirmed in `BRIDGE_CMD` in `watchdog.py`)
- [ ] Firewall blocks port 8765 from external access
- [ ] MT5 terminal on same machine as bridge (no remote MT5 over internet)
- [ ] NTP sync verified — time drift > 1s will cause bar timing errors
  - Windows: `w32tm /query /status`
  - Linux: `timedatectl status`
- [ ] If competition requires IP whitelist: machine IP registered in competition portal
- [ ] No other processes bind port 8765: `netstat -an | grep 8765`
- [ ] VPN active if required by competition rules
- [ ] Logfire token set in `.env` so alerts are sent off-machine

---

## 4. Process Management

- [ ] Watchdog tested for restart behavior:
  - Kill bridge manually → watchdog restarts it within 30s
  - Kill brain manually → watchdog restarts it after bridge is healthy
- [ ] Disk space: at least 1 GB free for logs and state
  - `df -h .` (Linux/Mac) or `Get-PsDrive` (Windows)
- [ ] Watchdog configured as system service (optional but recommended for multi-day rounds)
  - Linux: create systemd unit at `/etc/systemd/system/momq.service`
  - Windows: use NSSM or Task Scheduler with "Restart if process stops"
- [ ] Reboot test performed: start system, reboot machine, verify watchdog auto-starts
- [ ] Log output redirected to file: `python live/watchdog.py >> live_state/watchdog.log 2>&1 &`
- [ ] `live_state/heartbeat.json` written and updating every 60s after startup

---

## 5. Competition Day Timeline

All times UTC (BST = UTC+1).

### Round 1 — Jun 21/22

| UTC Time | BST Time | Action |
|----------|----------|--------|
| Jun 21 20:30 UTC | Jun 21 21:30 BST | Run preflight checks |
| Jun 21 20:45 UTC | Jun 21 21:45 BST | Start watchdog (`python live/watchdog.py`) |
| Jun 21 21:00 UTC | Jun 21 22:00 BST | **R1 START** — brain begins trading |
| Jun 22 21:00 UTC | Jun 22 22:00 BST | **R1 END** — brain goes idle |

### Round 2 — Jun 22/23

| UTC Time | BST Time | Action |
|----------|----------|--------|
| Jun 22 20:30 UTC | Jun 22 21:30 BST | Run preflight checks |
| Jun 22 21:00 UTC | Jun 22 22:00 BST | **R2 START** |
| Jun 23 21:00 UTC | Jun 23 22:00 BST | **R2 END** |

### Round 3 — Jun 23/24

| UTC Time | BST Time | Action |
|----------|----------|--------|
| Jun 23 20:30 UTC | Jun 23 21:30 BST | Run preflight checks (NOTE: R3 — COC disabled, CSS+MTF only) |
| Jun 23 21:00 UTC | Jun 23 22:00 BST | **R3 START** |
| Jun 24 21:00 UTC | Jun 24 22:00 BST | **R3 END** |

### Finals — Jun 24–26

| UTC Time | BST Time | Action |
|----------|----------|--------|
| Jun 24 20:30 UTC | Jun 24 21:30 BST | Run preflight checks |
| Jun 24 21:00 UTC | Jun 24 22:00 BST | **FINALS START** — COC active |
| Jun 25 21:00 UTC | Jun 25 22:00 BST | **bar 96** — COC banks, MTF takes over |
| Jun 26 21:00 UTC | Jun 26 22:00 BST | **FINALS END** |

**Do NOT stop the watchdog between rounds** — it sits idle and resumes automatically.

---

## 6. Emergency Procedures

### Check current status
```bash
curl -s http://127.0.0.1:8765/health | python -m json.tool
curl -s http://127.0.0.1:8765/account | python -m json.tool
curl -s http://127.0.0.1:8765/positions | python -m json.tool
```

### Kill switch — activate immediately
```bash
# Method 1: HTTP (fastest, also closes all positions)
curl -X POST http://127.0.0.1:8765/kill

# Method 2: File (brain halts entries on next bar check)
touch live_state/kill.flag

# Method 3: Kill watchdog (brain + bridge stop; bridge closes positions on shutdown)
kill -TERM $(cat live_state/watchdog_pid 2>/dev/null || pgrep -f watchdog.py)
```

### Close all positions manually
```bash
curl -X POST http://127.0.0.1:8765/close_all
```

### Close a specific position
```bash
curl -X POST http://127.0.0.1:8765/close/TICKET_NUMBER
```

### Remove kill switch and restart
```bash
rm live_state/kill.flag
python live/preflight.py && python live/watchdog.py
```

### Check heartbeat file
```bash
cat live_state/heartbeat.json
```

### Check saved state
```bash
python -c "
from live.state.store import StateStore
from pathlib import Path
s = StateStore(Path('live_state'))
st = s.load()
print(f'round={st.round_kind} bar={st.bar_i} positions={len(st.positions)}')
for p in st.positions:
    print(f'  {p.ticket} {p.symbol} {\"LONG\" if p.side==1 else \"SHORT\"} {p.lots}L sleeve={p.sleeve}')
"
```

### Check MT5 connection from Python
```bash
python -c "
from live.config import Settings
from live.bridge.mt5_client import MT5Client
s = Settings()
c = MT5Client(s.MT5_LOGIN, s.MT5_PASSWORD.get_secret_value(), s.MT5_SERVER)
print('Connected:', c.connect())
print(c.account_info())
c.disconnect()
"
```

---

## 7. Monitoring Checklist (During Round)

Check every 30 minutes:

- [ ] `heartbeat.json` timestamp is within 2 minutes of now
- [ ] `est_leverage` in heartbeat is below 24.0
- [ ] `mt5_connected` is `true`
- [ ] `kill_switch` is `false`
- [ ] `drawdown_pct` is above -5.0%
- [ ] No LEVERAGE_HARD/EXTREME warnings in logfire

---

## 8. Shutdown Checklist (End of Round)

- [ ] Wait for brain to detect round end (it goes idle automatically)
- [ ] Verify all round positions are closed (check `/positions` endpoint)
- [ ] Download trade history from MT5 terminal
- [ ] Save `live_state/heartbeat.json` and `live_state/live_state.json` to backup
- [ ] Note realized P&L for the round
- [ ] Keep watchdog running — it will auto-start next round
