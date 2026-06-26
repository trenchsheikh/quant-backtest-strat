"""Backtest HFT-inspired BTC micro-scalp variants — research only.

Usage:
    py -3.10 scripts/backtest_hft_scalp.py
    py -3.10 scripts/backtest_hft_scalp.py --search
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from momq.hft_scalp_research import EntryMode, HftScalpConfig, close_in_range, entry_direction
from scripts.backtest_btc_scalp import BacktestResult, _sharpe_15m


def _prepare_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.sort_values("ts").reset_index(drop=True).copy()
    out["mid"] = out["close"]
    out["range_usd"] = out["high"] - out["low"]
    h = out["high"].to_numpy()
    l = out["low"].to_numpy()
    c = out["close"].to_numpy()
    rng = np.maximum(h - l, 0.0)
    out["cir"] = np.where(rng > 0, (c - l) / rng, 0.5)
    vol = out["volume"].astype(float)
    roll = vol.rolling(60, min_periods=10)
    out["vol_z"] = ((vol - roll.mean()) / roll.std().replace(0, np.nan)).fillna(0.0)
    for s in range(1, 21):
        out[f"mom_{s}s"] = out["mid"].diff(s)
    out["bar_delta"] = out["mid"].diff(1)
    return out


def run_hft_backtest(
    bars: pd.DataFrame,
    cfg: HftScalpConfig,
    *,
    prepared: pd.DataFrame | None = None,
) -> tuple[BacktestResult, pd.DataFrame]:
    df = prepared if prepared is not None else _prepare_features(bars)
    half = cfg.spread_bps / 10_000.0
    max_lb = max(cfg.mom_slow_s, cfg.mom_fast_s, 15)
    n = len(df)

    ts_arr = df["ts"].to_numpy()
    mid = df["mid"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    range_usd = df["range_usd"].to_numpy(dtype=float)
    vol_z = df["vol_z"].to_numpy(dtype=float)
    mom_fast = df[f"mom_{cfg.mom_fast_s}s"].to_numpy(dtype=float)
    mom_slow = df[f"mom_{cfg.mom_slow_s}s"].to_numpy(dtype=float)
    cir = df["cir"].to_numpy(dtype=float)
    bar_delta = df["bar_delta"].to_numpy(dtype=float)

    pos = 0
    entry_px = 0.0
    entry_i = 0
    pending_flip = 1
    last_exit_reason: str | None = None
    last_exit_side: int | None = None
    sl_streak = 0
    best_mtm = 0.0

    trades: list[dict] = []
    equity = cfg.initial_equity
    eq_pts: list[tuple] = []

    for i in range(max_lb, n):
        m = mid[i]
        bid = m * (1.0 - half)
        ask = m * (1.0 + half)
        hi_bid = high[i] * (1.0 - half)
        lo_bid = low[i] * (1.0 - half)
        hi_ask = high[i] * (1.0 + half)
        lo_ask = low[i] * (1.0 + half)

        if pos != 0:
            hold_s = (ts_arr[i] - ts_arr[entry_i]) / np.timedelta64(1, "s")
            hold_s = float(hold_s)
            if pos == 1:
                mtm = (bid - entry_px) * cfg.lot_btc
                best_bar = (hi_bid - entry_px) * cfg.lot_btc
                worst_bar = (lo_bid - entry_px) * cfg.lot_btc
            else:
                mtm = (entry_px - ask) * cfg.lot_btc
                best_bar = (entry_px - lo_ask) * cfg.lot_btc
                worst_bar = (entry_px - hi_ask) * cfg.lot_btc
            best_mtm = max(best_mtm, mtm)

            reason = ""
            realized = mtm
            if worst_bar <= -cfg.sl_usd:
                realized = -cfg.sl_usd
                reason = "sl"
            elif best_bar >= cfg.tp_usd:
                realized = cfg.tp_usd
                reason = "tp"
            elif (
                cfg.use_trailing_after_pct > 0
                and best_mtm >= cfg.tp_usd * cfg.use_trailing_after_pct
                and mtm <= best_mtm * 0.35
            ):
                realized = mtm
                reason = "trail"
            elif hold_s >= cfg.max_hold_s:
                realized = mtm
                reason = "time"

            if reason:
                equity += realized
                trades.append({
                    "close_ts": ts_arr[i],
                    "direction": "long" if pos == 1 else "short",
                    "hold_s": hold_s,
                    "pnl": realized,
                    "reason": reason,
                })
                eq_pts.append((ts_arr[i], equity))
                last_exit_reason = reason
                last_exit_side = pos
                if reason == "sl":
                    sl_streak += 1
                else:
                    sl_streak = 0
                if cfg.flip_on_close:
                    pending_flip = -pos
                pos = 0
                best_mtm = 0.0
            continue

        if cfg.pause_after_sl_streak > 0 and sl_streak >= cfg.pause_after_sl_streak:
            sl_streak = 0
            continue
        if range_usd[i] < cfg.min_range_usd:
            continue
        if vol_z[i] < cfg.min_vol_z:
            continue

        direction = entry_direction(
            mode=cfg.entry_mode,
            pending_flip=pending_flip,
            mom_fast=float(mom_fast[i]),
            mom_slow=float(mom_slow[i]),
            cir=float(cir[i]),
            last_exit_reason=last_exit_reason,
            last_exit_side=last_exit_side,
            adverse_move_usd=float(bar_delta[i]) * cfg.lot_btc,
            cfg=cfg,
        )
        if direction is None:
            continue

        if direction == 1:
            pos = 1
            entry_px = ask
        else:
            pos = -1
            entry_px = bid
        entry_i = i
        best_mtm = 0.0

    if not eq_pts:
        eq_pts.append((ts_arr[max_lb], equity))
    eq_series = pd.Series(
        [e for _, e in eq_pts],
        index=pd.DatetimeIndex([pd.Timestamp(t) for t, _ in eq_pts]),
    ).sort_index()

    tdf = pd.DataFrame(trades)
    t_end = pd.Timestamp(ts_arr[-1])
    t_start = pd.Timestamp(ts_arr[max_lb])
    hours = max((t_end - t_start).total_seconds() / 3600.0, 1e-9)
    runmax = eq_series.cummax()
    dd = ((eq_series / runmax) - 1.0).min() * 100.0 if len(eq_series) else 0.0
    n = len(tdf)
    result = BacktestResult(
        trades=n,
        net_pnl=float(tdf["pnl"].sum()) if n else 0.0,
        return_pct=100.0 * (equity / cfg.initial_equity - 1.0),
        win_rate=float((tdf["pnl"] > 0).mean()) if n else 0.0,
        avg_pnl=float(tdf["pnl"].mean()) if n else 0.0,
        median_hold_s=float(tdf["hold_s"].median()) if n else 0.0,
        max_dd_pct=float(dd),
        sharpe_15m=_sharpe_15m(eq_series),
        trades_per_hour=n / hours,
    )
    return result, tdf


def _load_window(days: int = 7) -> pd.DataFrame:
    path = Path("research/player758/btc_1s_extended.parquet")
    df = pd.read_parquet(path)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    t1 = df["ts"].max()
    t0 = t1 - pd.Timedelta(days=days)
    return df[(df["ts"] >= t0) & (df["ts"] <= t1)].copy()


def search_configs(bars: pd.DataFrame, min_wr: float = 0.60) -> list[dict]:
    hits: list[dict] = []
    modes = list(EntryMode)
    for mode in modes:
        for tp in (1.5, 2.0, 2.5, 3.0, 3.67, 4.0):
            for sl in (0.8, 1.0, 1.2, 1.585, 2.0, 2.5):
                if tp <= sl * 0.8:
                    continue
                for hold in (2.5, 3.0, 3.5, 4.5, 6.0):
                    for spread in (0.0, 0.05, 0.1):
                        for micro in (0.52, 0.55, 0.58, 0.62):
                            for fast, slow in ((2, 5), (2, 10), (3, 8), (1, 3)):
                                if slow <= fast:
                                    continue
                                cfg = HftScalpConfig(
                                    tp_usd=tp,
                                    sl_usd=sl,
                                    max_hold_s=hold,
                                    spread_bps=spread,
                                    entry_mode=mode,
                                    mom_fast_s=fast,
                                    mom_slow_s=slow,
                                    micro_thresh=micro,
                                    min_range_usd=0.3,
                                    min_vol_z=-1.0,
                                    adverse_buffer_usd=0.5,
                                )
                                res, _ = run_hft_backtest(bars, cfg)
                                if res.trades < 500:
                                    continue
                                if res.win_rate < min_wr:
                                    continue
                                if res.net_pnl <= 0:
                                    continue
                                hits.append({
                                    "config": asdict(cfg),
                                    "metrics": asdict(res),
                                    "score": res.win_rate * 100 + res.return_pct,
                                })
    hits.sort(key=lambda x: (-x["metrics"]["win_rate"], -x["metrics"]["net_pnl"]))
    return hits


def refined_search(bars: pd.DataFrame, min_wr: float = 0.60) -> list[dict]:
    """Second pass around promising regions."""
    hits: list[dict] = []
    for mode in (EntryMode.MOM_MICRO, EntryMode.CARTEA, EntryMode.MOMENTUM_MULTI, EntryMode.MICROPRICE):
        for tp in np.arange(1.0, 3.5, 0.25):
            for sl in np.arange(0.6, 2.2, 0.2):
                if tp < sl * 0.7:
                    continue
                for hold in (2.0, 2.5, 3.0, 3.5, 4.0, 5.0):
                    for micro in np.arange(0.50, 0.70, 0.02):
                        for fast, slow in ((1, 3), (2, 5), (2, 8), (3, 10), (5, 15)):
                            if slow <= fast:
                                continue
                            for min_r in (0.0, 0.3, 0.8):
                                for vol_z in (-1.5, -0.5, 0.0):
                                    cfg = HftScalpConfig(
                                        tp_usd=float(round(tp, 2)),
                                        sl_usd=float(round(sl, 2)),
                                        max_hold_s=hold,
                                        spread_bps=0.0,
                                        entry_mode=mode,
                                        mom_fast_s=fast,
                                        mom_slow_s=slow,
                                        micro_thresh=float(round(micro, 2)),
                                        min_range_usd=min_r,
                                        min_vol_z=vol_z,
                                        adverse_buffer_usd=0.3,
                                        flip_on_close=(mode != EntryMode.MOMENTUM_MULTI),
                                    )
                                    res, _ = run_hft_backtest(bars, cfg)
                                    if res.trades < 300:
                                        continue
                                    if res.win_rate < min_wr:
                                        continue
                                    if res.net_pnl <= 0:
                                        continue
                                    hits.append({
                                        "config": asdict(cfg),
                                        "metrics": asdict(res),
                                    })
    hits.sort(key=lambda x: (-x["metrics"]["win_rate"], -x["metrics"]["net_pnl"]))
    return hits


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--search", action="store_true")
    p.add_argument("--min-wr", type=float, default=0.60)
    args = p.parse_args()

    bars = _load_window(args.days)
    print(f"Window: {bars['ts'].min()} .. {bars['ts'].max()} ({len(bars):,} rows)")

    baseline = HftScalpConfig(entry_mode=EntryMode.FLIP, spread_bps=0.0)
    base_res, _ = run_hft_backtest(bars, baseline)
    print("BASELINE flip:", asdict(base_res))

    if args.search:
        print("Phase 1 grid search...")
        hits = search_configs(bars, min_wr=args.min_wr)
        print(f"Phase 1 hits (>={args.min_wr:.0%} WR, positive PnL): {len(hits)}")
        if not hits:
            print("Phase 2 refined search...")
            hits = refined_search(bars, min_wr=args.min_wr)
            print(f"Phase 2 hits: {len(hits)}")

        out_dir = Path("research/player758/hft_search")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "results_60wr.json"
        payload = {
            "days": args.days,
            "window": f"{bars['ts'].min()} .. {bars['ts'].max()}",
            "baseline": asdict(base_res),
            "hits": hits[:50],
            "best": hits[0] if hits else None,
        }
        out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"Wrote {out_path}")
        if hits:
            b = hits[0]
            print("BEST:", json.dumps(b, indent=2, default=str))
        else:
            print("No config met criteria — trying relaxed search at 55%...")
            hits55 = refined_search(bars, min_wr=0.55)
            if hits55:
                print("Best at 55%:", json.dumps(hits55[0], indent=2, default=str))
        return

    # Default: run best candidate from research if exists
    cfg = HftScalpConfig(
        entry_mode=EntryMode.MOM_MICRO,
        tp_usd=2.0,
        sl_usd=1.2,
        max_hold_s=3.0,
        micro_thresh=0.58,
        mom_fast_s=2,
        mom_slow_s=5,
    )
    res, trades = run_hft_backtest(bars, cfg)
    print("CANDIDATE:", asdict(res))
    out_dir = Path("research/player758/hft_search")
    out_dir.mkdir(parents=True, exist_ok=True)
    trades.to_csv(out_dir / "candidate_trades.csv", index=False)


if __name__ == "__main__":
    main()
