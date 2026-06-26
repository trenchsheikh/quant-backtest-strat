"""Per-round competition metrics for live brain telemetry.

Stage 3 / Finals reset MaxDD and Sharpe on a fresh equity window while PnL
carries via live account equity. Platform requires >30 valid executed orders
in the stage or DD/Sharpe show as uncalculated.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from live.config import MIN_OBS_FOR_FULL_SHARPE, MIN_ROUND_TRADES

# Journal-only exits — not MT5 fills; exclude from scored trade counts in dashboard
RECONCILE_ONLY_EXITS = frozenset({"external_close"})


@dataclass(frozen=True)
class RoundMetrics:
    trade_count: int
    trades_needed: int
    sharpe_eligible: bool
    obs_count: int
    sharpe_rank_capped: bool
    round_return_pct: float
    round_max_dd_pct: float
    sharpe_15m: float

    @property
    def metrics_scored(self) -> bool:
        """Platform scores DD/Sharpe only when trade minimum is met."""
        return self.sharpe_eligible


def compute_round_metrics(
    equity_samples: list[float],
    *,
    round_equity_anchor: float,
    trade_count: int,
) -> RoundMetrics:
    anchor = round_equity_anchor if round_equity_anchor > 0 else (
        equity_samples[0] if equity_samples else 1_000_000.0
    )
    eligible = trade_count >= MIN_ROUND_TRADES
    needed = max(0, MIN_ROUND_TRADES - trade_count)

    if not equity_samples:
        return RoundMetrics(
            trade_count=trade_count,
            trades_needed=needed,
            sharpe_eligible=eligible,
            obs_count=0,
            sharpe_rank_capped=True,
            round_return_pct=0.0,
            round_max_dd_pct=0.0,
            sharpe_15m=0.0,
        )

    eq = np.asarray(equity_samples, dtype=float)
    runmax = np.maximum.accumulate(eq)
    dd = (eq / np.maximum(runmax, 1e-9) - 1.0).min() if len(eq) else 0.0
    ret = (eq[-1] / anchor - 1.0) if anchor > 0 else 0.0

    r = np.diff(eq) / np.maximum(eq[:-1], 1e-9) if len(eq) > 1 else np.array([])
    std = float(r.std(ddof=1)) if len(r) > 1 else 0.0
    sharpe = float(r.mean() / std) if std > 0 and len(r) else 0.0
    obs = len(r)

    return RoundMetrics(
        trade_count=trade_count,
        trades_needed=needed,
        sharpe_eligible=eligible,
        obs_count=obs,
        sharpe_rank_capped=obs < MIN_OBS_FOR_FULL_SHARPE,
        round_return_pct=100.0 * float(ret),
        round_max_dd_pct=100.0 * float(dd),
        sharpe_15m=sharpe,
    )


def count_executed_orders_since(
    events: list,
    since: datetime,
    *,
    exclude_reconcile: bool = False,
) -> int:
    """Count unique MT5 executions (ticket + open/close) since round t0."""
    seen: set[tuple[int, str]] = set()
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    for e in events:
        if getattr(e, "paper", False):
            continue
        ts = e.ts if hasattr(e, "ts") else e.get("ts")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < since:
            continue
        event = e.event if hasattr(e, "event") else e.get("event")
        if exclude_reconcile and str(event) == "close":
            reason = (
                e.exit_reason if hasattr(e, "exit_reason") else e.get("exit_reason")
            ) or ""
            if reason in RECONCILE_ONLY_EXITS:
                continue
        ticket = e.ticket if hasattr(e, "ticket") else e["ticket"]
        seen.add((int(ticket), str(event)))
    return len(seen)


def summarize_round_activity(events: list, since: datetime) -> dict[str, int]:
    """Opens/closes/scored executions in the current round window."""
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    opens = closes = reconcile_closes = 0
    for e in events:
        if getattr(e, "paper", False):
            continue
        ts = e.ts if hasattr(e, "ts") else e.get("ts")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < since:
            continue
        event = str(e.event if hasattr(e, "event") else e.get("event"))
        if event == "open":
            opens += 1
        elif event == "close":
            reason = (
                e.exit_reason if hasattr(e, "exit_reason") else e.get("exit_reason")
            ) or ""
            if reason in RECONCILE_ONLY_EXITS:
                reconcile_closes += 1
            else:
                closes += 1
    scored = count_executed_orders_since(events, since, exclude_reconcile=True)
    return {
        "opens": opens,
        "closes": closes,
        "reconcile_closes": reconcile_closes,
        "executed_orders": scored,
    }
