"""Quick leaderboard-proxy sweep for tournament variants."""
from __future__ import annotations
import copy
import glob
import os
from dataclasses import replace

import pandas as pd

from momq.resample import load_aligned
from momq.tournament_config import TournamentConfig
from momq.tournament_engine import run_all_competition_rounds
from momq.metrics import leaderboard_proxy
from momq import tournament_config as tc


def eval_cfg(name: str, cfg: TournamentConfig, mids, spreads) -> dict:
    results = run_all_competition_rounds(mids, spreads, cfg=cfg)
    rows = []
    for r in results:
        rows.append({
            "kind": r.round_kind.value,
            "ret": r.card.total_return_pct,
            "sharpe": r.card.sharpe_15m,
            "dd": r.card.max_drawdown_pct,
            "lb": leaderboard_proxy(r.card),
        })
    df = pd.DataFrame(rows)
    r3 = df[df.kind == "round_3"]
    return {
        "name": name,
        "avg_ret": df.ret.mean(),
        "min_ret": df.ret.min(),
        "avg_lb": df.lb.mean(),
        "r3_avg": r3.ret.mean() if len(r3) else 0,
        "r3_min": r3.ret.min() if len(r3) else 0,
        "all_pos": (df.ret > 0).all(),
        "rd_ok": all(r.card.risk_discipline_clean for r in results),
    }


def main():
    panel = "panel"
    paths = {os.path.basename(p).replace("_15m.parquet", ""): p
             for p in glob.glob(os.path.join(panel, "*_15m.parquet"))}
    mids, spreads = load_aligned(paths)
    base = TournamentConfig()

    out = []
    out.append(eval_cfg("coc_only_22x",
                        replace(base, budget=replace(base.budget, corr_spread=0, coc=22)),
                        mids, spreads))
    out.append(eval_cfg("current_coc18_css4", base, mids, spreads))
    out.append(eval_cfg("r3_coc_off_css6",
                        replace(base, r3_coc_scale=0.0,
                                budget=replace(base.budget, corr_spread=6)),
                        mids, spreads))
    out.append(eval_cfg("r3_full_coc_no_css",
                        replace(base, r3_coc_scale=1.0, r3_spread_scale=0.0),
                        mids, spreads))
    out.append(eval_cfg("coc20_css2",
                        replace(base, budget=replace(base.budget, coc=20, corr_spread=2)),
                        mids, spreads))
    out.append(eval_cfg("r1_css_off",
                        replace(base, budget=replace(base.budget, coc=22, corr_spread=0)),
                        mids, spreads))

    r3_pairs = [
        ("EURGBP", "EURCHF"),
        ("AUDUSD", "USDCAD"),
        ("GBPUSD", "USDCAD"),
        ("EURUSD", "GBPUSD"),
        ("USDCHF", "EURCHF"),
    ]
    for pa, pb in r3_pairs:
        cfg = copy.deepcopy(base)
        old = tc.ROUND_CORR_SPREADS["round_3"]
        tc.ROUND_CORR_SPREADS["round_3"] = (pa, pb)
        out.append(eval_cfg(f"r3_{pa}/{pb}", cfg, mids, spreads))
        tc.ROUND_CORR_SPREADS["round_3"] = old

    for scale in [0.0, 0.25, 0.5, 0.75, 1.0]:
        cfg = replace(base, r3_coc_scale=scale)
        out.append(eval_cfg(f"r3_coc_scale_{scale}", cfg, mids, spreads))

    df = pd.DataFrame(out).sort_values("avg_lb", ascending=False)
    print(df.to_string(index=False, float_format=lambda x: f"{x:+.2f}"))


if __name__ == "__main__":
    main()
