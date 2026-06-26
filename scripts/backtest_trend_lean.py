"""Lean reality-check on 24h window. Full spread = 2*engine_bps; live ~2.2 bps."""
from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from momq.hft_scalp_research import EntryMode, HftScalpConfig
from scripts.backtest_hft_scalp import run_hft_backtest, _prepare_features

df = pd.read_parquet("research/player758/btc_1s_extended.parquet")
df["ts"] = pd.to_datetime(df["ts"], utc=True)
HRS = float(sys.argv[1]) if len(sys.argv) > 1 else 24.0
bars = df[df["ts"] >= df["ts"].max() - pd.Timedelta(hours=HRS)].copy()
prepared = _prepare_features(bars)
px0, px1 = bars["close"].iloc[0], bars["close"].iloc[-1]
print(f"WINDOW {bars['ts'].min()} .. {bars['ts'].max()} ({len(bars):,}s) "
      f"BTC {px0:.0f}->{px1:.0f} ({(px1/px0-1)*100:+.2f}%)")
hb = lambda full: full / 2.0
def fmt(r):
    return (f"ret={r.return_pct:+6.2f}% net=${r.net_pnl:+9.0f} wr={r.win_rate*100:4.1f}% "
            f"n={r.trades:5d} hold={r.median_hold_s:5.1f}s dd={r.max_dd_pct:+5.2f}% sh={r.sharpe_15m:+.2f} t/hr={r.trades_per_hour:5.0f}")

print("\n[A] tight-TP taker modes vs spread (the cliff):")
for mode, tp, sl, h, mf, ms, mt in [
    (EntryMode.FLIP, 1.75, 1.40, 3.5, 2, 8, 0.60),
    (EntryMode.MOM_MICRO, 2.0, 1.2, 3.0, 2, 5, 0.58)]:
    for full in (0.0, 0.2, 2.2):
        cfg = HftScalpConfig(entry_mode=mode, tp_usd=tp, sl_usd=sl, max_hold_s=h,
            spread_bps=hb(full), mom_fast_s=mf, mom_slow_s=ms, micro_thresh=mt,
            min_range_usd=0.3, min_vol_z=-1.0)
        r, _ = run_hft_backtest(bars, cfg, prepared=prepared)
        print(f"  {mode.value:10s} @{full:.1f}bps: {fmt(r)}")

print("\n[B] TREND mode grid @ real 2.2 bps full spread (1 BTC lot):")
best = None
for tp in (25, 40, 60):
    for sl in (18, 30):
        for hold in (60, 120):
            for tmin in (10, 18):
                for (mf, ms) in ((3, 10), (5, 15)):
                    for trail in (0.0, 0.5):
                        cfg = HftScalpConfig(entry_mode=EntryMode.TREND, tp_usd=tp, sl_usd=sl,
                            max_hold_s=hold, spread_bps=hb(2.2), mom_fast_s=mf, mom_slow_s=ms,
                            trend_min_usd=tmin, min_range_usd=0.0, min_vol_z=-5.0,
                            use_trailing_after_pct=trail, flip_on_close=False)
                        r, _ = run_hft_backtest(bars, cfg, prepared=prepared)
                        if r.trades < 8:
                            continue
                        if best is None or r.return_pct > best[0]:
                            best = (r.return_pct, cfg, r, (tp, sl, hold, tmin, mf, ms, trail))
if best:
    _, cfg, r, p = best
    print(f"  BEST: tp{p[0]} sl{p[1]} hold{p[2]} tmin{p[3]} f{p[4]}/s{p[5]} trail{p[6]}")
    print(f"    @2.2bps: {fmt(r)}")
    for full in (0.0, 3.0):
        c2 = HftScalpConfig(**{**cfg.__dict__, "spread_bps": hb(full)})
        r2, _ = run_hft_backtest(bars, c2, prepared=prepared)
        print(f"    @{full:.1f}bps: {fmt(r2)}")
else:
    print("  no TREND config with >=8 trades")

move = px1 - px0
print(f"\n[C] benchmark hold 1 BTC: short=${-move:+.0f} long=${move:+.0f} (pre-spread)")
