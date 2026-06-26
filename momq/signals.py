"""Pure signal layer — bar-by-bar trade intents (algorithm core).

Every signal is a function:  (state, bar) → list[TradeIntent]
The engine is a thin executor; the beauty lives here.

Three alpha sources, each targeting a different market regime:

  COC — Contrarian Open Carry (forex + gold)
        At round open, rank the 12h pre-round returns across the forex/metals
        universe. Long the 2 weakest, short the 1 strongest. Pure mean-reversion
        bet: whatever sold off hardest pre-round snaps back.
        Sized with inverse-vol parity: each leg contributes equal expected P&L.

  MTF — Momentum Trend Follow (crypto: BTC, ETH, SOL, XRP)
        Crypto TRENDS within a round whereas forex REVERTS. EMA(4) vs EMA(16)
        crossover on 15m bars identifies a trend after it has established (bar≥8).
        One position at a time, held ≤24 bars or until reversal/stop-loss.
        This is the engine's primary alpha for R3 (where COC stays flat).

  CSS — Correlation Spread (dollar-neutral pair)
        Log-spread z-score mean-reversion between structurally correlated pairs.
        Runs from bar 4 onward; one spread open at a time.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import numpy as np
import pandas as pd

from .tournament_config import (
    RoundKind, TournamentConfig, PRIMARY_SYMBOLS, ROUND_CORR_SPREADS,
    METAL_SYMBOLS,  # noqa: F401
)


class SignalKind(Enum):
    COC = "coc"     # contrarian open carry @ round open
    MTF = "mtf"     # momentum trend follow (crypto EMA crossover)
    CSS = "css"     # correlation spread z-reversion


@dataclass(frozen=True)
class TradeIntent:
    kind: SignalKind
    symbols: tuple[str, ...]
    sides: tuple[int, ...]          # +1 long, -1 short per symbol
    notional_per_leg: float
    pair_id: str = ""
    priority: float = 1.0           # higher = deploy first
    reason: str = ""


@dataclass
class BarContext:
    ts: pd.Timestamp
    bar_i: int
    round_kind: RoundKind
    t0: pd.Timestamp
    t1: pd.Timestamp
    mids: pd.DataFrame
    spreads: pd.DataFrame
    cfg: TournamentConfig
    coc_budget: float
    spread_budget: float
    # CSS: list of (log-spread series, (sym_a, sym_b)) for each eligible pair this round.
    # Each entry is an independent mean-reversion signal; all can be open simultaneously.
    spread_states: list[tuple[pd.Series, tuple[str, str]]] = field(default_factory=list)
    open_css_pairs: set[str] = field(default_factory=set)  # pair_ids currently open
    open_coc_symbols: set[str] = field(default_factory=set)  # symbols with open COC legs
    open_symbols: set[str] = field(default_factory=set)  # all symbols with any open leg
    coc_deployed: bool = False   # True once any COC leg opened this round (incl. catch-up)
    coc_ever_this_round: bool = False  # persisted: COC was deployed at least once this round
    primary: list[str] = field(default_factory=list)
    # MTF context
    mtf_budget: float = 0.0
    crypto_symbols: list[str] = field(default_factory=list)
    open_mtf_symbols: set[str] = field(default_factory=set)   # currently-open MTF symbol names
    mtf_stop_cooldown_syms: set[str] = field(default_factory=set)  # syms in post-stop cooldown
    css_pair_cooldown: set[str] = field(default_factory=set)  # manual / stop cooldown on spreads
    blocked_mtf_sides: set[str] = field(default_factory=set)  # "SYMBOL:side" after manual take-profit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _zscore(series: pd.Series, ts: pd.Timestamp, window: int) -> float:
    # dropna before tailing: weekend gaps from 24/7 crypto data must not
    # shrink the effective forex lookback window.
    sub = series.loc[:ts].dropna().tail(window)
    if len(sub) < window // 2:
        return np.nan
    mu, sd = sub.mean(), sub.std(ddof=1)
    if sd <= 0 or not np.isfinite(sd):
        return np.nan
    return float((sub.iloc[-1] - mu) / sd)


def _rank_pre(mids: pd.DataFrame, t0: pd.Timestamp, symbols: list[str],
              window: int) -> list[tuple[str, float]]:
    """Rank symbols by return over the `window` valid bars immediately before t0.

    Uses per-symbol dropna so that weekend gaps (introduced when a 24/7 crypto
    panel is aligned with forex data) don't shift the lookback window onto NaN bars.
    """
    scores = []
    for sym in symbols:
        if sym not in mids.columns:
            continue
        sym_pre = mids[sym].loc[:t0].dropna()
        if len(sym_pre) < window:
            continue
        p0, p1 = float(sym_pre.iloc[-window]), float(sym_pre.iloc[-1])
        if p0 > 0:
            scores.append((sym, p1 / p0 - 1.0))
    scores.sort(key=lambda x: x[1])
    return scores


def _sym_vol(mids: pd.DataFrame, sym: str, t0: pd.Timestamp, window: int) -> float:
    """Realized per-bar return std for `sym` using valid data strictly before t0.

    Drops NaN before tailing so weekend crypto rows don't pad the window.
    """
    pre = mids[sym].loc[:t0].dropna().tail(window + 1)
    if len(pre) < max(4, window // 4):
        return np.nan
    ret = pre.pct_change().dropna()
    v = float(ret.std(ddof=1))
    return v if v > 1e-10 else np.nan


def _ema(series: pd.Series, span: int) -> float:
    """EWM mean of the series, returning just the last value."""
    if len(series) < 2:
        return float(series.iloc[-1]) if len(series) else np.nan
    return float(series.ewm(span=span, adjust=False).mean().iloc[-1])


# ---------------------------------------------------------------------------
# Signal 1: COC — Contrarian Open Carry (vol-parity sized)
# ---------------------------------------------------------------------------

def signal_coc_open(ctx: BarContext) -> list[TradeIntent]:
    """Round open only (bar_i == 0).

    Ranks the pre-round 12h return of the forex/metals universe, goes long
    the 2 weakest and short the 1 strongest.  Each leg is sized by inverse
    realized-vol so every leg contributes equal expected P&L regardless of
    asset class — a single $XAU leg won't drown out three forex legs.

    Returns one TradeIntent per leg (not a single multi-symbol intent) so
    the engine can independently manage each position.
    """
    if ctx.bar_i != 0 or ctx.coc_budget <= 0:
        return []
    return _build_coc_intents(ctx, budget_scale=1.0, label="open")


def signal_coc_catchup(ctx: BarContext) -> list[TradeIntent]:
    """Deploy COC mid-round when bar 0 was missed (late system start).

    Fires once per round when no COC has been deployed yet. Budget scales
    linearly with bars remaining so late entry does not oversize.
    """
    cfg = ctx.cfg
    if not cfg.coc_catchup_enabled or ctx.coc_budget <= 0:
        return []
    if ctx.bar_i <= 0 or ctx.bar_i > cfg.coc_catchup_max_bar:
        return []
    if ctx.coc_deployed:
        return []

    remaining = max(cfg.round_bars - ctx.bar_i, 1)
    scale = remaining / cfg.round_bars
    return _build_coc_intents(ctx, budget_scale=scale, label=f"catchup_b{ctx.bar_i}")


def _build_coc_intents(
    ctx: BarContext,
    budget_scale: float,
    label: str,
) -> list[TradeIntent]:
    """Shared COC rank + vol-parity sizing for open and catch-up entry."""
    ranked = _rank_pre(ctx.mids, ctx.t0, ctx.primary, ctx.cfg.coc_pre_window)
    need = ctx.cfg.coc_n_long + ctx.cfg.coc_n_short
    if len(ranked) < need:
        return []

    longs = [(s, 1) for s, _ in ranked[: ctx.cfg.coc_n_long]]
    shorts = [(s, -1) for s, _ in ranked[-ctx.cfg.coc_n_short :]]
    legs = longs + shorts

    ret_map = {s: r for s, r in ranked}
    rank_map = {s: i + 1 for i, (s, _) in enumerate(ranked)}
    n_candidates = len(ranked)

    vols: dict[str, float] = {}
    for sym, _ in legs:
        vols[sym] = _sym_vol(ctx.mids, sym, ctx.t0, ctx.cfg.coc_pre_window)

    scaled_budget = ctx.coc_budget * budget_scale
    total_notional = scaled_budget * ctx.cfg.init_equity
    tag = f"[{label} scale={budget_scale:.2f}]"

    intents: list[TradeIntent] = []
    all_valid = all(np.isfinite(v) and v > 0 for v in vols.values())

    if not all_valid:
        per_leg = total_notional / len(legs)
        for i, (sym, side) in enumerate(legs):
            if sym in ctx.open_symbols:
                continue
            intents.append(TradeIntent(
                SignalKind.COC, (sym,), (side,), per_leg,
                priority=10.5 if label != "open" else 10.0 + 0.01 * i,
                reason=(
                    f"COC {tag} pre_ret={ret_map.get(sym, 0):+.3%} "
                    f"rank={rank_map.get(sym, 0)}/{n_candidates} "
                    f"({'weakest' if side > 0 else 'strongest'}) "
                    f"notional_x={per_leg/ctx.cfg.init_equity:.1f}x (equal-wt)"
                ),
            ))
        return intents

    inv_v = {sym: 1.0 / vols[sym] for sym, _ in legs}
    total_w = sum(inv_v.values())

    for i, (sym, side) in enumerate(legs):
        if sym in ctx.open_symbols:
            continue
        notional = total_notional * inv_v[sym] / total_w
        leg_label = "weakest" if side > 0 else "strongest"
        intents.append(TradeIntent(
            SignalKind.COC, (sym,), (side,), notional,
            priority=10.5 if label != "open" else 10.0 + 0.01 * i,
            reason=(
                f"COC {tag} pre_ret={ret_map.get(sym, 0):+.3%} "
                f"rank={rank_map.get(sym, 0)}/{n_candidates} ({leg_label}) "
                f"vol={vols[sym]:.5f} wt={inv_v[sym]/total_w:.2f} "
                f"notional_x={notional/ctx.cfg.init_equity:.1f}x"
            ),
        ))
    return intents


# ---------------------------------------------------------------------------
# Signal 2: MTF — Crypto Momentum Trend Follow
# ---------------------------------------------------------------------------

def signal_crypto_momentum(ctx: BarContext) -> list[TradeIntent]:
    """Fires from bar 8 onward — EMA momentum on crypto + metals.

    Uses EMA(fast) vs EMA(slow) on 15m mids. Up to mtf_max_concurrent legs
    (e.g. BTC + SOL + XAU) each at budget/max_concurrent notional.
    """
    if ctx.mtf_budget <= 0 or ctx.bar_i < ctx.cfg.mtf_start_bar:
        return []
    n_open = sum(1 for _ in ctx.open_mtf_symbols)
    if n_open >= ctx.cfg.mtf_max_concurrent:
        return []

    already_open = ctx.open_mtf_symbols
    available = [s for s in ctx.crypto_symbols
                 if s in ctx.mids.columns and s not in already_open
                 and s not in ctx.open_symbols]
    if not available:
        return []

    candidates: list[tuple[str, float]] = []
    for sym in available:
        if sym in ctx.mtf_stop_cooldown_syms:
            continue
        series = ctx.mids[sym].loc[:ctx.ts]
        if len(series) < ctx.cfg.mtf_slow + 2:
            continue
        slow = _ema(series, ctx.cfg.mtf_slow)
        fast = _ema(series, ctx.cfg.mtf_fast)
        if not (np.isfinite(fast) and np.isfinite(slow) and slow > 0):
            continue
        sig = (fast - slow) / slow
        min_sig = (
            ctx.cfg.mtf_min_signal_metals
            if sym in METAL_SYMBOLS
            else ctx.cfg.mtf_min_signal
        )
        if abs(sig) >= min_sig:
            candidates.append((sym, sig))

    if not candidates:
        return []

    candidates.sort(key=lambda x: -abs(x[1]))
    slots = ctx.cfg.mtf_max_concurrent - n_open
    # Per-leg notional: split budget equally across max_concurrent slots so
    # each leg is budget/max_concurrent of equity (e.g., 6x / 2 = 3x per leg).
    per_leg = ctx.mtf_budget * ctx.cfg.init_equity / ctx.cfg.mtf_max_concurrent

    intents = []
    for sym, sig in candidates[:slots]:
        side = 1 if sig > 0 else -1
        block_key = f"{sym}:{side}"
        if block_key in ctx.blocked_mtf_sides:
            continue
        intents.append(TradeIntent(
            SignalKind.MTF, (sym,), (side,), per_leg,
            priority=7.0,
            reason=f"MTF {sym} EMA-sig={sig:+.4f} {'L' if side > 0 else 'S'}",
        ))
    return intents


# ---------------------------------------------------------------------------
# Signal 3: CSS — Correlation Spread (unchanged logic, tighter entry)
# ---------------------------------------------------------------------------

def signal_css_spread(ctx: BarContext) -> list[TradeIntent]:
    """Dollar-neutral spread when log-spread |z| exceeds entry threshold.

    When COC was never deployed this round (recovery mode): stricter entry, half budget,
    and optionally only the single best-|z| pair to avoid full-basket risk.
    """
    if ctx.bar_i < ctx.cfg.spread_start_bar:
        return []

    cfg = ctx.cfg
    # Recovery = late start with no COC yet. Not when we closed a failed COC book.
    recovery = not ctx.coc_deployed and not ctx.coc_ever_this_round
    budget = ctx.spread_budget
    if recovery:
        budget *= cfg.css_recovery_scale
    if budget <= 0 or not ctx.spread_states:
        return []

    z_entry = cfg.spread_z_entry_recovery if recovery else cfg.spread_z_entry

    candidates: list[tuple[float, str, str, str, int, float]] = []
    for spread_sig, spread_pair in ctx.spread_states:
        sym_a, sym_b = spread_pair
        pair_id = f"{sym_a}/{sym_b}"
        if pair_id in ctx.open_css_pairs or pair_id in ctx.css_pair_cooldown:
            continue
        if sym_a in ctx.open_symbols or sym_b in ctx.open_symbols:
            continue
        z = _zscore(spread_sig, ctx.ts, cfg.spread_z_window)
        if not np.isfinite(z) or abs(z) < z_entry:
            continue
        side = -1 if z > 0 else 1
        candidates.append((abs(z), pair_id, sym_a, sym_b, side, z))

    if not candidates:
        return []

    if recovery and cfg.css_single_pair_no_coc:
        candidates = [max(candidates, key=lambda x: x[0])]

    n_pairs = len(candidates)
    per_leg = budget * cfg.init_equity / n_pairs / 2
    intents: list[TradeIntent] = []
    for _, pair_id, sym_a, sym_b, side, z in candidates:
        mode = "recovery" if recovery else "normal"
        intents.append(TradeIntent(
            SignalKind.CSS, (sym_a, sym_b), (side, -side), per_leg,
            pair_id=pair_id,
            priority=5.0,
            reason=f"CSS z={z:+.2f} on {sym_a}/{sym_b} ({mode})",
        ))
    return intents


# ---------------------------------------------------------------------------
# Spread state builder (used by engine before the bar loop)
# ---------------------------------------------------------------------------

def build_spread_states(kind: RoundKind, mids: pd.DataFrame,
                        spread_budget: float) -> list[tuple[pd.Series, tuple[str, str]]]:
    """Return a list of (log-spread series, pair) for all configured pairs this round."""
    if spread_budget <= 0:
        return []
    pairs = ROUND_CORR_SPREADS.get(kind.value, [])
    states = []
    for pair in pairs:
        sym_a, sym_b = pair
        if sym_a not in mids.columns or sym_b not in mids.columns:
            continue
        states.append((np.log(mids[sym_a]) - np.log(mids[sym_b]), pair))
    return states


# Registry — ordered by evaluation priority; engine calls all each bar.
SIGNAL_GENERATORS = [
    signal_coc_open,
    signal_coc_catchup,
    signal_crypto_momentum,
    signal_css_spread,
]
