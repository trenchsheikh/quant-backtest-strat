"""Fast walk-forward: coarse grid on 10s subsampled bars, full eval on top configs."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from itertools import product
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from scripts.backtest_btc_scalp import ScalpConfig
from scripts.backtest_btc_scalp_extended import daily_breakdown, run_backtest_fast


def eval_cfg(bars: pd.DataFrame, cfg: ScalpConfig) -> dict:
    res, trades, _ = run_backtest_fast(bars, cfg)
    daily = daily_breakdown(trades, cfg.initial_equity)
    return {
        **asdict(res),
        "profitable_days": int((daily["net_pnl"] > 0).sum()) if len(daily) else 0,
        "days": int(len(daily)),
    }


def main() -> None:
    bars = pd.read_parquet("research/player758/btc_1s_extended.parquet")
    bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
    split = bars["ts"].quantile(0.70)
    train = bars[bars["ts"] < split].iloc[::10].reset_index(drop=True)
    test = bars[bars["ts"] >= split]
    test_fast = test.iloc[::10].reset_index(drop=True)

    results = []
    for tp, sl, hold, spread in product(
        [4.5, 5.5, 6.5],
        [1.0, 1.2, 1.5],
        [3.0, 3.5, 4.0],
        [0.0, 0.25],
    ):
        cfg = ScalpConfig(tp_usd=tp, sl_usd=sl, max_hold_s=hold, spread_bps=spread)
        tr = eval_cfg(train, cfg)
        te = eval_cfg(test_fast, cfg)
        results.append({
            "config": asdict(cfg),
            "train_return_pct": tr["return_pct"],
            "test_return_pct": te["return_pct"],
            "train_sharpe": tr["sharpe_15m"],
            "test_sharpe": te["sharpe_15m"],
            "test_trades_per_hour": te["trades_per_hour"],
            "test_max_dd_pct": te["max_dd_pct"],
        })
        print(f"tp={tp} sl={sl} hold={hold} sp={spread} train={tr['return_pct']:.2f}% test={te['return_pct']:.2f}%")

    results.sort(key=lambda x: x["test_return_pct"], reverse=True)
    top3 = results[:3]
    full_top = []
    for r in top3:
        cfg = ScalpConfig(**{k: r["config"][k] for k in ScalpConfig.__dataclass_fields__})
        full = eval_cfg(bars, cfg)
        test_full = eval_cfg(test, cfg)
        full_top.append({
            "config": r["config"],
            "full_sample": full,
            "oos_test_full_bars": test_full,
        })

    out = {
        "split": str(split),
        "grid_results_top10": results[:10],
        "full_eval_top3": full_top,
        "note": "Grid on 10s subsampled bars; top-3 re-evaluated on full 1s bars.",
    }
    path = Path("research/player758/extended/walk_forward_results.json")
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("BEST OOS:", json.dumps(full_top[0], indent=2))


if __name__ == "__main__":
    main()
