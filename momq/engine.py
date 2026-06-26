"""Stage 2: the spread mean-reversion backtest engine.

Design decisions that make this faithful to MoMQ:
  * Equity is marked to market on EVERY 15-min bar, with open positions valued
    at current mid. That equity series is what Sharpe/MaxDD are computed from.
  * Each pair is an independent market-neutral sub-strategy; total equity is
    init + sum of pair PnLs. Not netting exposure across pairs OVERCHARGES cost
    slightly -> conservative, which is the side of the line we want.
  * Hedge ratio (beta) and z-score use ONLY past bars (no look-ahead). beta is
    rolling OLS of log(A) on log(B). Legs are beta-WEIGHTED in notional, which
    is what actually makes a non-1:1 spread (XAU/XAG) neutral.
  * Costs come from the panel's measured spreads, haircut + slippage applied.
  * A z-stop and a time-stop bound the loss on any single trade.

leg_b=None => trade the instrument's own z-score reversion (already-stationary
crosses), with no second leg and no second-leg cost.
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import pandas as pd

from .config import BacktestConfig, Pair
from .metrics import compute_scorecard, Scorecard


@dataclass
class _Trade:
    pair: str
    side: int          # +1 long-spread (long A / short B), -1 short-spread
    open_ts: pd.Timestamp
    close_ts: pd.Timestamp
    pnl: float         # net of costs


def _rolling_beta(la: pd.Series, lb: pd.Series, window: int) -> pd.Series:
    """Rolling OLS slope of la on lb (beta), past-only via pandas rolling."""
    cov = la.rolling(window).cov(lb)
    var = lb.rolling(window).var()
    return (cov / var).replace([np.inf, -np.inf], np.nan)


def _build_signal(mids: pd.DataFrame, pair: Pair, sig) -> pd.DataFrame:
    """Return a frame with columns: spread, z, beta, plus leg mids, on the grid.
    All quantities at bar t use data up to and including t (decide on close)."""
    a = mids[pair.leg_a]
    out = pd.DataFrame(index=mids.index)
    out["midA"] = a
    la = np.log(a) if sig.use_log else a

    if pair.leg_b is None:
        out["beta"] = 0.0
        out["midB"] = np.nan
        spread = la
    else:
        b = mids[pair.leg_b]
        out["midB"] = b
        lb = np.log(b) if sig.use_log else b
        beta = _rolling_beta(la, lb, sig.beta_window)
        out["beta"] = beta
        spread = la - beta * lb

    out["spread"] = spread
    mu = spread.rolling(sig.z_window).mean()
    sd = spread.rolling(sig.z_window).std(ddof=1)
    out["z"] = (spread - mu) / sd
    out["sd_spread"] = sd      # spread vol in log units, for risk sizing
    return out


def run_backtest(mids: pd.DataFrame, spreads_bps: pd.DataFrame,
                 cfg: BacktestConfig) -> tuple[pd.Series, list[_Trade], Scorecard]:
    """Bar-by-bar engine across all configured pairs sharing one equity pool."""
    sig = cfg.signal
    grid = mids.index

    # Pre-compute per-pair signals (vectorised) where the legs exist in the data.
    pairs, signals = [], {}
    for p in cfg.pairs:
        if p.leg_a not in mids.columns:
            continue
        if p.leg_b is not None and p.leg_b not in mids.columns:
            continue
        pairs.append(p)
        signals[p.label] = _build_signal(mids, p, sig)
    if not pairs:
        raise ValueError("No configured pairs are present in the panel columns: "
                         f"{list(mids.columns)}")

    # Per-pair open-position state.
    pos = {p.label: 0 for p in pairs}          # -1/0/+1
    entry = {p.label: None for p in pairs}      # dict of entry data
    bars_held = {p.label: 0 for p in pairs}
    realized = 0.0
    trades: list[_Trade] = []
    n_wins = 0

    equity_curve = np.full(len(grid), np.nan)
    gross_curve = np.full(len(grid), np.nan)
    max_single = 0.0      # rule 13.3 single-instrument concentration
    max_netdir = 0.0      # rule 13.3 net directional concentration

    def half_spread_cost(sym_bps: float, notional: float) -> float:
        eff_bps = sym_bps * cfg.cost.spread_haircut + cfg.cost.extra_slippage_bps
        # last-look: model rejected favourable fills as an extra penalty
        eff_bps *= (1.0 + cfg.cost.last_look_reject_frac)
        return abs(notional) * (eff_bps / 1e4) * 0.5

    for i, ts in enumerate(grid):
        equity = cfg.init_equity + realized
        # ---- mark open positions + decide ----
        unreal_total = 0.0
        gross_total = 0.0
        for p in pairs:
            S = signals[p.label]
            z = S["z"].iat[i]; beta = S["beta"].iat[i]
            midA = S["midA"].iat[i]; midB = S["midB"].iat[i]
            sd = S["sd_spread"].iat[i]
            tradable = (i >= sig.min_history and np.isfinite(z) and np.isfinite(midA)
                        and (p.leg_b is None or np.isfinite(midB)))

            st = pos[p.label]
            # mark existing position
            unreal = 0.0
            if st != 0 and entry[p.label] is not None:
                e = entry[p.label]
                legA = e["NA"] * (midA / e["midA"] - 1.0)
                legB = e["NB"] * (midB / e["midB"] - 1.0) if p.leg_b is not None else 0.0
                unreal = st * (legA - legB)
                gross_total += abs(e["NA"]) + (abs(e["NB"]) if p.leg_b is not None else 0.0)

            if not tradable:
                unreal_total += unreal
                continue

            if st == 0:
                # entry?
                side = 0
                if z > sig.z_entry:   side = -1   # spread rich -> short spread
                elif z < -sig.z_entry: side = +1  # spread cheap -> long spread
                if side != 0 and np.isfinite(sd) and sd > 0:
                    # size: loss at z_stop ~ NA * (z_stop - z_entry) * sd  ==  risk_frac*equity
                    move = max((cfg.risk.z_stop - sig.z_entry), 0.5) * sd
                    NA = (cfg.risk.risk_frac * equity) / move if move > 0 else 0.0
                    NB = abs(beta) * NA if p.leg_b is not None else 0.0
                    gross = NA + NB
                    cap = cfg.risk.leverage_cap * equity
                    if gross > cap and gross > 0:        # respect leverage cap
                        NA *= cap / gross; NB *= cap / gross
                    cost = half_spread_cost(spreads_bps[p.leg_a].iat[i], NA)
                    if p.leg_b is not None:
                        cost += half_spread_cost(spreads_bps[p.leg_b].iat[i], NB)
                    realized -= cost
                    entry[p.label] = {"midA": midA, "midB": midB, "NA": NA, "NB": NB,
                                       "open_ts": ts}
                    pos[p.label] = side
                    bars_held[p.label] = 0
                    gross_total += NA + NB
            else:
                bars_held[p.label] += 1
                adverse = (st == +1 and z <= -cfg.risk.z_stop) or \
                          (st == -1 and z >= cfg.risk.z_stop)
                revert = abs(z) <= sig.z_exit
                timeout = bars_held[p.label] >= sig.time_stop_bars
                if revert or adverse or timeout:
                    e = entry[p.label]
                    legA = e["NA"] * (midA / e["midA"] - 1.0)
                    legB = e["NB"] * (midB / e["midB"] - 1.0) if p.leg_b is not None else 0.0
                    pnl_gross = st * (legA - legB)
                    cost = half_spread_cost(spreads_bps[p.leg_a].iat[i], e["NA"])
                    if p.leg_b is not None:
                        cost += half_spread_cost(spreads_bps[p.leg_b].iat[i], e["NB"])
                    realized += pnl_gross - cost
                    net = pnl_gross - cost
                    trades.append(_Trade(p.label, st, e["open_ts"], ts, net))
                    if net > 0: n_wins += 1
                    pos[p.label] = 0; entry[p.label] = None; unreal = 0.0
                else:
                    unreal_total += unreal
                    continue
            unreal_total += unreal

        equity_curve[i] = cfg.init_equity + realized + unreal_total
        gross_curve[i] = gross_total

        # ---- rule 13.3 exposure concentration check (post-decision) ----
        net_by_sym: dict[str, float] = {}
        gross_by_sym: dict[str, float] = {}
        for p in pairs:
            if pos[p.label] == 0 or entry[p.label] is None:
                continue
            e = entry[p.label]; st = pos[p.label]
            cA = st * e["NA"]                       # +long A if long-spread
            net_by_sym[p.leg_a] = net_by_sym.get(p.leg_a, 0.0) + cA
            gross_by_sym[p.leg_a] = gross_by_sym.get(p.leg_a, 0.0) + abs(cA)
            if p.leg_b is not None:
                cB = -st * e["NB"]                  # short B if long-spread
                net_by_sym[p.leg_b] = net_by_sym.get(p.leg_b, 0.0) + cB
                gross_by_sym[p.leg_b] = gross_by_sym.get(p.leg_b, 0.0) + abs(cB)
        tot_gross = sum(gross_by_sym.values())
        if tot_gross > 0:
            max_single = max(max_single, max(gross_by_sym.values()) / tot_gross)
            max_netdir = max(max_netdir, abs(sum(net_by_sym.values())) / tot_gross)

    eq = pd.Series(equity_curve, index=grid).ffill().dropna()
    gl = pd.Series(gross_curve, index=grid).reindex(eq.index) / eq
    card = compute_scorecard(eq, n_trades=len(trades), n_wins=n_wins,
                             init_equity=cfg.init_equity, gross_lev=gl,
                             max_single_instrument=max_single,
                             max_net_directional=max_netdir)
    return eq, trades, card
