"""Competition-exact scoring, verified against the official rules (Sections 11-17).

Final Score = 0.70*ReturnRank + 0.15*DrawdownRank + 0.10*SharpeRank + 0.05*RiskDiscipline
All four are PERCENTILE ranks vs the field, so this module reports the raw
metrics we control; the rank conversion needs the field we can't see.

Rule-exact definitions implemented here:
  * Return  (12.1): (Equity_final - Equity_initial) / Equity_initial, with
    Equity_initial FIXED at 1,000,000 (NOT the first sample). Equity carries
    across rounds, so each round's Return is cumulative-from-1M.
  * Sharpe  (12.5/17): mean/std of 15-min equity returns, non-annualised.
    Std=0 -> Sharpe=0. <8 valid observations -> Sharpe RANK capped at 50.
  * MaxDD   (12.3): max over t of (Peak-Equity)/Peak, on the 15-min series.
  * RiskDiscipline (13): starts 100; concentration/leverage/margin breaches
    deduct. We verify we never trip them (stay at 100).
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import pandas as pd

_BARS_PER_YEAR = 96 * 5 * 52          # intuition-only annualisation
MIN_TRADES_BEST_SHARPE = 30           # rule 17 eligibility
MIN_OBS_FOR_FULL_SHARPE = 8           # rule 12.5 sparse-data cap


@dataclass
class Scorecard:
    final_equity: float
    total_return_pct: float            # 70% of score (the headline)
    max_drawdown_pct: float            # 15%
    sharpe_15m: float                  # 10%
    n_trades: int
    win_rate_pct: float
    n_samples: int
    avg_gross_lev: float
    max_gross_lev: float
    max_single_instrument_pct: float   # rule 13.3 (>90% for 30m = -10)
    max_net_directional_pct: float     # rule 13.3 (>95% for 30m = -10)
    sharpe_annualised: float
    # eligibility / compliance flags
    sharpe_rank_capped: bool           # <8 obs -> rank capped at 50
    best_sharpe_trade_ok: bool         # >=30 trades
    risk_discipline_clean: bool        # no concentration/leverage breach seen

    def __str__(self) -> str:
        rd = "CLEAN (100)" if self.risk_discipline_clean else "*** BREACH RISK ***"
        bs = "eligible" if self.best_sharpe_trade_ok else f"NOT eligible (<{MIN_TRADES_BEST_SHARPE} trades)"
        cap = "  [<8 obs: Sharpe rank capped 50]" if self.sharpe_rank_capped else ""
        return (
            "== MoMQ Scorecard (raw metrics; ranks need the field) =========\n"
            f"  RETURN   (70% wt) : {self.total_return_pct:+.3f}%   <<< dominant\n"
            f"  MaxDD    (15% wt) : {self.max_drawdown_pct:.3f}%\n"
            f"  Sharpe15 (10% wt) : {self.sharpe_15m:+.4f}{cap}\n"
            f"  RiskDisc ( 5% wt) : {rd}\n"
            "  ---------------------------------------------------------------\n"
            f"  Final equity      : {self.final_equity:,.0f}\n"
            f"  Trades closed     : {self.n_trades}   (Best-Sharpe: {bs})\n"
            f"  Win rate          : {self.win_rate_pct:.1f}%\n"
            f"  Equity samples    : {self.n_samples}\n"
            f"  Gross leverage    : avg {self.avg_gross_lev:.2f}x / max {self.max_gross_lev:.2f}x  (cap 30x; penalty >28x)\n"
            f"  Max single-instr  : {self.max_single_instrument_pct:.1f}%  (penalty >90%/30m)\n"
            f"  Max net-direction : {self.max_net_directional_pct:.1f}%  (penalty >95%/30m)\n"
            f"  Sharpe annualised : {self.sharpe_annualised:+.2f}  (intuition only, NOT scored)\n"
            "==============================================================="
        )


def compute_scorecard(equity: pd.Series, n_trades: int, n_wins: int,
                      init_equity: float = 1_000_000.0,
                      gross_lev: pd.Series | None = None,
                      max_single_instrument: float = 0.0,
                      max_net_directional: float = 0.0,
                      ddof: int = 1) -> Scorecard:
    equity = equity.dropna()
    r = equity.pct_change().dropna()
    std = r.std(ddof=ddof)
    sharpe = float(r.mean() / std) if std > 0 else 0.0          # rule: Std=0 -> 0

    runmax = equity.cummax()
    maxdd = float((equity / runmax - 1.0).min()) if len(equity) else 0.0

    # rule 12.1: fixed 1M denominator, NOT first sample
    total_ret = float(equity.iloc[-1] / init_equity - 1.0) if len(equity) else 0.0
    win_rate = (100.0 * n_wins / n_trades) if n_trades else 0.0

    return Scorecard(
        final_equity=float(equity.iloc[-1]) if len(equity) else init_equity,
        total_return_pct=100.0 * total_ret,
        max_drawdown_pct=100.0 * maxdd,
        sharpe_15m=sharpe,
        n_trades=n_trades,
        win_rate_pct=win_rate,
        n_samples=len(equity),
        avg_gross_lev=float(gross_lev.mean()) if gross_lev is not None and len(gross_lev) else 0.0,
        max_gross_lev=float(gross_lev.max()) if gross_lev is not None and len(gross_lev) else 0.0,
        max_single_instrument_pct=100.0 * max_single_instrument,
        max_net_directional_pct=100.0 * max_net_directional,
        sharpe_annualised=sharpe * np.sqrt(_BARS_PER_YEAR),
        sharpe_rank_capped=len(r) < MIN_OBS_FOR_FULL_SHARPE,
        best_sharpe_trade_ok=n_trades >= MIN_TRADES_BEST_SHARPE,
        risk_discipline_clean=(max_single_instrument < 0.90 and max_net_directional < 0.95
                               and (gross_lev is None or (len(gross_lev) and gross_lev.max() < 28.0))),
    )


def leaderboard_proxy(card: Scorecard) -> float:
    """Proxy for MoMQ Final Score using raw metrics (ranks need the field).

    Final Score = 70% ReturnRank + 15% DrawdownRank + 10% SharpeRank + 5% RD
    We approximate ranks with raw values scaled to ~0-100 range.
    Higher is better. Use to compare strategy variants on the same data.
    """
    rd = 100.0 if card.risk_discipline_clean else 70.0
    # max_drawdown_pct is negative e.g. -5.0; map to 0-100 (0% DD -> 100)
    dd_score = max(0.0, 100.0 + card.max_drawdown_pct)
    sharpe_score = max(0.0, min(100.0, 50.0 + card.sharpe_15m * 500.0))
    return (0.70 * card.total_return_pct
            + 0.15 * dd_score
            + 0.10 * sharpe_score
            + 0.05 * rd)
