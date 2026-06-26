"""Run multi-sleeve competition tournament backtests.

    py -3.10 run_tournament.py --data .\\backtest-data --panel .\\panel --skip-resample
    py -3.10 run_tournament.py --mode full   # R1 + R2 + R3 + Finals proxies
"""
from __future__ import annotations
import argparse
import glob
import os
from datetime import datetime
import pandas as pd

from momq.resample import build_panel, load_aligned, scan_data_range
from momq.tournament_config import RoundKind, TournamentConfig
from momq.tournament_engine import run_all_competition_rounds, find_competition_rounds, RoundTrade
from momq.metrics import leaderboard_proxy


# -- Display helpers ----------------------------------------------------------

_EXIT_LABELS = {
    "eod":          "Held to round-end (EOD)",
    "finals_bank":  "Banked at Finals +24h profit-take",
    "z_reversion":  "CSS z-score reverted (|z| <= 0.10) — mean-reversion complete",
    "z_stop":       "CSS z-stop — spread diverged past entry threshold",
    "max_hold":     "Max hold reached — time stop triggered",
    "stop_loss":    "MTF stop-loss hit (<-2.5% price move on leg)",
    "ema_reversal": "MTF EMA crossed back — trend reversed",
    "mtf_tp":       "MTF take-profit — +2.0% price move banked",
    "mtf_trail":    "MTF trailing stop — gave back from peak after +1.0%",
    "mtf_profit_reversal": "MTF profit-aware EMA exit — trend weakened while in profit",
    "coc_tp":       "COC take-profit hit (+0.30% price move) — banked, re-entering",
    "coc_sl":       "COC stop-loss hit — closed",
    "coc_td":       "COC time-deterioration stop — never positive in 4h, -0.20% adverse (trend, not reversion)",
}

_SLEEVE_LABELS = {
    "coc": "COC",
    "mtf": "MTF",
    "css": "CSS",
}


def _exit_label(reason: str) -> str:
    return _EXIT_LABELS.get(reason, reason)


def _sl_line(t: RoundTrade) -> str:
    """Return the stop-loss / exit-plan line for a trade."""
    if t.sleeve == "mtf":
        sl = f"SL @ {t.sl_price:.5f}" if t.sl_price > 0 else "SL: price-based"
        return (f"Exit plan: {sl}  |  "
                f"TP: +2.0% / trail / profit-EMA / 6h max-hold  |  "
                f"Actual exit: {_exit_label(t.exit_reason)}")
    if t.sleeve == "css":
        return (f"Exit plan: z-score -> 0.10 (mean-reversion) or 4h max-hold  |  "
                f"Actual exit: {_exit_label(t.exit_reason)}")
    # COC — active TP/SL if levels are set (R1/R2), hold-to-EOD for Finals
    if t.tp_price > 0 or t.sl_price > 0:
        tp_s = f"TP @ {t.tp_price:.5f}" if t.tp_price > 0 else "no TP"
        sl_s = f"SL @ {t.sl_price:.5f}" if t.sl_price > 0 else "no SL"
        return f"Exit plan: {tp_s}  |  {sl_s}  |  Actual exit: {_exit_label(t.exit_reason)}"
    return f"Exit plan: hold to round-end  |  Actual exit: {_exit_label(t.exit_reason)}"


def _print_round_trades(trades: list[RoundTrade], init_equity: float) -> None:
    """Print a detailed trade-by-trade breakdown for one round."""
    if not trades:
        print("    (no trades this round)")
        return

    W = 100
    print(f"  {'-' * W}")
    print(f"  {'#':>2}  {'Slv':<4}  {'Symbol':<8}  {'Dir':<5}  "
          f"{'Opened':<17}  {'Closed':<17}  {'Held':>6}  "
          f"{'Entry':>11}  {'Exit':>11}  {'Chg%':>6}  "
          f"{'Gross P&L':>11}  {'Cost':>8}  {'Net P&L':>11}  {'Eq%':>6}")
    print(f"  {'-' * W}")

    for i, t in enumerate(trades, 1):
        dur_h  = t.duration_bars * 15 / 60
        dir_s  = "LONG " if t.side > 0 else "SHORT"
        slv_s  = _SLEEVE_LABELS.get(t.sleeve, t.sleeve.upper())

        # price change in the direction we traded
        if t.entry_price > 0:
            raw_chg = (t.exit_price / t.entry_price - 1.0) * t.side * 100
        else:
            raw_chg = 0.0

        eq_pct = t.pnl / init_equity * 100
        lev    = t.notional / init_equity if init_equity > 0 else 0.0

        sym_display = t.symbol
        if t.pair and "/" in t.pair:          # CSS two-leg pair
            sym_display = t.pair

        print(
            f"  {i:>2}  {slv_s:<4}  {sym_display:<8}  {dir_s}  "
            f"  {t.open_ts.strftime('%m-%d %H:%M:%S'):<17}"
            f"  {t.close_ts.strftime('%m-%d %H:%M:%S'):<17}"
            f"  {t.duration_bars:>3}b ({dur_h:.1f}h)"
            f"  {t.entry_price:>11.5f}  {t.exit_price:>11.5f}  {raw_chg:>+5.2f}%"
            f"  {t.gross_pnl:>+11,.0f}  {-t.cost:>+8,.0f}  {t.pnl:>+11,.0f}  {eq_pct:>+5.2f}%"
        )

        # Line 2: notional / leverage / signal
        print(f"       Notional: ${t.notional:>12,.0f}  ({lev:.1f}x leverage)")
        if t.signal_info:
            print(f"       Signal:   {t.signal_info}")
        print(f"       {_sl_line(t)}")

        if i < len(trades):
            print()

    # Round totals
    gross_total = sum(t.gross_pnl for t in trades)
    cost_total  = sum(t.cost for t in trades)
    net_total   = sum(t.pnl for t in trades)
    wins        = sum(1 for t in trades if t.pnl > 0)
    print(f"  {'-' * W}")
    print(
        f"  TOTAL  Gross {gross_total:>+12,.0f}  "
        f"Costs {-cost_total:>+8,.0f}  "
        f"Net {net_total:>+12,.0f} ({net_total/init_equity*100:>+5.2f}% of equity)  "
        f"[{wins}W/{len(trades)-wins}L  {wins/len(trades)*100:.0f}% win rate]"
    )
    print(f"  {'-' * W}")


def _write_csv(df: pd.DataFrame, path: str) -> str:
    """Write CSV; if file is locked (e.g. open in Excel), use a timestamped name."""
    try:
        df.to_csv(path, index=False)
        return path
    except PermissionError:
        base, ext = os.path.splitext(path)
        alt = f"{base}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
        df.to_csv(alt, index=False)
        print(f"  WARNING: could not write {path} (file locked?). Wrote {alt}")
        return alt


def main():
    ap = argparse.ArgumentParser(description="MoMQ multi-sleeve tournament backtester")
    ap.add_argument("--data", required=True)
    ap.add_argument("--panel", required=True)
    ap.add_argument("--skip-resample", action="store_true")
    ap.add_argument("--out", default="bt_tournament")
    ap.add_argument("--mode", choices=["r1", "full"], default="full",
                    help="r1 = Sun->Mon only; full = R1+R2+R3+Finals proxies")
    ap.add_argument("--start", default=None,
                    help="Only run rounds opening on or after this date (YYYY-MM-DD). "
                         "Use to exclude pre-competition warmup data when crypto panel "
                         "extends further back than tick data.")
    args = ap.parse_args()

    coverage = scan_data_range(args.data)
    if coverage["start"]:
        print(f"tick data: {coverage['files']} files, {coverage['days']} trading days "
              f"({coverage['start']} -> {coverage['end']})")

    if args.skip_resample:
        paths = {os.path.basename(p).replace("_15m.parquet", ""): p
                 for p in glob.glob(os.path.join(args.panel, "*_15m.parquet"))}
    else:
        print("resampling ticks -> 15m panel...")
        paths = build_panel(args.data, args.panel)

    mids, spreads = load_aligned(paths)
    cfg = TournamentConfig()
    kinds = [RoundKind.R1] if args.mode == "r1" else list(RoundKind)
    windows = find_competition_rounds(mids.index, kinds)
    if args.start:
        start_ts = pd.Timestamp(args.start)
        windows = [(k, t0, t1) for k, t0, t1 in windows if t0 >= start_ts]

    print("\n== Signal-Driven Tournament Engine (Judge-Scored) =============")
    print("  Architecture: signals.py -> engine.py -> judge.py (rules.md S11-13)")
    print("  Final Score = 70% ReturnRank + 15% DDRank + 10% SharpeRank + 5% RD")
    print(f"  Sleeve 1 COC {cfg.budget.coc:.0f}x: contrarian carry (forex, vol-parity sized)")
    print(f"  Sleeve 2 MTF {cfg.budget.mtf:.0f}x: crypto EMA momentum (BTC/ETH/SOL/XRP) [live only]")
    print(f"  Sleeve 3 CSS {cfg.budget.corr_spread:.0f}x: correlation spread z-reversion (R1/R2)")
    print(f"  R3: COC flat, MTF only | Finals: COC close @24h, MTF runs full 48h")
    print(f"  Total leverage cap: {cfg.budget.total_cap}x  |  Mode: {args.mode}  |  Rounds: {len(windows)}\n")

    results = run_all_competition_rounds(mids, spreads, kinds, cfg, windows=windows)
    os.makedirs(args.out, exist_ok=True)

    rows, all_trades = [], []
    for r in results:
        judge_score = r.verdict.final_score if r.verdict else leaderboard_proxy(r.card)
        rows.append({
            "round_kind": r.round_kind.value,
            "round_start": r.round_start,
            "round_end": r.round_end,
            "return_pct": r.card.total_return_pct,
            "sharpe_15m": r.card.sharpe_15m,
            "max_dd_pct": r.card.max_drawdown_pct,
            "trades": r.card.n_trades,
            "win_rate_pct": r.card.win_rate_pct,
            "final_equity": r.card.final_equity,
            "max_gross_lev": r.card.max_gross_lev,
            "risk_discipline": r.verdict.risk_discipline if r.verdict else 100.0,
            "risk_discipline_clean": r.card.risk_discipline_clean,
            "judge_score": judge_score,
            "leaderboard_proxy": leaderboard_proxy(r.card),
        })
        tag = f"{r.round_kind.value}_{r.round_start.date()}"
        _write_csv(r.equity.to_frame("equity"), os.path.join(args.out, f"equity_{tag}.csv"))
        for t in r.trades:
            lev = t.notional / cfg.init_equity if cfg.init_equity > 0 else 0.0
            raw_chg = (t.exit_price / t.entry_price - 1.0) * t.side if t.entry_price > 0 else 0.0
            all_trades.append({
                **t.__dict__,
                "round_kind":   r.round_kind.value,
                "round_start":  r.round_start,
                "direction":    "LONG" if t.side > 0 else "SHORT",
                "leverage_x":   round(lev, 2),
                "price_chg_pct": round(raw_chg * 100, 4),
                "pnl_eq_pct":   round(t.pnl / cfg.init_equity * 100, 4),
                "duration_min": t.duration_bars * 15,
                "sl_price":     t.sl_price if t.sl_price > 0 else "",
                "exit_label":   _exit_label(t.exit_reason),
            })

        js = r.verdict.final_score if r.verdict else leaderboard_proxy(r.card)
        rd = f"{r.verdict.risk_discipline:.0f}" if r.verdict else "?"
        hours = 48 if r.round_kind.value == "finals" else 24
        print(f"\n{'='*102}")
        print(
            f"  [{r.round_kind.value.upper():8s}]  "
            f"{r.round_start.strftime('%Y-%m-%d %H:%M')} -> "
            f"{r.round_end.strftime('%Y-%m-%d %H:%M')}  ({hours}h)"
        )
        print(
            f"  ret={r.card.total_return_pct:+.2f}%  "
            f"sharpe={r.card.sharpe_15m:+.4f}  "
            f"maxDD={r.card.max_drawdown_pct:.3f}%  "
            f"judge={js:.1f}/100  "
            f"trades={r.card.n_trades}  "
            f"win={r.card.win_rate_pct:.0f}%  "
            f"RD={rd}  "
            f"equity=${r.card.final_equity:,.0f}"
        )
        _print_round_trades(r.trades, cfg.init_equity)

    summary = pd.DataFrame(rows)

    # Per round-kind aggregates (print before saving so results show even if CSV locked)
    print("\n== By round type ==============================================")
    for kind in kinds:
        sub = summary[summary["round_kind"] == kind.value]
        if sub.empty:
            continue
        print(f"  {kind.value:8s}  n={len(sub)}  avg_ret={sub['return_pct'].mean():+.2f}%  "
              f"min={sub['return_pct'].min():+.2f}%  avg_sharpe={sub['sharpe_15m'].mean():+.3f}  "
              f"avg_judge={sub['judge_score'].mean():.1f}")

    print("\n== Tournament aggregate =======================================")
    print(f"  Total rounds        : {len(summary)}")
    print(f"  Avg return / round  : {summary['return_pct'].mean():+.2f}%")
    print(f"  Min return / round  : {summary['return_pct'].min():+.2f}%")
    print(f"  Avg Sharpe (15m)    : {summary['sharpe_15m'].mean():+.3f}")
    print(f"  Total trades        : {int(summary['trades'].sum())}")
    print(f"  Avg judge score     : {summary['judge_score'].mean():.1f}")
    print(f"  All rounds positive : {'YES' if (summary['return_pct'] > 0).all() else 'NO'}")
    print(f"  Risk discipline OK  : {'YES' if summary['risk_discipline_clean'].all() else 'NO'}")
    print(f"  Output              : {args.out}/")

    # Weekly tournament paths (R1 Sun -> R2 Mon -> R3 Tue -> Finals Wed)
    print("\n== Weekly tournament paths (if you advance each round) =========")
    r1 = summary[summary["round_kind"] == "round_1"].sort_values("round_start")
    for _, row in r1.iterrows():
        t0 = pd.Timestamp(row["round_start"])
        path = [row]
        for offset, kind in [(1, "round_2"), (2, "round_3"), (3, "finals")]:
            t = t0 + pd.Timedelta(days=offset)
            match = summary[(summary["round_kind"] == kind)
                            & (summary["round_start"].dt.normalize() == t.normalize())]
            if not match.empty:
                path.append(match.iloc[0])
        cum_ret = sum(p["return_pct"] for p in path)
        kinds = " -> ".join(p["round_kind"] for p in path)
        print(f"  {t0.date()}  [{kinds}]  "
              f"rounds={len(path)}  combined_ret={cum_ret:+.2f}%  "
              f"all_pos={'YES' if all(p['return_pct']>0 for p in path) else 'NO'}")
    print("===============================================================")

    # Judge ruling on best round
    best = max(results, key=lambda r: r.verdict.final_score if r.verdict else 0)
    if best.verdict:
        print(f"\n{best.verdict}")
    print("===============================================================\n")

    summary_path = _write_csv(summary, os.path.join(args.out, "rounds_summary.csv"))
    trades_path = _write_csv(pd.DataFrame(all_trades), os.path.join(args.out, "trades.csv"))
    print(f"\n  CSVs: {summary_path}")
    print(f"        {trades_path}")

    summary_txt = os.path.join(args.out, "run_summary.txt")
    try:
        with open(summary_txt, "w", encoding="utf-8") as f:
            f.write("strategy=Leaderboard-Optimized Tournament (COC + R3 flat)\n")
            f.write(f"mode={args.mode}\n")
            f.write(f"rounds={len(summary)}\n")
            f.write(f"avg_return_pct={summary['return_pct'].mean():.3f}\n")
            f.write(f"min_return_pct={summary['return_pct'].min():.3f}\n")
            f.write(f"avg_sharpe_15m={summary['sharpe_15m'].mean():.4f}\n")
            f.write(f"total_trades={int(summary['trades'].sum())}\n")
            f.write(f"all_rounds_positive={(summary['return_pct'] > 0).all()}\n")
            f.write(f"risk_discipline_clean={summary['risk_discipline_clean'].all()}\n")
            f.write(f"avg_judge_score={summary['judge_score'].mean():.2f}\n")
    except PermissionError:
        print(f"  WARNING: could not write {summary_txt} (file locked?)")


if __name__ == "__main__":
    main()
