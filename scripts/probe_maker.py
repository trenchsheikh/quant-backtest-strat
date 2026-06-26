"""Venue maker probe. Stage 1: plumbing (place far limit, list, cancel) — no fill risk."""
import sys, time, json
import httpx

B = "http://127.0.0.1:8765"
c = httpx.Client(base_url=B, timeout=10.0)

q = c.get("/quote/BTCUSD").json()
bid, ask, mid, spr = q["bid"], q["ask"], q["mid"], q["spread_bps"]
spread_usd = ask - bid
print(f"QUOTE bid={bid:.2f} ask={ask:.2f} mid={mid:.2f} spread={spread_usd:.2f}usd ({spr:.2f}bps)")

# Stage 1: place a buy_limit 0.1 lot far below market (won't fill)
px = round(bid - 300.0, 2)
print(f"\n[Stage1] place buy_limit 0.1 @ {px} (far below, no-fill plumbing test)")
r = c.post("/limit", json={"symbol": "BTCUSD", "kind": "buy_limit", "lots": 0.1,
                            "price": px, "comment": "probe1", "magic": 20260624}).json()
print("  /limit ->", r)
tkt = r.get("ticket")
time.sleep(0.5)
orders = c.get("/orders").json()
print(f"  /orders -> {len(orders)} pending:", [{ 'ticket':o['ticket'],'kind':o['type'],'px':o['price_open'],'lots':o['lots']} for o in orders])
if tkt:
    cr = c.post(f"/cancel/{tkt}").json()
    print("  /cancel ->", cr)
    time.sleep(0.4)
    orders2 = c.get("/orders").json()
    print(f"  /orders after cancel -> {len(orders2)} pending")
    print("\nPLUMBING:", "OK" if (r.get("ok") and not orders2) else "CHECK")
else:
    print("\nPLUMBING FAILED — limit not accepted:", r.get("message"))
