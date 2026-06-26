"""Extended multi-day backtest for BTC ping-pong scalp with daily breakdown."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.backtest_btc_scalp import ScalpConfig, BacktestResult, _sharpe_15m


def run_backtest_fast(bars: pd.DataFrame, cfg: ScalpConfig) -> tuple[BacktestResult, pd.DataFrame, pd.Series]:
    """Numpy-accelerated 1s backtest (same logic as backtest_btc_scalp.run_backtest)."""
    df = bars.sort_values("ts").reset_index(drop=True)
    n = len(df)
    if n < cfg.momentum_lookback_s + 2:
        empty = pd.DataFrame()
        eq = pd.Series([cfg.initial_equity], index=[df["ts"].iloc[0]] if n else pd.DatetimeIndex([]))
        return BacktestResult(0, 0, 0, 0, 0, 0, 0, 0, 0), empty, eq

    half = cfg.spread_bps / 10_000.0
    close = df["close"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    times = df["ts"]

    pos = 0
    entry_px = 0.0
    entry_i = 0
    pending_dir = 1

    trade_rows: list[dict] = []
    equity = cfg.initial_equity
    eq_ts: list[pd.Timestamp] = []
    eq_val: list[float] = []

    lb0 = cfg.momentum_lookback_s
    for i in range(lb0, n):
        ts = times.iloc[i]
        mid = close[i]
        bid = mid * (1.0 - half)
        ask = mid * (1.0 + half)
        hi_bid = high[i] * (1.0 - half)
        lo_bid = low[i] * (1.0 - half)
        hi_ask = high[i] * (1.0 + half)
        lo_ask = low[i] * (1.0 - half)

        if pos != 0:
            hold_s = (ts - times.iloc[entry_i]).total_seconds()
            lot = cfg.lot_btc
            if pos == 1:
                best = (hi_bid - entry_px) * lot
                worst = (lo_bid - entry_px) * lot
                mtm = (bid - entry_px) * lot
            else:
                best = (entry_px - lo_ask) * lot
                worst = (entry_px - hi_ask) * lot
                mtm = (entry_px - ask) * lot

            reason = ""
            realized = mtm
            if best >= cfg.tp_usd:
                realized, reason = cfg.tp_usd, "tp"
            elif worst <= -cfg.sl_usd:
                realized, reason = -cfg.sl_usd, "sl"
            elif hold_s >= cfg.max_hold_s:
                realized, reason = mtm, "time"

            if reason:
                equity += realized
                trade_rows.append({
                    "close_ts": ts,
                    "direction": "long" if pos == 1 else "short",
                    "hold_s": hold_s,
                    "pnl": realized,
                    "reason": reason,
                })
                eq_ts.append(ts)
                eq_val.append(equity)
                if cfg.flip_on_close:
                    pending_dir = -pos
                pos = 0
            continue

        direction = pending_dir if cfg.flip_on_close else (
            1 if close[i] >= close[i - lb0] else -1
        )
        if direction == 1:
            pos, entry_px = 1, ask
        else:
            pos, entry_px = -1, bid
        entry_i = i

    tdf = pd.DataFrame(trade_rows)
    if eq_ts:
        eq_series = pd.Series(eq_val, index=pd.DatetimeIndex(eq_ts), name="equity")
    else:
        eq_series = pd.Series([cfg.initial_equity], index=[times.iloc[0]], name="equity")

    hours = max((times.iloc[-1] - times.iloc[0]).total_seconds() / 3600.0, 1e-9)
    runmax = eq_series.cummax()
    dd = ((eq_series / runmax) - 1.0).min() * 100.0 if len(eq_series) else 0.0
    nt = len(tdf)
    res = BacktestResult(
        trades=nt,
        net_pnl=float(tdf["pnl"].sum()) if nt else 0.0,
        return_pct=100.0 * (equity / cfg.initial_equity - 1.0),
        win_rate=float((tdf["pnl"] > 0).mean()) if nt else 0.0,
        avg_pnl=float(tdf["pnl"].mean()) if nt else 0.0,
        median_hold_s=float(tdf["hold_s"].median()) if nt else 0.0,
        max_dd_pct=float(dd),
        sharpe_15m=_sharpe_15m(eq_series),
        trades_per_hour=nt / hours,
    )
    return res, tdf, eq_series


def daily_breakdown(trades: pd.DataFrame, initial_equity: float = 1_000_000.0) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    t = trades.copy()
    t["day"] = pd.to_datetime(t["close_ts"], utc=True).dt.floor("D")
    g = t.groupby("day").agg(
        trades=("pnl", "count"),
        net_pnl=("pnl", "sum"),
        win_rate=("pnl", lambda s: (s > 0).mean()),
        avg_pnl=("pnl", "mean"),
        tp_rate=("reason", lambda s: (s == "tp").mean()),
        sl_rate=("reason", lambda s: (s == "sl").mean()),
    )
    g["return_pct"] = 100.0 * g["net_pnl"] / initial_equity
    g["cum_pnl"] = g["net_pnl"].cumsum()
    g["cum_return_pct"] = 100.0 * g["cum_pnl"] / initial_equity
    out = g.reset_index()
    out["day"] = out["day"].astype(str)
    return out


def run_extended(
    bars_path: Path,
    out_dir: Path,
    configs: dict[str, ScalpConfig],
) -> dict:
    bars = pd.read_parquet(bars_path)
    bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict = {
        "bars_path": str(bars_path),
        "bars_rows": len(bars),
        "start": str(bars["ts"].min()),
        "end": str(bars["ts"].max()),
        "configs": {},
    }

    for name, cfg in configs.items():
        print(f"Running {name} ...")
        res, trades, eq = run_backtest_fast(bars, cfg)
        daily = daily_breakdown(trades, cfg.initial_equity)

        # volatility regime: daily BTC return std quintiles
        bars_d = bars.set_index("ts")["close"].resample("1h").last().pct_change()
        vol = float(bars_d.std() * np.sqrt(24)) if len(bars_d) > 2 else 0.0

        cfg_report = {
            "config": asdict(cfg),
            "aggregate": asdict(res),
            "hourly_btc_vol_ann": vol,
            "exit_mix": trades["reason"].value_counts(normalize=True).to_dict() if len(trades) else {},
            "daily": daily.to_dict(orient="records") if len(daily) else [],
            "daily_summary": {
                "profitable_days": int((daily["net_pnl"] > 0).sum()) if len(daily) else 0,
                "total_days": int(len(daily)),
                "best_day_pnl": float(daily["net_pnl"].max()) if len(daily) else 0.0,
                "worst_day_pnl": float(daily["net_pnl"].min()) if len(daily) else 0.0,
                "median_daily_pnl": float(daily["net_pnl"].median()) if len(daily) else 0.0,
                "pct_profitable_days": float((daily["net_pnl"] > 0).mean()) if len(daily) else 0.0,
            },
        }
        report["configs"][name] = cfg_report

        trades.to_csv(out_dir / f"trades_{name}.csv", index=False)
        daily.to_csv(out_dir / f"daily_{name}.csv", index=False)
        eq.to_csv(out_dir / f"equity_{name}.csv")

    out_path = out_dir / "extended_backtest_results.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"wrote {out_path}")
    return report


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--bars", default="research/player758/btc_1s_extended.parquet")
    p.add_argument("--out-dir", default="research/player758/extended")
    args = p.parse_args()

    configs = {
        "player_observed": ScalpConfig(
            tp_usd=3.67, sl_usd=1.58, max_hold_s=4.5, spread_bps=0.25,
        ),
        "grid_fitted": ScalpConfig(
            tp_usd=5.5, sl_usd=1.2, max_hold_s=3.5, spread_bps=0.0,
        ),
        "conservative": ScalpConfig(
            tp_usd=4.0, sl_usd=1.5, max_hold_s=4.0, spread_bps=0.5,
        ),
    }
    report = run_extended(Path(args.bars), Path(args.out_dir), configs)
    # compact stdout
    for name, block in report["configs"].items():
        a = block["aggregate"]
        d = block["daily_summary"]
        print(
            f"\n{name}: ret={a['return_pct']:.2f}% pnl=${a['net_pnl']:,.0f} "
            f"trades={a['trades']:,} wr={a['win_rate']:.1%} dd={a['max_dd_pct']:.3f}% "
            f"sharpe={a['sharpe_15m']:.2f} profitable_days={d['profitable_days']}/{d['total_days']}"
        )


if __name__ == "__main__":
    main()
