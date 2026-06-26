"""Recompute REAL scalp pnl from recorded fills (entry_price vs exit price),
bypassing the fictitious capped tp/sl pnl in the journal."""
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

data = json.loads(Path("live_state/trades.json").read_text(encoding="utf-8"))
events = data["events"]

# Pair opens->closes by ticket for scalp
opens = {}
for e in events:
    if e["event"] == "open" and e.get("sleeve") == "scalp":
        opens[e["ticket"]] = e

rows = []
for e in events:
    if e["event"] != "close" or e.get("sleeve") != "scalp":
        continue
    side = e["side"]
    entry = e.get("entry_price")
    exitp = e["price"]
    if entry is None:
        continue
    lots = e["lots"]
    real = side * (exitp - entry) * lots  # contract size 1 for BTCUSD
    rows.append({
        "ts": e["ts"], "side": side, "entry": entry, "exit": exitp,
        "real": real, "journal": e.get("pnl"), "reason": e.get("exit_reason"),
    })

rows.sort(key=lambda r: r["ts"])
n = len(rows)
real_total = sum(r["real"] for r in rows)
jrnl_total = sum(r["journal"] for r in rows if r["journal"] is not None)
real_wins = [r for r in rows if r["real"] > 0]
print(f"scalp closed trades: {n}")
print(f"JOURNAL pnl total : ${jrnl_total:,.2f}  (fictitious capped tp/sl)")
print(f"REAL fill pnl total: ${real_total:,.2f}  (side*(exit-entry)*lots)")
print(f"REAL win rate: {len(real_wins)/n*100:.1f}%  avg real ${real_total/n:.3f}/trade")
spread_drag = jrnl_total - real_total
print(f"implied total slippage/spread drag: ${spread_drag:,.2f}  (~${spread_drag/n:.2f}/trade)")

# cumulative real curve in 15-min buckets
buckets = defaultdict(lambda: [0.0, 0])
for r in rows:
    t = datetime.fromisoformat(r["ts"].replace("Z","+00:00"))
    kb = t.replace(minute=(t.minute//15)*15, second=0, microsecond=0)
    buckets[kb][0] += r["real"]; buckets[kb][1] += 1
cum = 0.0
print("\n15-min REAL scalp pnl (cumulative) — the TRUE equity contribution:")
for k in sorted(buckets):
    v,c = buckets[k]; cum += v
    print(f"  {k:%H:%M}  real=${v:>10.2f}  n={c:>4}  cum=${cum:>11.2f}")

# Real pnl by exit reason
by_r = defaultdict(lambda:[0.0,0])
for r in rows:
    by_r[r["reason"]][0]+=r["real"]; by_r[r["reason"]][1]+=1
print("\nREAL pnl by exit reason:")
for k,(v,c) in by_r.items():
    print(f"  {k:6} n={c:>5} real=${v:>10.2f} avg=${v/c:.2f}")

# Effective entry->exit price move distribution: how far does BTC need to move?
# Show avg favorable/adverse
print(f"\nBTC price range this session: first entry={rows[0]['entry']:.1f} last exit={rows[-1]['exit']:.1f}")
