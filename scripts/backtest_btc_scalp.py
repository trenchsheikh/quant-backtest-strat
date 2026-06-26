"""Backtest reverse-engineered player-758 BTC ping-pong scalp strategy."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass
class ScalpConfig:
    lot_btc: float = 1.0
    tp_usd: float = 3.67
    sl_usd: float = 1.58
    max_hold_s: float = 4.5
    spread_bps: float = 0.25
    flip_on_close: bool = True
    momentum_lookback_s: int = 2
    initial_equity: float = 1_000_000.0


@dataclass
class BacktestResult:
    trades: int
    net_pnl: float
    return_pct: float
    win_rate: float
    avg_pnl: float
    median_hold_s: float
    max_dd_pct: float
    sharpe_15m: float
    trades_per_hour: float


def _sharpe_15m(equity: pd.Series) -> float:
    eq = equity.resample("15min").last().ffill().dropna()
    if len(eq) < 3:
        return 0.0
    r = eq.pct_change().dropna()
    std = r.std(ddof=1)
    if std == 0 or not np.isfinite(std):
        return 0.0
    return float(r.mean() / std)


def _long_pnl(entry: float, bid: float, lot: float) -> float:
    return (bid - entry) * lot


def _short_pnl(entry: float, ask: float, lot: float) -> float:
    return (entry - ask) * lot


def run_backtest(bars: pd.DataFrame, cfg: ScalpConfig) -> tuple[BacktestResult, pd.DataFrame, pd.Series]:
    df = bars.sort_values("ts").reset_index(drop=True)
    half = cfg.spread_bps / 10_000.0

    pos = 0
    entry_px = 0.0
    entry_ts: pd.Timestamp | None = None
    pending_dir = 1

    trades: list[dict] = []
    equity = cfg.initial_equity
    eq_pts: list[tuple[pd.Timestamp, float]] = []

    for i in range(cfg.momentum_lookback_s, len(df)):
        ts = df.at[i, "ts"]
        mid = df.at[i, "close"]
        bid = mid * (1.0 - half)
        ask = mid * (1.0 + half)
        hi_bid = df.at[i, "high"] * (1.0 - half)
        lo_bid = df.at[i, "low"] * (1.0 - half)
        hi_ask = df.at[i, "high"] * (1.0 + half)
        lo_ask = df.at[i, "low"] * (1.0 + half)

        if pos != 0:
            hold_s = (ts - entry_ts).total_seconds() if entry_ts is not None else 0.0
            if pos == 1:
                best = _long_pnl(entry_px, hi_bid, cfg.lot_btc)
                worst = _long_pnl(entry_px, lo_bid, cfg.lot_btc)
                mtm = _long_pnl(entry_px, bid, cfg.lot_btc)
            else:
                best = _short_pnl(entry_px, lo_ask, cfg.lot_btc)
                worst = _short_pnl(entry_px, hi_ask, cfg.lot_btc)
                mtm = _short_pnl(entry_px, ask, cfg.lot_btc)

            reason = ""
            realized = mtm
            if best >= cfg.tp_usd:
                realized = cfg.tp_usd
                reason = "tp"
            elif worst <= -cfg.sl_usd:
                realized = -cfg.sl_usd
                reason = "sl"
            elif hold_s >= cfg.max_hold_s:
                realized = mtm
                reason = "time"

            if reason:
                equity += realized
                trades.append({
                    "close_ts": ts,
                    "direction": "long" if pos == 1 else "short",
                    "hold_s": hold_s,
                    "pnl": realized,
                    "reason": reason,
                })
                eq_pts.append((ts, equity))
                if cfg.flip_on_close:
                    pending_dir = -pos
                else:
                    lb = i - cfg.momentum_lookback_s
                    pending_dir = 1 if df.at[i, "close"] >= df.at[lb, "close"] else -1
                pos = 0
                entry_ts = None
            continue

        lb = i - cfg.momentum_lookback_s
        mom_dir = 1 if df.at[i, "close"] >= df.at[lb, "close"] else -1
        direction = pending_dir if cfg.flip_on_close else mom_dir

        if direction == 1:
            pos = 1
            entry_px = ask
        else:
            pos = -1
            entry_px = bid
        entry_ts = ts

    if not eq_pts:
        eq_pts.append((df.at[0, "ts"], equity))
    eq_series = pd.Series(
        [e for _, e in eq_pts],
        index=pd.DatetimeIndex([t for t, _ in eq_pts]),
        name="equity",
    ).sort_index()

    tdf = pd.DataFrame(trades)
    hours = max((df["ts"].iloc[-1] - df["ts"].iloc[0]).total_seconds() / 3600.0, 1e-9)
    runmax = eq_series.cummax()
    dd = ((eq_series / runmax) - 1.0).min() * 100.0
    n = len(tdf)
    result = BacktestResult(
        trades=n,
        net_pnl=float(tdf["pnl"].sum()) if n else 0.0,
        return_pct=100.0 * (equity / cfg.initial_equity - 1.0),
        win_rate=float((tdf["pnl"] > 0).mean()) if n else 0.0,
        avg_pnl=float(tdf["pnl"].mean()) if n else 0.0,
        median_hold_s=float(tdf["hold_s"].median()) if n else 0.0,
        max_dd_pct=float(dd),
        sharpe_15m=_sharpe_15m(eq_series),
        trades_per_hour=n / hours,
    )
    return result, tdf, eq_series


def player_window_stats(path: Path, start: str, end: str) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    df = pd.DataFrame(raw["trades"])
    df["traded_at"] = pd.to_datetime(df["traded_at"], utc=True, format="ISO8601")
    t0 = pd.Timestamp(start)
    t1 = pd.Timestamp(end)
    sub = df[
        (df["symbol"] == "BTCUSD")
        & (df["last_qty"] >= 0.99)
        & (df["traded_at"] >= t0)
        & (df["traded_at"] <= t1)
    ]
    hours = max((t1 - t0).total_seconds() / 3600.0, 1e-9)
    return {
        "player_trades": int(len(sub)),
        "player_net_pnl": float(sub["realized_pnl"].sum()),
        "player_win_rate": float((sub["realized_pnl"] > 0).mean()) if len(sub) else 0.0,
        "player_avg_pnl": float(sub["realized_pnl"].mean()) if len(sub) else 0.0,
        "player_trades_per_hour": len(sub) / hours,
    }


def grid_search(bars: pd.DataFrame, target_pnl: float) -> tuple[ScalpConfig, BacktestResult]:
    best_cfg = ScalpConfig()
    best_res = None
    best_score = float("inf")

    for tp in (3.0, 3.67, 4.5, 5.5):
        for sl in (1.2, 1.58, 2.0):
            for hold in (3.5, 4.5, 5.5, 7.0):
                for spread in (0.0, 0.15, 0.25, 0.35):
                    cfg = ScalpConfig(tp_usd=tp, sl_usd=sl, max_hold_s=hold, spread_bps=spread)
                    res, _, _ = run_backtest(bars, cfg)
                    # match pnl, trade count, win rate jointly
                    trade_err = abs(res.trades_per_hour - 815) / 815
                    pnl_err = abs(res.net_pnl - target_pnl) / max(abs(target_pnl), 1.0)
                    wr_err = abs(res.win_rate - 0.656)
                    score = pnl_err + 0.5 * trade_err + 0.3 * wr_err
                    if score < best_score:
                        best_score = score
                        best_cfg = cfg
                        best_res = res
    assert best_res is not None
    return best_cfg, best_res


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--bars", default="research/player758/btc_1s.parquet")
    p.add_argument("--start", default="2026-06-23T22:00:00+00:00")
    p.add_argument("--end", default="2026-06-24T11:00:00+00:00")
    p.add_argument("--out", default="research/player758/backtest_results.json")
    args = p.parse_args()

    bars = pd.read_parquet(args.bars)
    bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
    t0 = pd.Timestamp(args.start)
    t1 = pd.Timestamp(args.end)
    bars = bars[(bars["ts"] >= t0) & (bars["ts"] <= t1)].copy()

    player = player_window_stats(Path("research/player758/trades.json"), args.start, args.end)
    cfg, res = grid_search(bars, player["player_net_pnl"])
    _, trades, eq = run_backtest(bars, cfg)

    out = {
        "window": f"{args.start} .. {args.end}",
        **player,
        "backtest": asdict(res),
        "fitted_config": asdict(cfg),
        "strategy_spec": {
            "name": "btc_ping_pong_scalp",
            "symbol": "BTCUSD",
            "description": (
                "Always-in-market 1 BTC clips. Alternate long/short on each close (72% flip). "
                "Exit at +TP USD, -SL USD, or max_hold seconds using 1s bar high/low. "
                "Entry at ask (long) / bid (short) with half-spread."
            ),
        },
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    trades.to_csv(out_path.parent / "backtest_trades.csv", index=False)
    eq.to_csv(out_path.parent / "backtest_equity.csv")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
