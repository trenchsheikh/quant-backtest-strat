"""Stage 3: run the full backtest over a directory of tick parquets.

    python run.py --data /path/to/ticks --panel /path/to/panel_out

Point --data at the folder holding ALL your symbol-day parquets (FX + XAU + XAG).
First run resamples everything (slow, ~once); pass --skip-resample afterwards to
reuse the 15-min panel and iterate on parameters fast.
"""
from __future__ import annotations
import argparse
import glob
import os
import pandas as pd

from momq.resample import build_panel, load_aligned, scan_data_range
from momq.engine import run_backtest
from momq.config import BacktestConfig, all_symbol_pairs, Pair


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="dir of tick parquets")
    ap.add_argument("--panel", required=True, help="dir to write/read 15m panel")
    ap.add_argument("--skip-resample", action="store_true",
                    help="reuse existing *_15m.parquet in --panel")
    ap.add_argument("--out", default="bt_results", help="dir for equity/trades output")
    ap.add_argument("--all-symbols", action="store_true",
                    help="trade every symbol in the panel (single-leg z-reversion)")
    ap.add_argument("--per-symbol", action="store_true",
                    help="with --all-symbols, also print a per-symbol scorecard table")
    args = ap.parse_args()

    coverage = scan_data_range(args.data)
    if coverage["start"]:
        print(f"tick data: {coverage['files']} files, {coverage['symbols']} symbols, "
              f"{coverage['days']} trading days "
              f"({coverage['start']} -> {coverage['end']})")
    else:
        print(f"tick data: {coverage['files']} parquet files in {args.data}")

    if args.skip_resample:
        paths = {os.path.basename(p).replace("_15m.parquet", ""): p
                 for p in glob.glob(os.path.join(args.panel, "*_15m.parquet"))}
        print(f"reusing panel: {list(paths)}")
    else:
        print("resampling ticks -> 15m panel (one-off, can be slow)...")
        paths = build_panel(args.data, args.panel)
        print(f"panel built: {list(paths)}")

    mids, spreads = load_aligned(paths)
    t0, t1 = mids.index.min(), mids.index.max()
    n_days = (t1.date() - t0.date()).days + 1
    print(f"backtest window: {t0} -> {t1}  "
          f"({len(mids)} bars, {mids.shape[1]} symbols, {n_days} calendar days)")

    os.makedirs(args.out, exist_ok=True)

    cfg = BacktestConfig()
    if args.all_symbols:
        cfg.pairs = all_symbol_pairs(list(mids.columns))
        print(f"all-symbols mode: {len(cfg.pairs)} instruments "
              f"({mids.index.min().date()} -> {mids.index.max().date()})")
    else:
        present = [p.label for p in cfg.pairs
                   if p.leg_a in mids.columns
                   and (p.leg_b is None or p.leg_b in mids.columns)]
        print(f"trading pairs present in data: {present}")

    eq, trades, card = run_backtest(mids, spreads, cfg)
    print()
    print(card)

    if args.per_symbol and args.all_symbols:
        rows = []
        for sym in sorted(mids.columns):
            one = BacktestConfig(pairs=[Pair(sym, None, f"{sym}-revert")])
            _, sym_trades, sym_card = run_backtest(mids, spreads, one)
            rows.append({
                "symbol": sym,
                "return_pct": sym_card.total_return_pct,
                "sharpe_15m": sym_card.sharpe_15m,
                "max_dd_pct": sym_card.max_drawdown_pct,
                "trades": sym_card.n_trades,
                "win_rate_pct": sym_card.win_rate_pct,
            })
        per_sym = pd.DataFrame(rows).sort_values("return_pct", ascending=False)
        print("\n== Per-symbol scorecards (isolated runs) =======================")
        print(per_sym.to_string(index=False, float_format=lambda x: f"{x:+.3f}"))
        per_sym.to_csv(os.path.join(args.out, "per_symbol.csv"), index=False)
        print(f"wrote per_symbol.csv to {args.out}/")

    eq.to_frame("equity").to_csv(os.path.join(args.out, "equity_15m.csv"))
    pd.DataFrame([t.__dict__ for t in trades]).to_csv(
        os.path.join(args.out, "trades.csv"), index=False)
    with open(os.path.join(args.out, "run_summary.txt"), "w", encoding="utf-8") as f:
        f.write(f"backtest_start={t0}\n")
        f.write(f"backtest_end={t1}\n")
        f.write(f"bars={len(mids)}\n")
        f.write(f"symbols={mids.shape[1]}\n")
        f.write(f"calendar_days={n_days}\n")
        if coverage["start"]:
            f.write(f"tick_data_start={coverage['start']}\n")
            f.write(f"tick_data_end={coverage['end']}\n")
            f.write(f"trading_days={coverage['days']}\n")
        f.write(f"return_pct={card.total_return_pct:.3f}\n")
        f.write(f"sharpe_15m={card.sharpe_15m:.4f}\n")
        f.write(f"trades={card.n_trades}\n")
    print(f"\nwrote equity_15m.csv + trades.csv + run_summary.txt to {args.out}/")
    print("Per-pair sanity: if total Sharpe is good, confirm it's not one pair "
          "carrying it -- run pairs individually before trusting it.")


if __name__ == "__main__":
    main()
