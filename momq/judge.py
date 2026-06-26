"""Tournament Judge — scores rounds exactly as rules.md §11–13 specify.

    Final Score = 70%×ReturnRank + 15%×DrawdownRank + 10%×SharpeRank + 5%×RiskDiscipline

The judge does not trade. It only measures. Strategy proposes; the judge disposes.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

from .metrics import Scorecard, compute_scorecard, MIN_OBS_FOR_FULL_SHARPE
from .risk_monitor import RiskDisciplineMonitor, RiskViolation

INIT_EQUITY = 1_000_000.0


@dataclass
class RiskTelemetry:
    """Per-bar readings fed to the RD monitor."""
    timestamps: list[pd.Timestamp] = field(default_factory=list)
    leverage: list[float] = field(default_factory=list)
    single_instrument: list[float] = field(default_factory=list)
    net_directional: list[float] = field(default_factory=list)
    margin_usage: list[float] = field(default_factory=list)

    def append(self, ts: pd.Timestamp, lev: float, single: float, net: float,
               margin: float = 0.0) -> None:
        self.timestamps.append(ts)
        self.leverage.append(lev)
        self.single_instrument.append(single)
        self.net_directional.append(net)
        self.margin_usage.append(margin)


@dataclass
class JudgeVerdict:
    """Complete ruling for one competition round."""
    return_pct: float
    max_dd_pct: float
    sharpe_15m: float
    risk_discipline: float
    return_rank: float
    dd_rank: float
    sharpe_rank: float
    final_score: float
    n_participants: int
    violations: list[RiskViolation]
    compliance_flags: list[str]
    sharpe_capped: bool
    card: Scorecard
    notes: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        cap = "  [Sharpe rank capped @50: <8 obs]" if self.sharpe_capped else ""
        vio = ""
        if self.violations:
            vio = "\n  RD violations:\n" + "\n".join(
                f"    -{v.penalty}  {v.kind}: {v.detail}" for v in self.violations)
        flags = ""
        if self.compliance_flags:
            flags = "\n  Compliance review flags: " + ", ".join(self.compliance_flags)
        return (
            "== TOURNAMENT JUDGE RULING (rules.md S11) =====================\n"
            f"  FINAL SCORE      : {self.final_score:6.2f} / 100\n"
            "  ---------------------------------------------------------------\n"
            f"  Return Rank (70%): {self.return_rank:6.2f}  <- raw {self.return_pct:+.3f}%\n"
            f"  Drawdown Rank(15%): {self.dd_rank:6.2f}  <- MaxDD {self.max_dd_pct:.3f}%\n"
            f"  Sharpe Rank (10%): {self.sharpe_rank:6.2f}  <- Sharpe {self.sharpe_15m:+.4f}{cap}\n"
            f"  Risk Discip ( 5%): {self.risk_discipline:6.2f}\n"
            f"  Field size       : N={self.n_participants}\n"
            "==============================================================="
            f"{vio}{flags}"
        )


def _rank_to_score(rank_1based: int, n: int) -> float:
    """rules §12.2: 100 × (N − Rank) / (N − 1). Rank 1 = best."""
    if n <= 1:
        return 100.0
    return 100.0 * (n - rank_1based) / (n - 1)


def _metric_ranks(values: list[float], higher_is_better: bool) -> list[int]:
    """1-based ranks; rank 1 = best."""
    n = len(values)
    order = np.argsort(values)
    if higher_is_better:
        order = order[::-1]
    ranks = np.empty(n, dtype=int)
    ranks[order] = np.arange(1, n + 1)
    return ranks.tolist()


class TournamentJudge:
    """Scores one participant vs a field using competition formulas."""

    WEIGHTS = (0.70, 0.15, 0.10, 0.05)

    def __init__(self, init_equity: float = INIT_EQUITY):
        self.init_equity = init_equity

    def compute_rd(self, telemetry: RiskTelemetry) -> RiskDisciplineMonitor:
        mon = RiskDisciplineMonitor()
        for i, ts in enumerate(telemetry.timestamps):
            mon.update_bar(
                ts,
                leverage=telemetry.leverage[i],
                single_instrument=telemetry.single_instrument[i],
                net_directional=telemetry.net_directional[i],
                margin_usage=telemetry.margin_usage[i],
            )
        return mon

    def score_round(self,
                    equity: pd.Series,
                    n_trades: int,
                    n_wins: int,
                    telemetry: RiskTelemetry,
                    field_returns: list[float] | None = None,
                    field_maxdds: list[float] | None = None,
                    field_sharpes: list[float] | None = None,
                    ) -> JudgeVerdict:
        """Score one round. Field lists are *other* participants' raw metrics."""
        eq = equity.dropna()
        r = eq.pct_change().dropna()
        std = r.std(ddof=1)
        sharpe = float(r.mean() / std) if std > 0 else 0.0
        runmax = eq.cummax()
        maxdd = float((eq / runmax - 1.0).min()) if len(eq) else 0.0
        ret_pct = 100.0 * (float(eq.iloc[-1]) / self.init_equity - 1.0) if len(eq) else 0.0

        rd_mon = self.compute_rd(telemetry)
        rd_score = rd_mon.score

        # Build synthetic field if not provided (assume median competitor)
        if field_returns is None:
            field_returns = [0.0, 0.5, 1.0, 1.5, 2.0]
        if field_maxdds is None:
            field_maxdds = [-6.0, -4.0, -3.0, -2.0, -1.0]
        if field_sharpes is None:
            field_sharpes = [-0.05, 0.0, 0.03, 0.06, 0.10]

        all_ret = field_returns + [ret_pct]
        all_dd = field_maxdds + [maxdd * 100.0]  # maxdd is negative fraction
        all_sh = field_sharpes + [sharpe]

        n = len(all_ret)
        ret_rank = _rank_to_score(_metric_ranks(all_ret, True)[-1], n)
        dd_rank = _rank_to_score(_metric_ranks(all_dd, True)[-1], n)  # less negative = higher
        sh_rank = _rank_to_score(_metric_ranks(all_sh, True)[-1], n)

        sharpe_capped = len(r) < MIN_OBS_FOR_FULL_SHARPE
        if sharpe_capped:
            sh_rank = min(sh_rank, 50.0)

        w_ret, w_dd, w_sh, w_rd = self.WEIGHTS
        final = w_ret * ret_rank + w_dd * dd_rank + w_sh * sh_rank + w_rd * rd_score

        ms = max(telemetry.single_instrument) if telemetry.single_instrument else 0.0
        nd = max(telemetry.net_directional) if telemetry.net_directional else 0.0
        gl = pd.Series(telemetry.leverage) if telemetry.leverage else None

        card = compute_scorecard(
            eq, n_trades, n_wins, self.init_equity, gl, ms, nd,
        )
        card = Scorecard(
            **{**card.__dict__,
               "risk_discipline_clean": rd_score >= 100.0 and not rd_mon.state.violations},
        )

        notes = []
        if ret_pct <= 0:
            notes.append("Negative return — Return Rank will suffer (70% weight).")
        if maxdd * 100 < -5:
            notes.append(f"Deep drawdown ({maxdd*100:.1f}%) — Drawdown Rank penalized.")

        return JudgeVerdict(
            return_pct=ret_pct,
            max_dd_pct=maxdd * 100.0,
            sharpe_15m=sharpe,
            risk_discipline=rd_score,
            return_rank=ret_rank,
            dd_rank=dd_rank,
            sharpe_rank=sh_rank,
            final_score=final,
            n_participants=n,
            violations=rd_mon.state.violations,
            compliance_flags=rd_mon.state.compliance_flags,
            sharpe_capped=sharpe_capped,
            card=card,
            notes=notes,
        )
