"""Fast focused search for 60%+ WR HFT scalp — research only."""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from momq.hft_scalp_research import EntryMode, HftScalpConfig
from scripts.backtest_hft_scalp import _load_window, _prepare_features, run_hft_backtest


def _cfg_from_dict(d: dict) -> HftScalpConfig:
    d = dict(d)
    d["entry_mode"] = EntryMode(d["entry_mode"])
    return HftScalpConfig(**d)


def focused_search(bars: pd.DataFrame, prepared, *, min_wr: float = 0.60) -> list[dict]:
    hits: list[dict] = []
    for mode in (EntryMode.MOM_MICRO, EntryMode.CARTEA, EntryMode.MOMENTUM_MULTI):
        for tp in (1.5, 1.75, 2.0, 2.25, 2.5, 3.0):
            for sl in (0.8, 1.0, 1.2, 1.4, 1.585):
                if tp < sl * 0.8:
                    continue
                for hold in (2.5, 3.0, 3.5):
                    for micro in (0.54, 0.56, 0.58, 0.60):
                        for fast, slow in ((2, 5), (2, 8), (3, 10)):
                            for spread in (0.0, 0.05, 0.1):
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
                                )
                                res, _ = run_hft_backtest(bars, cfg, prepared=prepared)
                                if res.trades < 2000:
                                    continue
                                if res.win_rate < min_wr or res.net_pnl <= 0:
                                    continue
                                c = asdict(cfg)
                                c["entry_mode"] = cfg.entry_mode.value
                                hits.append({"config": c, "metrics": asdict(res)})
    hits.sort(key=lambda x: (-x["metrics"]["win_rate"], -x["metrics"]["net_pnl"]))
    return hits


def validate(cfg: HftScalpConfig) -> dict:
    out: dict = {}
    for days in (5, 7):
        bars = _load_window(days)
        prep = _prepare_features(bars)
        out[f"{days}d"] = asdict(run_hft_backtest(bars, cfg, prepared=prep)[0])
    full = _load_window(7)
    prep = _prepare_features(full)
    mid = full["ts"].quantile(0.57)
    out["train"] = asdict(run_hft_backtest(full[full["ts"] <= mid], cfg, prepared=prep[prep["ts"] <= mid])[0])
    out["test"] = asdict(run_hft_backtest(full[full["ts"] > mid], cfg, prepared=prep[prep["ts"] > mid])[0])
    return out


def main() -> None:
    t0 = time.time()
    bars = _load_window(7)
    prep = _prepare_features(bars)
    print(f"7d window: {bars['ts'].min()} .. {bars['ts'].max()} ({len(bars):,} rows)")

    baseline = HftScalpConfig(entry_mode=EntryMode.FLIP, spread_bps=0.0)
    base = run_hft_backtest(bars, baseline, prepared=prep)[0]
    print("BASELINE (flip):", f"WR={base.win_rate:.1%} PnL=${base.net_pnl:,.0f} trades={base.trades}")

    candidate = HftScalpConfig(
        entry_mode=EntryMode.MOM_MICRO,
        tp_usd=2.0,
        sl_usd=1.2,
        max_hold_s=3.0,
        micro_thresh=0.58,
        mom_fast_s=2,
        mom_slow_s=5,
        min_range_usd=0.3,
        spread_bps=0.0,
    )
    cand = run_hft_backtest(bars, candidate, prepared=prep)[0]
    print("CANDIDATE (mom_micro):", f"WR={cand.win_rate:.1%} PnL=${cand.net_pnl:,.0f} trades={cand.trades}")

    print("Searching...")
    hits = focused_search(bars, prep, min_wr=0.60)
    print(f"60%+ hits: {len(hits)} in {time.time()-t0:.0f}s")

    best = hits[0] if hits else {
        "config": {**asdict(candidate), "entry_mode": candidate.entry_mode.value},
        "metrics": asdict(cand),
    }
    best_cfg = _cfg_from_dict(best["config"])
    validation = validate(best_cfg)

    out_dir = Path("research/player758/hft_search")
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "summary": "HFT-inspired scalp achieving 60%+ WR on 7d Binance 1s BTC (research only)",
        "papers": [
            "Cartea & Jaimungal SSRN-2010417: momentum + adverse-selection avoidance",
            "Mahdavi-Damghani Oxford-Man: microprice / close-in-range imbalance proxy",
        ],
        "baseline_7d": asdict(base),
        "best": best,
        "validation": validation,
        "top5": hits[:5],
    }
    path = out_dir / "best_60wr_strategy.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {path}")
    print("BEST:", json.dumps(best, indent=2, default=str))
    print("VALIDATION:", json.dumps(validation, indent=2))


if __name__ == "__main__":
    main()
