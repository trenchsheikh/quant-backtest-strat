"""Risk Discipline monitor — rules.md §13 (sustained breach windows).

All violation thresholds require *continuous* persistence:
  * Leverage >28x  for ≥30 min  → -20 pts  (2 × 15m bars)
  * Leverage >29x  for ≥15 min  → -30 pts  (1 bar)
  * Single-instr >90% for ≥30 min → -10 pts
  * Net-direction >95% for ≥30 min → -10 pts
  * Margin >90% for ≥30 min → -20 pts
"""
from __future__ import annotations
from dataclasses import dataclass, field
import pandas as pd

BARS_15M = 1
BARS_30M = 2


@dataclass
class RiskViolation:
    kind: str
    penalty: int
    start_ts: pd.Timestamp
    end_ts: pd.Timestamp
    detail: str


@dataclass
class RiskDisciplineState:
    score: float = 100.0
    violations: list[RiskViolation] = field(default_factory=list)
    compliance_flags: list[str] = field(default_factory=list)


class _BreachTracker:
    def __init__(self, threshold: float, min_bars: int, penalty: int, kind: str):
        self.threshold = threshold
        self.min_bars = min_bars
        self.penalty = penalty
        self.kind = kind
        self.streak = 0
        self.breach_start: pd.Timestamp | None = None
        self.penalized = False

    def update(self, value: float, ts: pd.Timestamp) -> RiskViolation | None:
        if value > self.threshold:
            self.streak += 1
            if self.breach_start is None:
                self.breach_start = ts
        else:
            self.streak = 0
            self.breach_start = None
            self.penalized = False

        if self.streak >= self.min_bars and not self.penalized:
            self.penalized = True
            return RiskViolation(
                kind=self.kind,
                penalty=self.penalty,
                start_ts=self.breach_start or ts,
                end_ts=ts,
                detail=f"{self.kind} > {self.threshold:.0%} for {self.min_bars * 15}min",
            )
        return None


class RiskDisciplineMonitor:
    """Per-round RD score; resets at round inception (rules §13)."""

    def __init__(self):
        self.state = RiskDisciplineState()
        self._trackers = [
            _BreachTracker(0.90, BARS_30M, 20, "margin_usage"),
            _BreachTracker(0.95, BARS_15M, 30, "margin_usage_severe"),
            _BreachTracker(28.0, BARS_30M, 20, "leverage"),
            _BreachTracker(29.0, BARS_15M, 30, "leverage_severe"),
            _BreachTracker(0.90, BARS_30M, 10, "single_instrument"),
            _BreachTracker(0.95, BARS_30M, 10, "net_directional"),
        ]
        self._review_trackers = [
            (29.5, BARS_15M, "leverage_near_cap"),
            (0.98, BARS_15M, "margin_near_cap"),
        ]
        self._review_streaks: dict[str, tuple[int, pd.Timestamp | None]] = {
            k: (0, None) for _, _, k in self._review_trackers
        }

    def reset(self) -> None:
        self.__init__()

    def update_bar(self, ts: pd.Timestamp, *,
                   leverage: float,
                   single_instrument: float,
                   net_directional: float,
                   margin_usage: float = 0.0) -> None:
        readings = {
            "margin_usage": margin_usage,
            "margin_usage_severe": margin_usage,
            "leverage": leverage,
            "leverage_severe": leverage,
            "single_instrument": single_instrument,
            "net_directional": net_directional,
        }
        for tr in self._trackers:
            v = readings.get(tr.kind, 0.0)
            hit = tr.update(v, ts)
            if hit:
                self.state.score = max(0.0, self.state.score - hit.penalty)
                self.state.violations.append(hit)

        for threshold, min_bars, kind in self._review_trackers:
            streak, start = self._review_streaks[kind]
            val = leverage if "leverage" in kind else margin_usage
            if val > threshold:
                streak += 1
                if start is None:
                    start = ts
            else:
                streak, start = 0, None
            self._review_streaks[kind] = (streak, start)
            if streak >= min_bars and kind not in self.state.compliance_flags:
                self.state.compliance_flags.append(kind)

    @property
    def score(self) -> float:
        return self.state.score

    @property
    def clean(self) -> bool:
        return self.state.score >= 100.0 and not self.state.violations
