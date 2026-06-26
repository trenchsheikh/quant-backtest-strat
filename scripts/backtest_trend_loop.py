"""Reality-check: backtest scalp/trend modes at the REAL competition BTC spread.

Engine spread_bps is a HALF-spread (each side), so full_spread_bps = 2 * engine_bps.
Live BTC full spread ~2.2 bps (~$14 round trip) => engine spread_bps = 1.1.
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from momq.hft_scalp_research import EntryMode, HftScalpConfig
from scripts.backtest_hft_scalp import run_hft_backtest, _prepare_features


def load_window(hours: float) -> pd.DataFrame:
    df = pd.read_parquet("research/player758/btc_1s_extended.parquet")
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    t1 = df["ts"].max()
    t0 = t1 - pd.Timedelta(hours=hours)
    w = df[(df["ts"] >= t0) & (df["ts"] <= t1)].copy()
    return w


def half_bps(full_bps: float) -> float:
    return full_bps / 2.0


def fmt(res) -> str:
    return (f"ret={res.return_pct:+6.2f}%  net=${res.net_pnl:+10.1f}  wr={res.win_rate*100:4.1f}%  "
            f"n={res.trades:5d}  med_hold={res.median_hold_s:5.1f}s  dd={res.max_dd_pct:+5.2f}%  "
            f"sh15={res.sharpe_15m:+.3f}  t/hr={res.trades_per_hour:6.0f}")


def main() -> None:
    for hrs, label in ((24, "LAST 24h"), (72, "LAST 72h")):
        bars = load_window(hrs)
        prepared = _prepare_features(bars)
        px0, px1 = bars["close"].iloc[0], bars["close"].iloc[-1]
        print(f"\n================ {label}: {bars['ts'].min()} .. {bars['ts'].max()} "
              f"({len(bars):,} s) BTC {px0:.0f}->{px1:.0f} ({(px1/px0-1)*100:+.2f}%) ================")

        # --- 1) Existing tight-TP modes across spreads: show the cliff ---
        print("\n-- tight-TP taker modes (player758-style) vs spread --")
        for mode, tp, sl, hold, mf, ms, mt in [
            (EntryMode.FLIP,      1.75, 1.40, 3.5, 2, 8, 0.60),
            (EntryMode.MOM_MICRO, 2.00, 1.20, 3.0, 2, 5, 0.58),
            (EntryMode.MOMENTUM,  2.00, 1.20, 3.0, 2, 5, 0.58),
        ]:
            line = f"  {mode.value:10s} tp{tp} sl{sl}: "
            for full in (0.0, 0.2, 2.2):
                cfg = HftScalpConfig(entry_mode=mode, tp_usd=tp, sl_usd=sl, max_hold_s=hold,
                                     spread_bps=half_bps(full), mom_fast_s=mf, mom_slow_s=ms,
                                     micro_thresh=mt, min_range_usd=0.3, min_vol_z=-1.0)
                res, _ = run_hft_backtest(bars, cfg, prepared=prepared)
                line += f"[{full:.1f}bps ret={res.return_pct:+5.2f}% wr={res.win_rate*100:4.1f}%] "
            print(line)

        # --- 2) TREND mode (wide TP/SL, magnitude gate) at REAL 2.2 bps spread ---
        print("\n-- TREND mode @ real 2.2 bps full spread (~$14 r/t), 1 BTC lot --")
        hdr = "  tp/sl/hold/trendmin/fast/slow/trail :"
        print(hdr)
        best = None
        for tp in (20, 30, 40, 60):
            for sl in (15, 20, 30):
                for hold in (30, 60, 120):
                    for tmin in (8, 12, 18):
                        for (mf, ms) in ((3, 10), (5, 15), (2, 8)):
                            for trail in (0.0, 0.5):
                                cfg = HftScalpConfig(
                                    entry_mode=EntryMode.TREND, tp_usd=tp, sl_usd=sl,
                                    max_hold_s=hold, spread_bps=half_bps(2.2),
                                    mom_fast_s=mf, mom_slow_s=ms, trend_min_usd=tmin,
                                    min_range_usd=0.0, min_vol_z=-5.0,
                                    use_trailing_after_pct=trail, flip_on_close=False,
                                )
                                res, _ = run_hft_backtest(bars, cfg, prepared=prepared)
                                if res.trades < 10:
                                    continue
                                key = res.return_pct
                                if best is None or key > best[0]:
                                    best = (key, cfg, res, (tp, sl, hold, tmin, mf, ms, trail))
        if best:
            _, cfg, res, p = best
            print(f"  BEST TREND @2.2bps: tp{p[0]} sl{p[1]} hold{p[2]} tmin{p[3]} "
                  f"f{p[4]}/s{p[5]} trail{p[6]}")
            print(f"      {fmt(res)}")
            # stress at 3.0 bps
            cfg2 = HftScalpConfig(**{**cfg.__dict__, "spread_bps": half_bps(3.0)})
            r2, _ = run_hft_backtest(bars, cfg2, prepared=prepared)
            print(f"      stress @3.0bps: {fmt(r2)}")
            cfg0 = HftScalpConfig(**{**cfg.__dict__, "spread_bps": 0.0})
            r0, _ = run_hft_backtest(bars, cfg0, prepared=prepared)
            print(f"      ref    @0.0bps: {fmt(r0)}")
        else:
            print("  no TREND config produced >=10 trades")

        # --- 3) Pure directional benchmark: hold short/long whole window (1 BTC) ---
        move = px1 - px0
        print(f"\n-- benchmark: holding 1 BTC short whole window = ${-move:+.0f}; "
              f"long = ${move:+.0f} (before spread) --")


if __name__ == "__main__":
    main()
