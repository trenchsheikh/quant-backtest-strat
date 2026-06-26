"""BTC ping-pong scalp — reverse-engineered from player #758 (MoMQ leader).

Rules-compliant: single instrument, ~1 BTC clips, sub-minute holds, no overnight swing.
Designed for a fast loop (1-2s), separate from the 15m brain.
"""
from __future__ import annotations

from dataclasses import dataclass


# Player #758 mature 1.0-lot medians (research/player758/strategy_params.json)
PLAYER_TP_USD_PER_LOT: float = 3.67
PLAYER_SL_USD_PER_LOT: float = 1.585
PLAYER_MEDIAN_HOLD_S: float = 4.051


@dataclass(frozen=True)
class BtcScalpParams:
    """Scalp boundaries — TP/SL scale linearly with lot size unless overridden."""
    symbol: str = "BTCUSD"
    lot_btc: float = 1.0
    tp_usd: float = 3.67
    sl_usd: float = 1.585
    max_hold_s: float = 4.5
    spread_bps: float = 0.0
    flip_on_close: bool = True
    momentum_after_sl: bool = True
    momentum_lookback_s: float = 2.0

    @classmethod
    def from_research(cls, path: str = "research/player758/strategy_params.json") -> "BtcScalpParams":
        import json
        from pathlib import Path

        p = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            tp_usd=float(p["tp_usd_median"]),
            sl_usd=abs(float(p["sl_usd_median"])),
            max_hold_s=float(p["median_hold_seconds"]) + 0.5,
        )

    @classmethod
    def for_lot(
        cls,
        lot_btc: float,
        *,
        symbol: str = "BTCUSD",
        tp_usd: float = 0.0,
        sl_usd: float = 0.0,
        tp_per_lot: float = PLAYER_TP_USD_PER_LOT,
        sl_per_lot: float = PLAYER_SL_USD_PER_LOT,
        max_hold_s: float = PLAYER_MEDIAN_HOLD_S + 0.5,
        flip_on_close: bool = True,
        momentum_after_sl: bool = True,
        momentum_lookback_s: float = 2.0,
    ) -> "BtcScalpParams":
        """Build params with player #758 medians scaled to ``lot_btc``."""
        return cls(
            symbol=symbol,
            lot_btc=lot_btc,
            tp_usd=tp_usd if tp_usd > 0 else tp_per_lot * lot_btc,
            sl_usd=sl_usd if sl_usd > 0 else sl_per_lot * lot_btc,
            max_hold_s=max_hold_s,
            flip_on_close=flip_on_close,
            momentum_after_sl=momentum_after_sl,
            momentum_lookback_s=momentum_lookback_s,
        )


@dataclass
class ScalpPosition:
    side: int  # 1 long, -1 short
    entry_price: float
    entry_ts: float  # unix seconds
    pending_flip_dir: int = 1


def unrealized_pnl_usd(
    pos: ScalpPosition,
    *,
    bid: float,
    ask: float,
    lot: float,
) -> float:
    if pos.side == 1:
        return (bid - pos.entry_price) * lot
    return (pos.entry_price - ask) * lot


def should_exit(
    pos: ScalpPosition,
    *,
    bid: float,
    ask: float,
    hi_bid: float,
    lo_bid: float,
    hi_ask: float,
    lo_ask: float,
    now_ts: float,
    params: BtcScalpParams,
) -> tuple[bool, float, str]:
    """Return (exit?, realized_usd, reason). Backtest: intrabar high/low."""
    hold = now_ts - pos.entry_ts
    lot = params.lot_btc
    if pos.side == 1:
        best = (hi_bid - pos.entry_price) * lot
        worst = (lo_bid - pos.entry_price) * lot
        mtm = (bid - pos.entry_price) * lot
    else:
        best = (pos.entry_price - lo_ask) * lot
        worst = (pos.entry_price - hi_ask) * lot
        mtm = (pos.entry_price - ask) * lot

    if worst <= -params.sl_usd:
        return True, -params.sl_usd, "sl"
    if best >= params.tp_usd:
        return True, params.tp_usd, "tp"
    if hold >= params.max_hold_s:
        return True, mtm, "time"
    return False, 0.0, ""


def should_exit_live(
    pos: ScalpPosition,
    *,
    bid: float,
    ask: float,
    now_ts: float,
    params: BtcScalpParams,
    best_mtm: float,
    worst_mtm: float,
) -> tuple[bool, float, str]:
    """Live poll exit with running tick extrema; SL/TP capped at boundaries."""
    hold = now_ts - pos.entry_ts
    mtm = unrealized_pnl_usd(pos, bid=bid, ask=ask, lot=params.lot_btc)
    best = max(best_mtm, mtm)
    worst = min(worst_mtm, mtm)

    if worst <= -params.sl_usd:
        return True, -params.sl_usd, "sl"
    if best >= params.tp_usd:
        return True, params.tp_usd, "tp"
    if hold >= params.max_hold_s:
        return True, mtm, "time"
    return False, 0.0, ""


def entry_side_live(
    *,
    pending_flip_dir: int,
    mid_now: float,
    mid_prev: float,
    last_exit_reason: str | None,
    last_exit_side: int | None,
    flip_on_close: bool,
    momentum_after_sl: bool,
) -> int:
    """Flip after TP/time; follow 2s momentum after SL (avoids fading trends)."""
    if momentum_after_sl and last_exit_reason == "sl":
        return 1 if mid_now >= mid_prev else -1
    if flip_on_close:
        return pending_flip_dir
    return 1 if mid_now >= mid_prev else -1
