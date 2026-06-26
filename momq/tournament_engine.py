"""Tournament engine — signal-driven execution scored by the Judge.

Architecture:
  signals.py  → proposes TradeIntents every bar (the algorithm)
  engine.py   → executes intents under RD constraints (the broker)
  judge.py    → scores the round per rules.md (the tournament judge)

Three-sleeve design:
  COC (priority 10): contrarian carry at round open — vol-parity sized legs
  MTF (priority  7): crypto EMA momentum — one position at a time, exits via
                     stop-loss / reversal / max-hold
  CSS (priority  5): correlation spread z-reversion — dollar-neutral pair
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import pandas as pd

from .judge import TournamentJudge, RiskTelemetry, JudgeVerdict
from .metrics import Scorecard
from .signals import (
    BarContext, TradeIntent, SignalKind,
    SIGNAL_GENERATORS, build_spread_states,
)
from .tournament_config import (
    TournamentConfig, RoundKind, ROUND_OPEN_DOW, PRIMARY_SYMBOLS, MOMENTUM_SYMBOLS,
)


@dataclass
class RoundTrade:
    symbol: str
    side: int                   # +1 long / -1 short
    sleeve: str                 # coc / mtf / css
    open_ts: pd.Timestamp
    close_ts: pd.Timestamp
    pnl: float                  # net PnL after all costs
    pair: str = ""
    # ---- detailed trade record ----
    notional: float = 0.0       # position size in USD
    entry_price: float = 0.0
    exit_price: float = 0.0
    gross_pnl: float = 0.0      # PnL before spread cost
    cost: float = 0.0           # round-trip spread + slippage
    duration_bars: int = 0      # bars held (1 bar = 15 min)
    exit_reason: str = "eod"    # eod | finals_bank | z_reversion | max_hold | stop_loss | ema_reversal | mtf_tp | mtf_trail | mtf_profit_reversal | coc_tp | coc_sl
    sl_price: float = 0.0       # price level at which stop-loss triggers (0 = no SL)
    tp_price: float = 0.0       # COC only: price level at which take-profit triggers (0 = no TP)
    signal_info: str = ""       # human-readable entry signal description


@dataclass
class RoundResult:
    round_kind: RoundKind
    round_start: pd.Timestamp
    round_end: pd.Timestamp
    equity: pd.Series
    trades: list[RoundTrade]
    card: Scorecard
    verdict: JudgeVerdict | None = None


@dataclass
class _Pos:
    symbol: str
    side: int
    notional: float
    entry_mid: float
    open_ts: pd.Timestamp
    sleeve: str
    bars: int = 0
    pair_id: str = ""
    leg_b_sym: str = ""
    entry_mid_b: float = 0.0
    notional_b: float = 0.0
    # ---- detailed entry metadata ----
    signal_info: str = ""       # entry signal description from TradeIntent.reason
    entry_equity: float = 0.0   # equity at moment of entry
    sl_price: float = 0.0       # stop-loss price
    tp_price: float = 0.0       # take-profit price (COC only)
    ever_positive: bool = False  # True once unrealised PnL has been > 0 (used by TD stop)
    entry_z: float = 0.0        # CSS: z-score at entry (for z-stop)
    peak_mtm_pct: float = 0.0   # MTF: highest price-based MTM seen (trailing stop)


def find_competition_rounds(index: pd.DatetimeIndex,
                            kinds: list[RoundKind] | None = None
                            ) -> list[tuple[RoundKind, pd.Timestamp, pd.Timestamp]]:
    kinds = kinds or list(RoundKind)
    out: list[tuple[RoundKind, pd.Timestamp, pd.Timestamp]] = []
    for kind in kinds:
        open_dow = ROUND_OPEN_DOW[kind]
        hours = 48 if kind == RoundKind.FINALS else 24
        for day in index[index.dayofweek == open_dow].normalize().unique():
            t0 = day + pd.Timedelta(hours=22)
            t1 = t0 + pd.Timedelta(hours=hours)
            sub = index[(index >= t0) & (index <= t1)]
            if len(sub) >= 8:
                out.append((kind, t0, t1))
    return sorted(out, key=lambda x: x[1])


def _cost(bps: float, notional: float, cfg: TournamentConfig) -> float:
    eff = bps * cfg.spread_haircut + cfg.extra_slippage_bps
    eff *= 1.0 + cfg.last_look_reject_frac
    return abs(notional) * (eff / 1e4) * 0.5


def _parse_css_entry_z(signal_info: str) -> float:
    """Extract entry z from CSS signal_info like 'CSS z=-1.32 on EURUSD/USDCHF'."""
    import re
    m = re.search(r"z=([+-]?\d+\.?\d*)", signal_info)
    if not m:
        return 0.0
    try:
        return float(m.group(1))
    except ValueError:
        return 0.0


def _zscore(series: pd.Series, ts: pd.Timestamp, window: int) -> float:
    sub = series.loc[:ts].tail(window)
    if len(sub) < window // 2:
        return np.nan
    mu, sd = sub.mean(), sub.std(ddof=1)
    if sd <= 0 or not np.isfinite(sd):
        return np.nan
    return float((sub.iloc[-1] - mu) / sd)


def _ema_last(series: pd.Series, span: int) -> float:
    if len(series) < 2:
        return float(series.iloc[-1]) if len(series) else np.nan
    return float(series.ewm(span=span, adjust=False).mean().iloc[-1])


class Portfolio:
    def __init__(self, cfg: TournamentConfig):
        self.cfg = cfg
        self.positions: list[_Pos] = []
        self.realized = 0.0
        self.trades: list[RoundTrade] = []
        self.n_wins = 0

    def gross_notional(self) -> float:
        return sum(abs(p.notional) + abs(p.notional_b) for p in self.positions)

    def exposure_stats(self) -> tuple[float, float]:
        gross_by: dict[str, float] = {}
        net_by: dict[str, float] = {}
        for p in self.positions:
            gross_by[p.symbol] = gross_by.get(p.symbol, 0) + abs(p.notional)
            net_by[p.symbol] = net_by.get(p.symbol, 0) + p.side * p.notional
            if p.leg_b_sym:
                gross_by[p.leg_b_sym] = gross_by.get(p.leg_b_sym, 0) + abs(p.notional_b)
                net_by[p.leg_b_sym] = net_by.get(p.leg_b_sym, 0) - p.side * p.notional_b
        tot = sum(gross_by.values())
        if tot <= 0:
            return 0.0, 0.0
        return max(gross_by.values()) / tot, abs(sum(net_by.values())) / tot

    def symbol_busy(self, sym: str) -> bool:
        return any(p.symbol == sym or p.leg_b_sym == sym for p in self.positions)

    def can_add(self, add_gross: float, equity: float) -> bool:
        base = max(self.cfg.init_equity, equity)
        return self.gross_notional() + add_gross <= self.cfg.budget.total_cap * base

    def open_single(self, sym: str, side: int, notional: float, mid: float,
                    bps: float, ts: pd.Timestamp, sleeve: str, equity: float,
                    signal_info: str = "") -> bool:
        if self.symbol_busy(sym) or notional <= 0 or not self.can_add(notional, equity):
            return False
        self.realized -= _cost(bps, notional, self.cfg)
        sl_price = 0.0
        tp_price = 0.0
        if sleeve == "mtf" and notional > 0 and equity > 0:
            price_move_pct = self.cfg.mtf_stop_pct * equity / notional
            sl_price = mid * (1.0 - side * price_move_pct)
        elif sleeve == "coc":
            if self.cfg.coc_sl_pct > 0:
                sl_price = mid * (1.0 - side * self.cfg.coc_sl_pct)
            tp_price = mid * (1.0 + side * self.cfg.coc_tp_pct)
        self.positions.append(_Pos(sym, side, notional, mid, ts, sleeve,
                                   signal_info=signal_info,
                                   entry_equity=equity, sl_price=sl_price,
                                   tp_price=tp_price))
        return True

    def open_spread(self, sym_a: str, sym_b: str, side: int, na: float, nb: float,
                    ma: float, mb: float, bps_a: float, bps_b: float,
                    ts: pd.Timestamp, pair_id: str, equity: float,
                    signal_info: str = "", entry_z: float = 0.0) -> bool:
        if self.symbol_busy(sym_a) or self.symbol_busy(sym_b):
            return False
        add = na + nb
        if not self.can_add(add, equity):
            return False
        self.realized -= _cost(bps_a, na, self.cfg) + _cost(bps_b, nb, self.cfg)
        ez = entry_z if entry_z != 0.0 else _parse_css_entry_z(signal_info)
        self.positions.append(_Pos(sym_a, side, na, ma, ts, "css",
                                   pair_id=pair_id, leg_b_sym=sym_b,
                                   entry_mid_b=mb, notional_b=nb,
                                   signal_info=signal_info,
                                   entry_equity=equity, entry_z=ez))
        return True

    def mtm(self, mids: pd.DataFrame, ts: pd.Timestamp) -> float:
        u = 0.0
        for p in self.positions:
            ma = float(mids[p.symbol].loc[ts])
            leg_a = p.notional * (ma / p.entry_mid - 1.0)
            leg_b = 0.0
            if p.leg_b_sym:
                mb = float(mids[p.leg_b_sym].loc[ts])
                leg_b = p.notional_b * (mb / p.entry_mid_b - 1.0)
            u += p.side * (leg_a - leg_b)
        return u

    def pos_mtm(self, p: _Pos, mids: pd.DataFrame, ts: pd.Timestamp) -> float:
        ma = float(mids[p.symbol].loc[ts])
        leg_a = p.notional * (ma / p.entry_mid - 1.0)
        leg_b = 0.0
        if p.leg_b_sym:
            mb = float(mids[p.leg_b_sym].loc[ts])
            leg_b = p.notional_b * (mb / p.entry_mid_b - 1.0)
        return p.side * (leg_a - leg_b)

    def close(self, p: _Pos, mids: pd.DataFrame, spreads: pd.DataFrame,
              ts: pd.Timestamp, exit_reason: str = "eod") -> None:
        ma = float(mids[p.symbol].loc[ts])
        bps_a = float(spreads[p.symbol].loc[ts]) if p.symbol in spreads.columns else 1.0
        leg_a = p.notional * (ma / p.entry_mid - 1.0)
        cost = _cost(bps_a, p.notional, self.cfg)
        leg_b = 0.0
        if p.leg_b_sym:
            mb = float(mids[p.leg_b_sym].loc[ts])
            bps_b = float(spreads[p.leg_b_sym].loc[ts]) if p.leg_b_sym in spreads.columns else 1.0
            leg_b = p.notional_b * (mb / p.entry_mid_b - 1.0)
            cost += _cost(bps_b, p.notional_b, self.cfg)
        gross = p.side * (leg_a - leg_b)
        net = gross - cost
        if not np.isfinite(net):
            return
        self.realized += net
        if net > 0:
            self.n_wins += 1
        duration_bars = max(p.bars, int((ts - p.open_ts).total_seconds() / 900))
        self.trades.append(RoundTrade(
            symbol=p.symbol, side=p.side, sleeve=p.sleeve,
            open_ts=p.open_ts, close_ts=ts, pnl=net,
            pair=p.pair_id or p.symbol,
            notional=p.notional, entry_price=p.entry_mid, exit_price=ma,
            gross_pnl=gross, cost=cost, duration_bars=duration_bars,
            exit_reason=exit_reason, sl_price=p.sl_price, tp_price=p.tp_price,
            signal_info=p.signal_info,
        ))
        self.positions.remove(p)


def _execute_intent(pf: Portfolio, intent: TradeIntent, ctx: BarContext,
                    equity: float) -> bool:
    """Map a TradeIntent to portfolio orders."""
    ts = ctx.ts
    sleeve = intent.kind.value

    if intent.kind == SignalKind.CSS and len(intent.symbols) == 2:
        sym_a, sym_b = intent.symbols
        side = intent.sides[0]
        ma = float(ctx.mids[sym_a].loc[ts])
        mb = float(ctx.mids[sym_b].loc[ts])
        ba = float(ctx.spreads[sym_a].loc[ts]) if sym_a in ctx.spreads.columns else 1.0
        bb = float(ctx.spreads[sym_b].loc[ts]) if sym_b in ctx.spreads.columns else 1.0
        n = intent.notional_per_leg
        return pf.open_spread(sym_a, sym_b, side, n, n, ma, mb, ba, bb, ts,
                              intent.pair_id, equity, signal_info=intent.reason,
                              entry_z=_parse_css_entry_z(intent.reason))

    # Single-leg intent (COC per-leg, MTF)
    opened = False
    for sym, side in zip(intent.symbols, intent.sides):
        if sym not in ctx.mids.columns:
            continue
        mid = float(ctx.mids[sym].loc[ts])
        bps = float(ctx.spreads[sym].loc[ts]) if sym in ctx.spreads.columns else 1.0
        if pf.open_single(sym, side, intent.notional_per_leg, mid, bps, ts, sleeve, equity,
                          signal_info=intent.reason):
            opened = True
    return opened


def run_combined_round(mids: pd.DataFrame, spreads: pd.DataFrame,
                       kind: RoundKind, t0: pd.Timestamp, t1: pd.Timestamp,
                       cfg: TournamentConfig | None = None,
                       judge: TournamentJudge | None = None) -> RoundResult:
    cfg = cfg or TournamentConfig()
    judge = judge or TournamentJudge(cfg.init_equity)
    grid = mids.index[(mids.index >= t0) & (mids.index <= t1)]
    pf = Portfolio(cfg)
    primary = [s for s in PRIMARY_SYMBOLS if s in mids.columns]
    crypto = [s for s in MOMENTUM_SYMBOLS if s in mids.columns]

    # ---- per-round sleeve budgets ------------------------------------------
    coc_budget = cfg.budget.coc
    spread_budget = cfg.budget.corr_spread
    mtf_budget = cfg.budget.mtf

    if kind == RoundKind.R1:
        spread_budget *= cfg.r1_spread_scale
        mtf_budget    *= cfg.r1_mtf_scale
    elif kind == RoundKind.R2:
        coc_budget *= cfg.r2_coc_scale
        spread_budget *= cfg.r2_spread_scale
        mtf_budget    *= cfg.r2_mtf_scale
    elif kind == RoundKind.R3:
        coc_budget    *= cfg.r3_coc_scale        # = 0 (COC flat on R3)
        spread_budget *= cfg.r3_spread_scale      # = 0
        mtf_budget    *= cfg.r3_mtf_scale         # = 1 (MTF is R3's only alpha)
    elif kind == RoundKind.FINALS:
        coc_budget    *= cfg.finals_coc_scale
        spread_budget *= cfg.finals_spread_scale
        mtf_budget    *= cfg.finals_mtf_scale

    spread_states = build_spread_states(kind, mids, spread_budget)
    # pair_sig_map: pair_id → log-spread series for fast lookup during CSS exits
    pair_sig_map: dict[str, pd.Series] = {
        f"{pair[0]}/{pair[1]}": sig for sig, pair in spread_states
    }
    open_spread_pair_ids: set[str] = set()   # pair_ids of currently-open CSS positions
    eq_curve: list[float] = []
    telemetry = RiskTelemetry()

    # COC re-entry state: sym -> (earliest_reentry_bar, side, notional, signal_info)
    # Populated when a COC leg closes via TP; cleared on execution or round end.
    coc_reentry_queue: dict[str, tuple[int, int, float, str]] = {}
    coc_reentry_used: set[str] = set()  # symbols that have had their one allowed re-entry
    coc_deployed: bool = False

    # MTF stop cooldown: sym -> earliest bar at which MTF may re-enter after a stop-out.
    # Prevents whipsaw re-entry into the same choppy crypto after being stopped out.
    mtf_stop_cooldown: dict[str, int] = {}

    for bar_i, ts in enumerate(grid):
        equity = cfg.init_equity + pf.realized + pf.mtm(mids, ts)

        # ---- Finals: bank COC after 24h ------------------------------------
        if kind == RoundKind.FINALS and (ts - t0) >= pd.Timedelta(hours=cfg.finals_coc_hold_hours):
            for p in list(pf.positions):
                if p.sleeve == "coc":
                    pf.close(p, mids, spreads, ts, "finals_bank")

        # ---- COC active management: TP + time-deterioration stop ----------
        # Only in R1/R2 — Finals banks at 24h; R3 COC is flat.
        if kind in (RoundKind.R1, RoundKind.R2):
            for p in list(pf.positions):
                if p.sleeve != "coc" or p.symbol not in mids.columns:
                    continue
                mid = float(mids[p.symbol].loc[ts])
                if not (np.isfinite(mid) and mid > 0 and p.entry_mid > 0):
                    continue
                price_move = p.side * (mid / p.entry_mid - 1.0)

                # Track if this leg has ever shown positive unrealised PnL
                if not p.ever_positive and price_move > 0:
                    p.ever_positive = True

                if price_move >= cfg.coc_tp_pct:
                    # Take-profit: bank the gain and re-enter after cooldown
                    pf.close(p, mids, spreads, ts, "coc_tp")
                    if p.symbol not in coc_reentry_used:
                        coc_reentry_queue[p.symbol] = (
                            bar_i + cfg.coc_reentry_cooldown,
                            p.side, p.notional, p.signal_info,
                        )
                        coc_reentry_used.add(p.symbol)
                elif (cfg.coc_td_threshold > 0
                      and not p.ever_positive
                      and (ts - p.open_ts).total_seconds() / 900 >= cfg.coc_td_start_bar
                      and price_move <= -cfg.coc_td_threshold):
                    pf.close(p, mids, spreads, ts, "coc_td")
                elif cfg.coc_sl_pct > 0 and price_move <= -cfg.coc_sl_pct:
                    pf.close(p, mids, spreads, ts, "coc_sl")

        # ---- CSS exits: z-reversion, z-stop, or max-hold ------------------
        for p in list(pf.positions):
            if p.sleeve != "css":
                continue
            sig = pair_sig_map.get(p.pair_id)
            if sig is not None:
                z = _zscore(sig, ts, cfg.spread_z_window)
                p.bars += 1
                if p.bars >= cfg.spread_max_hold:
                    pf.close(p, mids, spreads, ts, "max_hold")
                elif np.isfinite(z) and abs(z) <= cfg.spread_z_exit:
                    pf.close(p, mids, spreads, ts, "z_reversion")
                elif (cfg.spread_z_stop > 0 and p.entry_z != 0.0
                      and np.isfinite(z)
                      and abs(z) >= abs(p.entry_z) + cfg.spread_z_stop):
                    pf.close(p, mids, spreads, ts, "z_stop")

        # ---- MTF exits: stop | TP | trail | max-hold | EMA reversal --------
        equity = cfg.init_equity + pf.realized + pf.mtm(mids, ts)
        for p in list(pf.positions):
            if p.sleeve != "mtf":
                continue
            p.bars += 1

            mid = float(mids[p.symbol].loc[ts]) if p.symbol in mids.columns else 0.0
            price_move = (
                p.side * (mid / p.entry_mid - 1.0)
                if np.isfinite(mid) and mid > 0 and p.entry_mid > 0
                else 0.0
            )
            if price_move > p.peak_mtm_pct:
                p.peak_mtm_pct = price_move

            mtm_pnl = pf.pos_mtm(p, mids, ts)
            if equity > 0 and mtm_pnl / equity < -cfg.mtf_stop_pct:
                pf.close(p, mids, spreads, ts, "stop_loss")
                # Impose cooldown: no MTF re-entry on this symbol for N bars
                mtf_stop_cooldown[p.symbol] = bar_i + cfg.mtf_stop_cooldown
                continue

            if cfg.mtf_tp_pct > 0 and price_move >= cfg.mtf_tp_pct:
                pf.close(p, mids, spreads, ts, "mtf_tp")
                continue

            if (
                cfg.mtf_trail_arm_pct > 0
                and cfg.mtf_trail_pct > 0
                and p.peak_mtm_pct >= cfg.mtf_trail_arm_pct
                and price_move <= p.peak_mtm_pct - cfg.mtf_trail_pct
            ):
                pf.close(p, mids, spreads, ts, "mtf_trail")
                continue

            if p.bars >= cfg.mtf_max_hold:
                pf.close(p, mids, spreads, ts, "max_hold")
                continue

            if p.symbol in mids.columns:
                series = mids[p.symbol].loc[:ts]
                if len(series) >= cfg.mtf_slow + 2:
                    fast = _ema_last(series, cfg.mtf_fast)
                    slow = _ema_last(series, cfg.mtf_slow)
                    if np.isfinite(fast) and np.isfinite(slow) and slow > 0:
                        sig = (fast - slow) / slow
                        rev_thresh = cfg.mtf_reversal
                        if (
                            cfg.mtf_profit_reversal_arm_pct > 0
                            and price_move >= cfg.mtf_profit_reversal_arm_pct
                        ):
                            rev_thresh = cfg.mtf_profit_reversal
                            reversed_ = (p.side == 1 and sig < rev_thresh) or \
                                        (p.side == -1 and sig > -rev_thresh)
                            if reversed_:
                                pf.close(p, mids, spreads, ts, "mtf_profit_reversal")
                        else:
                            reversed_ = (p.side == 1 and sig < -rev_thresh) or \
                                        (p.side == -1 and sig > rev_thresh)
                            if reversed_:
                                pf.close(p, mids, spreads, ts, "ema_reversal")

        # ---- Recompute equity after all exits ------------------------------
        equity = cfg.init_equity + pf.realized + pf.mtm(mids, ts)

        # ---- COC re-entry: execute queued re-entries after cooldown --------
        for sym in list(coc_reentry_queue.keys()):
            reentry_bar, side, notional, sig_info = coc_reentry_queue[sym]
            if bar_i < reentry_bar or pf.symbol_busy(sym):
                continue
            if sym not in mids.columns:
                del coc_reentry_queue[sym]
                continue
            mid = float(mids[sym].loc[ts])
            bps = float(spreads[sym].loc[ts]) if sym in spreads.columns else 1.0
            if np.isfinite(mid) and mid > 0:
                pf.open_single(sym, side, notional, mid, bps, ts, "coc", equity,
                               sig_info + " [re-entry after TP]")
            del coc_reentry_queue[sym]
            equity = cfg.init_equity + pf.realized + pf.mtm(mids, ts)

        # ---- Build bar context and gather intents -------------------------
        open_spread_pair_ids = {p.pair_id for p in pf.positions if p.sleeve == "css" and p.pair_id}
        open_mtf_symbols = {p.symbol for p in pf.positions if p.sleeve == "mtf"}
        open_coc_symbols = {p.symbol for p in pf.positions if p.sleeve == "coc"}
        open_symbols = {p.symbol for p in pf.positions}
        for p in pf.positions:
            if p.leg_b_sym:
                open_symbols.add(p.leg_b_sym)
        mtf_cooldown_syms = {sym for sym, until in mtf_stop_cooldown.items() if bar_i < until}

        # When finals_coc_scale > 0, suppress MTF for first 24h (COC+CSS+MTF > RD cap).
        finals_coc_bars = cfg.finals_coc_hold_hours * 4
        suppress_mtf = (
            kind == RoundKind.FINALS
            and cfg.finals_coc_scale > 0
            and bar_i < finals_coc_bars
        )
        effective_mtf_budget = 0.0 if suppress_mtf else mtf_budget

        ctx = BarContext(
            ts=ts, bar_i=bar_i, round_kind=kind, t0=t0, t1=t1,
            mids=mids, spreads=spreads, cfg=cfg,
            coc_budget=coc_budget, spread_budget=spread_budget,
            mtf_budget=effective_mtf_budget, crypto_symbols=crypto,
            spread_states=spread_states, open_css_pairs=open_spread_pair_ids,
            open_coc_symbols=open_coc_symbols,
            open_symbols=open_symbols,
            coc_deployed=coc_deployed,
            open_mtf_symbols=open_mtf_symbols,
            mtf_stop_cooldown_syms=mtf_cooldown_syms,
            primary=primary,
        )

        intents: list[TradeIntent] = []
        for gen in SIGNAL_GENERATORS:
            intents.extend(gen(ctx))
        intents.sort(key=lambda x: -x.priority)

        for intent in intents:
            if intent.kind == SignalKind.CSS and intent.pair_id in open_spread_pair_ids:
                continue
            if _execute_intent(pf, intent, ctx, equity):
                if intent.kind == SignalKind.CSS:
                    open_spread_pair_ids.add(intent.pair_id)
                elif intent.kind == SignalKind.COC:
                    coc_deployed = True
                equity = cfg.init_equity + pf.realized + pf.mtm(mids, ts)

        # ---- Telemetry for risk-discipline scoring -------------------------
        gross = pf.gross_notional()
        ms, nd = pf.exposure_stats()
        lev = gross / cfg.init_equity if cfg.init_equity > 0 else 0.0
        margin = lev / 30.0
        telemetry.append(ts, lev, ms, nd, margin)
        eq_curve.append(equity)

    # ---- Close all remaining positions at round end -----------------------
    for p in list(pf.positions):
        pf.close(p, mids, spreads, grid[-1], "eod")

    eq_series = pd.Series(eq_curve, index=grid)
    verdict = judge.score_round(eq_series, len(pf.trades), pf.n_wins, telemetry)
    return RoundResult(kind, t0, t1, eq_series, pf.trades, verdict.card, verdict)


def run_all_competition_rounds(
        mids: pd.DataFrame, spreads: pd.DataFrame,
        kinds: list[RoundKind] | None = None,
        cfg: TournamentConfig | None = None,
        windows: list[tuple[RoundKind, pd.Timestamp, pd.Timestamp]] | None = None,
) -> list[RoundResult]:
    cfg = cfg or TournamentConfig()
    judge = TournamentJudge(cfg.init_equity)
    rounds = windows if windows is not None else find_competition_rounds(mids.index, kinds)
    return [run_combined_round(mids, spreads, kind, t0, t1, cfg, judge)
            for kind, t0, t1 in rounds]
