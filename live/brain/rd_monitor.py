"""Live Risk Discipline monitor — rules.md §13.

Tracks sustained breach streaks on 15-minute bar boundaries (same logic as
momq/risk_monitor.py used in backtest judge scoring).

Penalties apply after continuous persistence:
  leverage >28x for ≥30min (2 bars)  → -20
  leverage >29x for ≥15min (1 bar)   → -30
  margin   >90% for ≥30min (2 bars)  → -20
  margin   >95% for ≥15min (1 bar)   → -30
  single-instrument >90% for ≥30min  → -10
  net-directional   >95% for ≥30min  → -10
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Pre-emptive entry blocks (buffer below penalty thresholds)
LEVERAGE_ENTRY_BLOCK = 27.0   # stay below 28x RD threshold
MARGIN_ENTRY_BLOCK = 0.85     # stay below 90% RD threshold
SINGLE_INSTR_ENTRY_BLOCK = 0.85
NET_DIR_ENTRY_BLOCK = 0.92    # stay below 95% RD threshold

# Broker stop-out (rules §2): margin level 30%
STOP_OUT_MARGIN_LEVEL = 30.0
STOP_OUT_WARN_MARGIN_LEVEL = 80.0


def normalize_margin_level(
    margin_level: float | None,
    used_margin: float,
) -> float | None:
    """MT5 reports margin_level=0 when flat (margin=0) — not broker stop-out."""
    if margin_level is None or used_margin <= 0:
        return None
    return float(margin_level)


@dataclass
class RdViolation:
    kind: str
    penalty: int
    bar_i: int
    detail: str


@dataclass
class LiveRdState:
    score: float = 100.0
    streaks: dict[str, int] = field(default_factory=dict)
    violations: list[RdViolation] = field(default_factory=list)
    compliance_flags: list[str] = field(default_factory=list)
    entries_blocked: bool = False


class _StreakTracker:
    def __init__(self, threshold: float, min_bars: int, penalty: int, kind: str):
        self.threshold = threshold
        self.min_bars = min_bars
        self.penalty = penalty
        self.kind = kind
        self.streak = 0
        self.penalized = False

    def update(self, value: float, bar_i: int) -> RdViolation | None:
        if value > self.threshold:
            self.streak += 1
        else:
            self.streak = 0
            self.penalized = False

        if self.streak >= self.min_bars and not self.penalized:
            self.penalized = True
            return RdViolation(
                kind=self.kind,
                penalty=self.penalty,
                bar_i=bar_i,
                detail=f"{self.kind}>{self.threshold} for {self.min_bars * 15}min",
            )
        return None


class LiveRdMonitor:
    """Stateful RD tracker persisted across brain restarts via LiveState."""

    def __init__(self) -> None:
        self.state = LiveRdState()
        self._trackers = [
            _StreakTracker(0.90, 2, 20, "margin_usage"),
            _StreakTracker(0.95, 1, 30, "margin_usage_severe"),
            _StreakTracker(28.0, 2, 20, "leverage"),
            _StreakTracker(29.0, 1, 30, "leverage_severe"),
            _StreakTracker(0.90, 2, 10, "single_instrument"),
            _StreakTracker(0.95, 2, 10, "net_directional"),
        ]
        self._review_trackers = [
            (29.5, 1, "leverage_near_cap"),
            (0.98, 1, "margin_near_cap"),
        ]
        self._review_streaks: dict[str, int] = {k: 0 for _, _, k in self._review_trackers}

    def load(self, score: float, streaks: dict[str, int], violations: list[dict]) -> None:
        self.state.score = score
        self.state.streaks = dict(streaks)
        self.state.violations = [
            RdViolation(**v) if isinstance(v, dict) else v for v in violations
        ]

    def update_bar(
        self,
        bar_i: int,
        *,
        leverage: float,
        single_instrument: float,
        net_directional: float,
        margin_usage: float,
        margin_level: float | None = None,
    ) -> LiveRdState:
        readings = {
            "margin_usage": margin_usage,
            "margin_usage_severe": margin_usage,
            "leverage": leverage,
            "leverage_severe": leverage,
            "single_instrument": single_instrument,
            "net_directional": net_directional,
        }

        for tr in self._trackers:
            val = readings.get(tr.kind, 0.0)
            hit = tr.update(val, bar_i)
            self.state.streaks[tr.kind] = tr.streak
            if hit:
                self.state.score = max(0.0, self.state.score - hit.penalty)
                self.state.violations.append(hit)

        for threshold, min_bars, kind in self._review_trackers:
            val = leverage if "leverage" in kind else margin_usage
            if val > threshold:
                self._review_streaks[kind] += 1
            else:
                self._review_streaks[kind] = 0
            if self._review_streaks[kind] >= min_bars and kind not in self.state.compliance_flags:
                self.state.compliance_flags.append(kind)

        # Pre-emptive entry freeze (margin_level must be normalized by caller)
        self.state.entries_blocked = (
            leverage >= LEVERAGE_ENTRY_BLOCK
            or margin_usage >= MARGIN_ENTRY_BLOCK
            or single_instrument >= SINGLE_INSTR_ENTRY_BLOCK
            or net_directional >= NET_DIR_ENTRY_BLOCK
            or (margin_level is not None and margin_level <= STOP_OUT_WARN_MARGIN_LEVEL)
            or self._streak_blocks_entries(readings)
        )

        return self.state

    def compute_entries_blocked(
        self,
        *,
        leverage: float,
        single_instrument: float,
        net_directional: float,
        margin_usage: float,
        margin_level: float | None = None,
        used_margin: float = 0.0,
    ) -> bool:
        """Pre-trade gate using current readings + streak state (no streak advance)."""
        readings = self._readings(
            leverage=leverage,
            single_instrument=single_instrument,
            net_directional=net_directional,
            margin_usage=margin_usage,
        )
        ml = normalize_margin_level(margin_level, used_margin)
        return (
            leverage >= LEVERAGE_ENTRY_BLOCK
            or margin_usage >= MARGIN_ENTRY_BLOCK
            or single_instrument >= SINGLE_INSTR_ENTRY_BLOCK
            or net_directional >= NET_DIR_ENTRY_BLOCK
            or (ml is not None and ml <= STOP_OUT_WARN_MARGIN_LEVEL)
            or self._streak_blocks_entries(readings)
        )

    def reset(self) -> None:
        """Fresh RD window at round transition (Stage 3 DD/RD reset)."""
        self.state = LiveRdState()
        self._review_streaks = {k: 0 for _, _, k in self._review_trackers}
        for tr in self._trackers:
            tr.streak = 0
            tr.penalized = False

    def restore_from_state(
        self,
        score: float,
        streaks: dict[str, int],
        violations: list[dict[str, object]],
        flags: list[str],
    ) -> None:
        """Reload streak counters after brain restart."""
        self.state.score = score
        self.state.streaks = dict(streaks)
        self.state.compliance_flags = list(flags)
        self.state.violations = [
            RdViolation(
                kind=str(v.get("kind", "")),
                penalty=int(v.get("penalty", 0)),
                bar_i=int(v.get("bar_i", 0)),
                detail=str(v.get("detail", "")),
            )
            for v in violations
        ]
        for tr in self._trackers:
            tr.streak = int(streaks.get(tr.kind, 0))
            tr.penalized = tr.streak >= tr.min_bars

    def _readings(
        self,
        *,
        leverage: float,
        single_instrument: float,
        net_directional: float,
        margin_usage: float,
    ) -> dict[str, float]:
        return {
            "margin_usage": margin_usage,
            "margin_usage_severe": margin_usage,
            "leverage": leverage,
            "leverage_severe": leverage,
            "single_instrument": single_instrument,
            "net_directional": net_directional,
        }

    def sync_streaks_for_gating(
        self,
        *,
        leverage: float,
        single_instrument: float,
        net_directional: float,
        margin_usage: float,
    ) -> None:
        """Clear stale streaks when current readings are below breach thresholds."""
        readings = self._readings(
            leverage=leverage,
            single_instrument=single_instrument,
            net_directional=net_directional,
            margin_usage=margin_usage,
        )
        for tr in self._trackers:
            val = readings.get(tr.kind, 0.0)
            if val <= tr.threshold:
                tr.streak = 0
                tr.penalized = False

    def _streak_blocks_entries(
        self, readings: dict[str, float]
    ) -> bool:
        """Pre-emptive block only while a breach is still active (not stale streak)."""
        for tr in self._trackers:
            val = readings.get(tr.kind, 0.0)
            if val <= tr.threshold:
                continue
            if tr.min_bars > 1 and tr.streak >= tr.min_bars - 1 and tr.streak > 0:
                return True
            if tr.min_bars == 1 and tr.streak >= 1:
                return True
        return False
