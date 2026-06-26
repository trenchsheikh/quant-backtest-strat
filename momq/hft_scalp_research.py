"""HFT-inspired scalp signals — research only (not wired to live loop).

Insights applied:
- Cartea & Jaimungal (SSRN 2010417): short-horizon momentum, adverse-selection
  avoidance, inventory turnover, don't fade informed flow after losses.
- Mahdavi-Damghani (Oxford-Man): order-flow / microprice proxies from 1s bars
  (close location in range, volume imbalance, multi-scale mid dynamics).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EntryMode(str, Enum):
    FLIP = "flip"  # baseline player-758
    MOMENTUM = "momentum"  # always trade with short momentum
    MOMENTUM_MULTI = "mom_multi"  # 2s+5s agree
    MICROPRICE = "microprice"  # close-in-range imbalance
    MOM_MICRO = "mom_micro"  # momentum + microprice agree
    CARTEA = "cartea"  # momentum + no adverse selection buffer breach
    TREND = "trend"  # multi-scale momentum aligned + magnitude gate (wide TP/SL, clears spread)


@dataclass(frozen=True)
class HftScalpConfig:
    lot_btc: float = 1.0
    tp_usd: float = 3.67
    sl_usd: float = 1.585
    max_hold_s: float = 4.5
    spread_bps: float = 0.0
    entry_mode: EntryMode = EntryMode.MOM_MICRO
    mom_fast_s: int = 2
    mom_slow_s: int = 5
    micro_thresh: float = 0.55  # close-in-range above/below mid
    min_range_usd: float = 0.5  # skip flat 1s bars
    min_vol_z: float = -0.5  # volume vs 60s rolling (z-score floor)
    adverse_buffer_usd: float = 1.0  # skip if last bar moved against us
    momentum_after_sl: bool = True
    flip_on_close: bool = True
    pause_after_sl_streak: int = 0
    use_trailing_after_pct: float = 0.0  # 0=off; e.g. 0.5 locks half of peak mtm
    initial_equity: float = 1_000_000.0
    # TREND mode: require |mom_slow| >= trend_min_usd so we only chase real moves
    # big enough to clear the ~$14 BTC round-trip spread. cir_confirm adds a
    # microprice agreement filter. trend_min_usd is in price-USD (per 1 BTC).
    trend_min_usd: float = 12.0
    trend_cir_confirm: bool = False


def close_in_range(close: float, high: float, low: float) -> float:
    """Microprice proxy: 0=at low, 1=at high (Mahdavi order-book imbalance idea)."""
    rng = high - low
    if rng <= 0:
        return 0.5
    return (close - low) / rng


def entry_direction(
    *,
    mode: EntryMode,
    pending_flip: int,
    mom_fast: float,
    mom_slow: float,
    cir: float,
    last_exit_reason: str | None,
    last_exit_side: int | None,
    adverse_move_usd: float,
    cfg: HftScalpConfig,
) -> int | None:
    """Return +1/-1 to enter, or None to skip bar (stay flat until next signal)."""
    mom_dir = 1 if mom_fast >= 0 else -1
    slow_dir = 1 if mom_slow >= 0 else -1
    micro_long = cir >= cfg.micro_thresh
    micro_short = cir <= (1.0 - cfg.micro_thresh)

    if cfg.entry_mode == EntryMode.FLIP:
        if cfg.momentum_after_sl and last_exit_reason == "sl":
            return 1 if mom_fast >= 0 else -1
        return pending_flip

    if cfg.entry_mode == EntryMode.MOMENTUM:
        return mom_dir

    if cfg.entry_mode == EntryMode.MOMENTUM_MULTI:
        if mom_dir != slow_dir:
            return None
        return mom_dir

    if cfg.entry_mode == EntryMode.MICROPRICE:
        if micro_long:
            return 1
        if micro_short:
            return -1
        return None

    if cfg.entry_mode == EntryMode.MOM_MICRO:
        if mom_dir == 1 and micro_long and slow_dir == 1:
            return 1
        if mom_dir == -1 and micro_short and slow_dir == -1:
            return -1
        return None

    if cfg.entry_mode == EntryMode.TREND:
        # Only enter WITH a multi-scale-aligned move large enough to clear spread.
        # mom_fast is interpreted as price-USD change over mom_fast_s; mom_slow over
        # mom_slow_s. Require both same sign and the slow leg to exceed trend_min_usd.
        if mom_dir != slow_dir:
            return None
        if abs(mom_slow) < cfg.trend_min_usd:
            return None
        if cfg.trend_cir_confirm:
            if slow_dir == 1 and not micro_long:
                return None
            if slow_dir == -1 and not micro_short:
                return None
        return slow_dir

    if cfg.entry_mode == EntryMode.CARTEA:
        # Trade with momentum; skip if last bar moved against intended side
        if mom_dir != slow_dir:
            return None
        side = mom_dir
        if side == 1 and adverse_move_usd < -cfg.adverse_buffer_usd:
            return None
        if side == -1 and adverse_move_usd > cfg.adverse_buffer_usd:
            return None
        if cfg.momentum_after_sl and last_exit_reason == "sl":
            return side
        if cfg.flip_on_close and last_exit_reason in (None, "tp", "time"):
            if pending_flip == side:
                return side
            return None
        return side

    return mom_dir
