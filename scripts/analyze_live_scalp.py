"""Quick forensic analysis of the live trade journal — scalp P&L reality check."""
import json
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path

path = Path("live_state/trades.json")
data = json.loads(path.read_text(encoding="utf-8"))
events = data["events"]
print(f"total events: {len(events)}  updated_at={data.get('updated_at')}")

# Split by sleeve & event
closes = [e for e in events if e["event"] == "close"]
opens = [e for e in events if e["event"] == "open"]
print(f"opens={len(opens)} closes={len(closes)}")

by_sleeve = Counter(e.get("sleeve", "?") for e in closes)
print("closes by sleeve:", dict(by_sleeve))

paper_ct = Counter((e.get("sleeve","?"), bool(e.get("paper"))) for e in closes)
print("closes by (sleeve,paper):", dict(paper_ct))

def summarize(name, rows):
    pnls = [e["pnl"] for e in rows if e.get("pnl") is not None]
    if not pnls:
        print(f"\n[{name}] no pnl rows"); return
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    flat = [p for p in pnls if p == 0]
    total = sum(pnls)
    print(f"\n[{name}] trades={n}  netPnL=${total:,.2f}  avg=${total/n:.3f}/trade")
    print(f"   win%={len(wins)/n*100:.1f}  wins={len(wins)} losses={len(losses)} flat={len(flat)}")
    if wins: print(f"   avg win=${sum(wins)/len(wins):.3f}  max win=${max(wins):.2f}")
    if losses: print(f"   avg loss=${sum(losses)/len(losses):.3f}  max loss=${min(losses):.2f}")
    # exit reasons
    er = Counter(e.get("exit_reason","?") for e in rows)
    print("   exit reasons:", dict(er))
    # pnl by exit reason
    by_er = defaultdict(float)
    for e in rows:
        if e.get("pnl") is not None:
            by_er[e.get("exit_reason","?")] += e["pnl"]
    print("   pnl by reason:", {k: round(v,1) for k,v in by_er.items()})

scalp_closes = [e for e in closes if e.get("sleeve") == "scalp"]
summarize("ALL SCALP", scalp_closes)
summarize("SCALP real", [e for e in scalp_closes if not e.get("paper")])
summarize("SCALP paper", [e for e in scalp_closes if e.get("paper")])

# Non-scalp (brain) closes
summarize("NON-SCALP (brain)", [e for e in closes if e.get("sleeve") != "scalp"])

# Time-bucketed scalp real PnL (per 15 min) to see equity curve shape
def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z","+00:00"))

real = [e for e in scalp_closes if not e.get("paper") and e.get("pnl") is not None]
real.sort(key=lambda e: e["ts"])
if real:
    print(f"\nSCALP real first={real[0]['ts']} last={real[-1]['ts']}")
    buckets = defaultdict(lambda: [0.0,0])
    for e in real:
        t = parse_ts(e["ts"])
        key = t.strftime("%m-%d %H:%M")[:-1]+"0"  # 10-min bucket label
        kb = t.replace(minute=(t.minute//15)*15, second=0, microsecond=0)
        b = buckets[kb]
        b[0]+=e["pnl"]; b[1]+=1
    cum=0.0
    print("\n15-min scalp REAL pnl buckets (cumulative):")
    for k in sorted(buckets):
        v,c = buckets[k]
        cum+=v
        print(f"  {k:%m-%d %H:%M}  pnl=${v:>9.2f}  n={c:>4}  cum=${cum:>10.2f}")

# Recent 40 scalp real trades detail
print("\nlast 30 scalp real closes:")
for e in real[-30:]:
    print(f"  {e['ts'][11:19]} side={e['side']:+d} entry={e.get('entry_price')} exit={e['price']} "
          f"pnl=${e['pnl']:.2f} reason={e.get('exit_reason')} {e.get('signal_info','')}")
