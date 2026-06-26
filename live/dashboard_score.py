"""Live MoMQ competition score — rules.md §11–13 (read-only helper for dashboard).

Final Score = 70%×ReturnRank + 15%×DrawdownRank + 10%×SharpeRank + 5%×RiskDiscipline

True ranks need the full participant field; we estimate ranks vs a configurable
benchmark field (defaults mirror TournamentJudge synthetic medians).
"""
from __future__ import annotations

from dataclasses import dataclass

from live.brain.round_metrics import compute_round_metrics
from live.config import INIT_EQUITY
from momq.judge import _metric_ranks, _rank_to_score

# Default benchmark field (raw metrics) — other finalists / typical competitors
DEFAULT_FIELD_RETURNS_PCT = [0.0, 2.0, 5.0, 8.0, 12.0, 15.0]
DEFAULT_FIELD_MAXDD_PCT = [-8.0, -6.0, -5.0, -4.0, -3.0, -2.0]
DEFAULT_FIELD_SHARPES = [-0.05, 0.0, 0.02, 0.04, 0.06, 0.08]


@dataclass(frozen=True)
class LiveCompetitionScore:
    """Competition score breakdown for the active round window."""

    competition_return_pct: float  # vs fixed $1M (rules §12.1)
    round_return_pct: float
    round_max_dd_pct: float
    sharpe_15m: float
    risk_discipline: float
    return_rank: float
    dd_rank: float
    sharpe_rank: float
    final_score: float
    field_size: int
    trade_count: int
    sharpe_eligible: bool
    sharpe_rank_capped: bool
    metrics_scored: bool
    notes: list[str]

    def to_dict(self) -> dict:
        return {
            "competition_return_pct": round(self.competition_return_pct, 3),
            "round_return_pct": round(self.round_return_pct, 3),
            "round_max_dd_pct": round(self.round_max_dd_pct, 3),
            "sharpe_15m": round(self.sharpe_15m, 4),
            "risk_discipline": round(self.risk_discipline, 1),
            "return_rank": round(self.return_rank, 2),
            "dd_rank": round(self.dd_rank, 2),
            "sharpe_rank": round(self.sharpe_rank, 2),
            "final_score": round(self.final_score, 2),
            "field_size": self.field_size,
            "trade_count": self.trade_count,
            "sharpe_eligible": self.sharpe_eligible,
            "sharpe_rank_capped": self.sharpe_rank_capped,
            "metrics_scored": self.metrics_scored,
            "score_formula": (
                f"{self.final_score:.1f} = "
                f"0.70×{self.return_rank:.1f} + 0.15×{self.dd_rank:.1f} + "
                f"0.10×{self.sharpe_rank:.1f} + 0.05×{self.risk_discipline:.1f}"
            ),
            "notes": self.notes,
        }


def compute_live_competition_score(
    *,
    live_equity: float | None,
    equity_samples: list[float],
    round_equity_anchor: float,
    trade_count: int,
    rd_score: float,
    field_returns_pct: list[float] | None = None,
    field_maxdd_pct: list[float] | None = None,
    field_sharpes: list[float] | None = None,
) -> LiveCompetitionScore:
    """Estimate live Final Score using competition rules."""
    anchor = round_equity_anchor if round_equity_anchor > 0 else INIT_EQUITY
    equity = live_equity if live_equity is not None else (equity_samples[-1] if equity_samples else anchor)

    chart_eq = list(equity_samples)
    if live_equity is not None:
        if not chart_eq or chart_eq[-1] != live_equity:
            chart_eq.append(live_equity)

    round_m = compute_round_metrics(
        chart_eq,
        round_equity_anchor=anchor,
        trade_count=trade_count,
    )

    # Rules §12.1: return vs fixed $1M (cumulative across competition)
    competition_return_pct = 100.0 * (equity / INIT_EQUITY - 1.0)

    ret_field = list(field_returns_pct or DEFAULT_FIELD_RETURNS_PCT)
    dd_field = list(field_maxdd_pct or DEFAULT_FIELD_MAXDD_PCT)
    sh_field = list(field_sharpes or DEFAULT_FIELD_SHARPES)

    all_ret = ret_field + [competition_return_pct]
    all_dd = dd_field + [round_m.round_max_dd_pct]
    all_sh = sh_field + [round_m.sharpe_15m]

    n = len(all_ret)
    ret_rank = _rank_to_score(_metric_ranks(all_ret, True)[-1], n)
    dd_rank = _rank_to_score(_metric_ranks(all_dd, True)[-1], n)
    sh_rank = _rank_to_score(_metric_ranks(all_sh, True)[-1], n)

    if round_m.sharpe_rank_capped:
        sh_rank = min(sh_rank, 50.0)

    rd = float(max(0.0, min(100.0, rd_score)))
    final = 0.70 * ret_rank + 0.15 * dd_rank + 0.10 * sh_rank + 0.05 * rd

    notes: list[str] = []
    if not round_m.sharpe_eligible:
        notes.append(
            f"Only {trade_count} executed orders this round — platform may mark DD/Sharpe uncalculated (<31)."
        )
    if round_m.sharpe_rank_capped:
        notes.append("Fewer than 8 fifteen-minute equity samples — Sharpe rank capped at 50.")
    notes.append(
        f"Ranks estimated vs benchmark field of {len(ret_field)} profiles + you (N={n}). "
        "Official rank uses live leaderboard."
    )
    if competition_return_pct <= 0:
        notes.append("Competition return ≤ 0% — Return rank (70% weight) is weak.")

    return LiveCompetitionScore(
        competition_return_pct=competition_return_pct,
        round_return_pct=round_m.round_return_pct,
        round_max_dd_pct=round_m.round_max_dd_pct,
        sharpe_15m=round_m.sharpe_15m,
        risk_discipline=rd,
        return_rank=ret_rank,
        dd_rank=dd_rank,
        sharpe_rank=sh_rank,
        final_score=final,
        field_size=n,
        trade_count=trade_count,
        sharpe_eligible=round_m.sharpe_eligible,
        sharpe_rank_capped=round_m.sharpe_rank_capped,
        metrics_scored=round_m.metrics_scored,
        notes=notes,
    )
