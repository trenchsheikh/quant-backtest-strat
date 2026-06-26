"""Compare R1 baseline vs R1 recovery config on proxy rounds.

    py -3.10 scripts/backtest_r1_recovery.py
"""
from __future__ import annotations

import sys
from pathlib import Path
import glob
import os

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

from momq.resample import load_aligned
from momq.tournament_config import RoundKind, TournamentConfig
from momq.tournament_engine import find_competition_rounds, run_combined_round


def _cfg_baseline() -> TournamentConfig:
    """Pre-recovery behaviour: no catch-up, no z-stop, full dual-pair CSS."""
    return TournamentConfig(
        coc_catchup_enabled=False,
        spread_z_stop=0.0,
        css_recovery_scale=1.0,
        spread_z_entry_recovery=1.3,
        css_single_pair_no_coc=False,
    )


def _cfg_recovery() -> TournamentConfig:
    """R1 recovery: COC catch-up, CSS z-stop, stricter CSS when COC flat."""
    return TournamentConfig(
        coc_catchup_enabled=True,
        spread_z_stop=0.8,
        css_recovery_scale=0.5,
        spread_z_entry_recovery=1.6,
        css_single_pair_no_coc=True,
    )


def _cfg_late_start_recovery() -> TournamentConfig:
    """Recovery config — late start is handled by catch-up in-engine."""
    return _cfg_recovery()


def _run_label(cfg: TournamentConfig, name: str, mids: pd.DataFrame, spreads: pd.DataFrame) -> None:
    rounds = find_competition_rounds(mids.index, [RoundKind.R1])
    returns: list[float] = []
    sharpes: list[float] = []
    for kind, t0, t1 in rounds:
        r = run_combined_round(mids, spreads, kind, t0, t1, cfg)
        ret = (float(r.equity.iloc[-1]) / cfg.init_equity - 1.0) * 100
        sh = r.verdict.sharpe_15m if r.verdict else 0.0
        returns.append(ret)
        sharpes.append(sh)
        print(f"  {t0.date()}  ret={ret:+.2f}%  sharpe={sh:+.3f}  trades={len(r.trades)}")

    avg_ret = sum(returns) / len(returns) if returns else 0.0
    min_ret = min(returns) if returns else 0.0
    avg_sh = sum(sharpes) / len(sharpes) if sharpes else 0.0
    print(f"  >> {name}: avg={avg_ret:+.2f}%  min={min_ret:+.2f}%  avg_sharpe={avg_sh:+.3f}\n")


def main() -> None:
    panel_dir = Path(__file__).parent.parent / "panel"
    paths = {
        os.path.basename(p).replace("_15m.parquet", ""): p
        for p in glob.glob(str(panel_dir / "*_15m.parquet"))
    }
    if not paths:
        print(f"No panel parquets in {panel_dir} — run run_tournament.py first.")
        sys.exit(1)
    mids, spreads = load_aligned(paths)
    print(f"Panel: {len(mids)} bars, {mids.index[0]} -> {mids.index[-1]}\n")

    for name, cfg_fn in [
        ("baseline (no recovery)", _cfg_baseline),
        ("R1 recovery (deploy)", _cfg_recovery),
    ]:
        print(f"=== {name} ===")
        _run_label(cfg_fn(), name, mids, spreads)


if __name__ == "__main__":
    main()
