"""BTC passive market-maker loop — EARN the spread via limit orders.

Brackets the market with a buy_limit below and sell_limit above. When one leg
fills (we take on inventory), the opposite resting leg becomes the take-profit;
a hard stop + max-hold cap the downside. One unit of inventory at a time.

This is the *taker spread is fatal* fix: we provide liquidity instead of paying.
Validate live at small size, read realized P&L, then scale toward the risk budget.

Run:  py -3.10 -m live.brain.maker_loop
Stop: delete live_state/maker.on  (or touch live_state/kill.flag)
"""
from __future__ import annotations

import json
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import httpx

REPO = Path(__file__).resolve().parent.parent.parent
STATE = REPO / "live_state"
ON_FLAG = STATE / "maker.on"
KILL_FLAG = STATE / "kill.flag"
LOG = STATE / "maker_log.jsonl"

BRIDGE = "http://127.0.0.1:8765"
SYMBOL = "BTCUSD"
MAGIC = 20260625
CONTRACT = 1.0  # BTC per lot


@dataclass
class MakerParams:
    lot: float = 0.2            # inventory unit (start small; scale after validation)
    offset_usd: float = 4.0     # place limits this far outside bid/ask
    tp_usd: float = 16.0        # take-profit distance from entry (>= spread to be +EV)
    stop_usd: float = 110.0     # hard catastrophe stop (rare; regime filter handles trends)
    max_hold_s: float = 70.0    # inventory discipline — cut stale bags fast
    requote_drift_usd: float = 6.0   # re-center bracket if mid drifts this far
    max_spread_usd: float = 30.0     # skip quoting if spread blows out
    # regime filter — only harvest spread when BTC is RANGING; stand flat in trends.
    # This is what makes the curve smooth + uptrending: sit out the moves that ran
    # the maker over, collect spread only when price oscillates.
    regime_window_s: float = 40.0    # lookback to classify regime
    range_ratio_max: float = 0.60    # |net drift|/path below this = ranging; above = trend
    max_path_usd: float = 150.0      # if 40s range exceeds this, vol too high -> stand flat
    long_window_s: float = 180.0     # sustained multi-minute trend lookback
    long_drift_max: float = 80.0     # |drift| over long window above this -> stand flat
    trend_exit_ratio: float = 0.72   # in-position: trend this strong against us -> exit fast
    poll_s: float = 0.6
    session_loss_limit: float = 600.0   # halt+flatten if realized PnL <= -this
    session_profit_target: float = 0.0  # 0 = no auto-stop on profit


@dataclass
class MakerState:
    start_balance: float = 0.0
    last_balance: float = 0.0
    realized: float = 0.0
    n_round_trips: int = 0
    wins: int = 0
    losses: int = 0
    bracket_center: float = 0.0
    pos_entry: float = 0.0
    pos_side: int = 0
    pos_since: float = 0.0
    halted: bool = False


def log_event(**kw):
    kw["ts"] = datetime.now(timezone.utc).isoformat()
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(kw) + "\n")


class MakerLoop:
    def __init__(self, p: MakerParams):
        self.p = p
        self.s = MakerState()
        self.c = httpx.Client(base_url=BRIDGE, timeout=8.0)
        self._mids: deque[tuple[float, float]] = deque(maxlen=600)

    def _regime(self, now: float) -> tuple[bool, float, float]:
        """Classify regime over regime_window_s.

        Returns (ranging, net_drift, directionality). directionality =
        |net_drift| / path_range in [0,1]: ~0 = pure chop, ~1 = clean trend.
        ranging = low directionality AND vol (path) not blown out. We only
        quote when ranging, so a sustained trend can't run the maker over.
        """
        pts = [(ts, m) for ts, m in self._mids if now - ts <= self.p.regime_window_s]
        if len(pts) < 6:
            return False, 0.0, 1.0  # insufficient history -> treat as trend (stand flat, safe)
        mids = [m for _, m in pts]
        net = mids[-1] - mids[0]
        path = max(mids) - min(mids)
        direc = abs(net) / path if path > 1e-9 else 0.0
        ranging = (direc <= self.p.range_ratio_max) and (path <= self.p.max_path_usd)
        # sustained multi-minute trend gate — stand flat even if the last 40s looks
        # calm, so a one-way grind (like a $300/20min BTC run) can't pick us off.
        long_pts = [m for ts, m in self._mids if now - ts <= self.p.long_window_s]
        if len(long_pts) >= 10:
            long_drift = abs(long_pts[-1] - long_pts[0])
            if long_drift > self.p.long_drift_max:
                ranging = False
                direc = max(direc, 0.85)
        return ranging, net, direc

    # --- bridge helpers ---
    def quote(self):
        try:
            return self.c.get(f"/quote/{SYMBOL}").json()
        except Exception:
            return None

    def account(self):
        try:
            return self.c.get("/account").json()
        except Exception:
            return None

    def my_positions(self):
        try:
            return [x for x in self.c.get("/positions").json()
                    if x["symbol"] == SYMBOL and int(x.get("magic", 0)) == MAGIC]
        except Exception:
            return []

    def my_orders(self):
        try:
            return [x for x in self.c.get("/orders").json()
                    if x["symbol"] == SYMBOL and int(x.get("magic", 0)) == MAGIC]
        except Exception:
            return []

    def place_limit(self, kind: str, price: float, lots: float | None = None):
        vol = round(self.p.lot if lots is None else lots, 2)
        r = self.c.post("/limit", json={"symbol": SYMBOL, "kind": kind, "lots": vol,
                                        "price": round(price, 2), "comment": "maker", "magic": MAGIC}).json()
        return r

    def cancel(self, ticket: int):
        try:
            return self.c.post(f"/cancel/{ticket}").json()
        except Exception:
            return None

    def cancel_all_mine(self):
        for o in self.my_orders():
            self.cancel(o["ticket"])

    def market_close(self, ticket: int):
        try:
            return self.c.post(f"/close/{ticket}").json()
        except Exception:
            return None

    # --- core ---
    def run(self):
        acct = self.account()
        if not acct:
            print("no account — bridge down?"); return
        self.s.start_balance = self.s.last_balance = acct["balance"]
        print(f"MAKER start: balance={self.s.start_balance:.2f} lot={self.p.lot} "
              f"off={self.p.offset_usd} tp={self.p.tp_usd} stop={self.p.stop_usd}")
        log_event(evt="start", **acct, params=self.p.__dict__)
        # clean slate
        self.cancel_all_mine()

        while True:
            if KILL_FLAG.exists() or not ON_FLAG.exists():
                print("halt flag — flattening & exit")
                self.flatten_all()
                return
            try:
                self.tick()
            except Exception as exc:
                log_event(evt="tick_error", error=str(exc))
                print("tick error:", exc)
            time.sleep(self.p.poll_s)

    def _update_balance(self):
        acct = self.account()
        if acct:
            self.s.realized = acct["balance"] - self.s.start_balance
            self.s.last_balance = acct["balance"]
        return acct

    def tick(self):
        q = self.quote()
        if not q:
            return
        bid, ask = q["bid"], q["ask"]
        mid = (bid + ask) / 2.0
        spread = ask - bid
        now = time.time()
        self._mids.append((now, mid))

        pos = self.my_positions()
        orders = self.my_orders()

        # safety: never hold more than one inventory unit (double-fill whipsaw)
        if len(pos) > 1:
            print(f"  !! {len(pos)} positions — flatten all")
            log_event(evt="multi_position", n=len(pos))
            self.flatten_all()
            self.s.pos_side = 0
            return

        # session safety
        self._update_balance()
        if self.s.realized <= -self.p.session_loss_limit:
            if not self.s.halted:
                print(f"SESSION LOSS LIMIT hit realized={self.s.realized:.2f} — flatten & halt")
                log_event(evt="loss_limit", realized=self.s.realized)
            self.s.halted = True
            self.flatten_all()
            ON_FLAG.unlink(missing_ok=True)
            return
        if self.p.session_profit_target > 0 and self.s.realized >= self.p.session_profit_target:
            print(f"PROFIT TARGET hit realized={self.s.realized:.2f} — flatten & halt")
            log_event(evt="profit_target", realized=self.s.realized)
            self.flatten_all()
            ON_FLAG.unlink(missing_ok=True)
            return

        regime = self._regime(now)  # (ranging, net_drift, directionality)
        if pos:
            self.manage_position(pos[0], bid, ask, mid, now, orders, regime)
        else:
            # detect a just-closed position (inventory cleared)
            if self.s.pos_side != 0:
                self._on_flat()
            self.maintain_bracket(bid, ask, mid, spread, orders, regime)

    def _on_flat(self):
        acct = self._update_balance()
        self.s.n_round_trips += 1
        # crude win/loss attribution from balance delta vs last position mark
        print(f"  -> FLAT  realized_session={self.s.realized:+.2f}  round_trips={self.s.n_round_trips}")
        log_event(evt="flat", realized=self.s.realized, round_trips=self.s.n_round_trips)
        self.s.pos_side = 0
        self.s.pos_entry = 0.0

    def manage_position(self, pos, bid, ask, mid, now, orders, regime):
        side = 1 if pos["type"] == 0 else -1
        entry = pos["price_open"]
        pos_lots = round(float(pos["lots"]), 2)
        tp_price = round(entry + self.p.tp_usd if side == 1 else entry - self.p.tp_usd, 2)
        tp_kind = "sell_limit" if side == 1 else "buy_limit"
        tp_type = 3 if side == 1 else 2

        if self.s.pos_side == 0:
            # newly filled — clear ALL resting orders (the leftover bracket leg /
            # unfilled remainder) and post exactly ONE take-profit sized to the
            # ACTUAL filled volume so a partial fill can never flip our inventory.
            self.cancel_all_mine()
            self.s.pos_side = side
            self.s.pos_entry = entry
            self.s.pos_since = now
            r = self.place_limit(tp_kind, tp_price, lots=pos_lots)
            print(f"  FILL {'LONG' if side==1 else 'SHORT'} {pos_lots} @ {entry:.2f} "
                  f"(spread {ask-bid:.1f}) TP@{tp_price:.2f} ok={r.get('ok')}")
            log_event(evt="fill", side=side, lots=pos_lots, entry=entry, bid=bid, ask=ask, tp=tp_price)
            return

        held = now - self.s.pos_since
        # fast trend-exit: a trend has formed AGAINST us — bail before max_hold/stop
        ranging, net_drift, direc = regime
        if (not ranging) and direc >= self.p.trend_exit_ratio:
            if (side == 1 and net_drift < -self.p.tp_usd) or (side == -1 and net_drift > self.p.tp_usd):
                print(f"  TREND-EXIT {'long' if side==1 else 'short'} net={net_drift:+.1f} dir={direc:.2f}")
                self._exit_market(pos, "trend"); return
        # hard stop
        if side == 1 and bid <= entry - self.p.stop_usd:
            print(f"  STOP long: bid {bid:.1f} <= {entry - self.p.stop_usd:.1f}")
            self._exit_market(pos, "stop"); return
        if side == -1 and ask >= entry + self.p.stop_usd:
            print(f"  STOP short: ask {ask:.1f} >= {entry + self.p.stop_usd:.1f}")
            self._exit_market(pos, "stop"); return
        # max hold
        if held >= self.p.max_hold_s:
            print(f"  MAXHOLD {held:.0f}s — close at market")
            self._exit_market(pos, "maxhold"); return
        # keep exactly ONE correct TP (right side, price, AND size) resting; drop the rest
        good_tp = [o for o in orders if o["type"] == tp_type
                   and abs(o["price_open"] - tp_price) < 1.5
                   and abs(float(o["lots"]) - pos_lots) < 0.01]
        for o in orders:
            if o not in good_tp:
                self.cancel(o["ticket"])
        if not good_tp:
            r = self.place_limit(tp_kind, tp_price, lots=pos_lots)
            if r.get("ok"):
                log_event(evt="tp_reset", side=side, tp=tp_price, lots=pos_lots)

    def _exit_market(self, pos, reason):
        self.cancel_all_mine()
        r = self.market_close(pos["ticket"])
        log_event(evt="exit", reason=reason, ticket=pos["ticket"], result=r)
        time.sleep(0.4)
        self._on_flat()

    def maintain_bracket(self, bid, ask, mid, spread, orders, regime):
        ranging, net_drift, direc = regime
        # ONLY quote when ranging + spread sane. In a trend (or vol blowout) we
        # stand flat — this is the core of the smooth/uptrending behavior.
        if spread > self.p.max_spread_usd or not ranging:
            if orders:
                self.cancel_all_mine()
            return
        buy_px = round(bid - self.p.offset_usd, 2)
        sell_px = round(ask + self.p.offset_usd, 2)
        has_buy = any(o["type"] == 2 for o in orders)
        has_sell = any(o["type"] == 3 for o in orders)
        # re-center if drifted
        if self.s.bracket_center and abs(mid - self.s.bracket_center) > self.p.requote_drift_usd:
            self.cancel_all_mine()
            has_buy = has_sell = False
        if not has_buy:
            self.place_limit("buy_limit", buy_px)
        if not has_sell:
            self.place_limit("sell_limit", sell_px)
        self.s.bracket_center = mid

    def flatten_all(self):
        # retry until genuinely flat — a single market_close can silently fail
        for attempt in range(6):
            self.cancel_all_mine()
            pos = self.my_positions()
            if not pos:
                break
            for p in pos:
                self.market_close(p["ticket"])
            time.sleep(0.4)
        leftover = self.my_positions()
        self._update_balance()
        if leftover:
            print(f"!! FLATTEN INCOMPLETE — {len(leftover)} pos remain: "
                  f"{[p['ticket'] for p in leftover]}")
            log_event(evt="flatten_incomplete", tickets=[p["ticket"] for p in leftover])
        print(f"FLATTENED. session realized={self.s.realized:+.2f} round_trips={self.s.n_round_trips}")
        log_event(evt="flatten", realized=self.s.realized, round_trips=self.s.n_round_trips)


if __name__ == "__main__":
    p = MakerParams()
    # allow quick overrides: python -m live.brain.maker_loop lot=0.2 tp=16 ...
    for arg in sys.argv[1:]:
        if "=" in arg:
            k, v = arg.split("=", 1)
            if hasattr(p, k):
                setattr(p, k, type(getattr(p, k))(v))
    MakerLoop(p).run()
