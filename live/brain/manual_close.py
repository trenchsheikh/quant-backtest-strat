"""Classify manual MT5 closes and apply symbol/pair cooldowns."""
from __future__ import annotations

import logfire

from live.state.store import LivePosition, LiveState

# Loss: meaningful hit vs equity or leg MTM
MANUAL_LOSS_EQUITY_PCT = 0.005
MANUAL_LOSS_LEG_PCT = 0.02
# Profit at peak: user likely fading momentum
MANUAL_PROFIT_EQUITY_PCT = 0.003
MANUAL_PROFIT_PEAK_MTM = 0.008
MANUAL_SCRATCH_EQUITY_PCT = 0.001

CSS_PAIR_COOLDOWN_BARS = 8
MTF_PROFIT_SIDE_BLOCK_BARS = 8


def classify_manual_close(
    pos: LivePosition,
    pnl: float | None,
    equity: float,
) -> str:
    if pnl is None:
        return "external_close"

    eq = max(equity, 1.0)
    leg_notional = max(abs(pos.notional_usd), 1.0)
    leg_ret = pnl / leg_notional
    eq_ret = pnl / eq

    if eq_ret <= -MANUAL_LOSS_EQUITY_PCT or leg_ret <= -MANUAL_LOSS_LEG_PCT:
        return "manual_close_loss"
    if (
        eq_ret >= MANUAL_PROFIT_EQUITY_PCT
        or (pos.peak_mtm_pct >= MANUAL_PROFIT_PEAK_MTM and pnl > 0)
    ):
        return "manual_close_profit"
    if abs(eq_ret) < MANUAL_SCRATCH_EQUITY_PCT:
        return "manual_close_scratch"
    return "manual_close"


def apply_manual_close_policy(
    state: LiveState,
    pos: LivePosition,
    pnl: float | None,
    equity: float,
    bar_i: int,
    *,
    mtf_stop_cooldown_bars: int = 12,
) -> str:
    """Update cooldown state and return journal exit_reason."""
    if pos.sleeve == "unknown":
        return "external_close"

    reason = classify_manual_close(pos, pnl, equity)
    if reason in ("external_close", "manual_close_scratch"):
        return reason

    if reason == "manual_close_loss":
        if pos.sleeve == "mtf":
            state.mtf_cooldown[pos.symbol] = bar_i + mtf_stop_cooldown_bars
        elif pos.sleeve == "css" and pos.pair_id:
            state.css_pair_cooldown[pos.pair_id] = bar_i + CSS_PAIR_COOLDOWN_BARS
        logfire.warn(
            "brain.manual_close_loss",
            symbol=pos.symbol,
            sleeve=pos.sleeve,
            pnl=pnl,
            pair_id=pos.pair_id or None,
            cooldown_until=state.mtf_cooldown.get(pos.symbol)
            or state.css_pair_cooldown.get(pos.pair_id or ""),
        )
    elif reason == "manual_close_profit":
        block_key = f"{pos.symbol}:{pos.side}"
        state.mtf_side_block[block_key] = bar_i + MTF_PROFIT_SIDE_BLOCK_BARS
        if pos.sleeve == "css" and pos.pair_id:
            state.css_pair_cooldown[pos.pair_id] = bar_i + CSS_PAIR_COOLDOWN_BARS
        logfire.info(
            "brain.manual_close_profit",
            symbol=pos.symbol,
            side=pos.side,
            sleeve=pos.sleeve,
            pnl=pnl,
            peak_mtm_pct=round(pos.peak_mtm_pct, 4),
            block_until=state.mtf_side_block[block_key],
        )
    else:
        logfire.info(
            "brain.manual_close",
            symbol=pos.symbol,
            sleeve=pos.sleeve,
            pnl=pnl,
            reason=reason,
        )

    return reason
