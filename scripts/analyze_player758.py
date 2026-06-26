"""Reverse-engineer player 758 BTC scalp strategy from trade tape."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass
class StrategyParams:
    """Fitted parameters for replication."""
    symbol: str
    lot_size: float
    mode: str  # ping_pong_scalp
    min_hold_seconds: float
    median_hold_seconds: float
    p90_hold_seconds: float
    tp_usd_median: float
    tp_usd_p75: float
    sl_usd_median: float
    sl_usd_p90: float
    trade_interval_median_s: float
    win_rate: float
    avg_pnl_usd: float
    trades_per_hour: float
    active_from: str
    active_to: str
    notes: str


def load_trades(path: Path) -> pd.DataFrame:
    raw = json.loads(path.read_text(encoding="utf-8"))
    df = pd.DataFrame(raw["trades"])
    df["traded_at"] = pd.to_datetime(df["traded_at"], utc=True, format="ISO8601")
    df = df.sort_values("traded_at").reset_index(drop=True)
    df["side_label"] = df["side"].map({1: "buy", 2: "sell"})
    return df


def _match_round_trips(btc: pd.DataFrame) -> pd.DataFrame:
    """FIFO match 1-lot clips into round-trips (open -> close)."""
    rows: list[dict] = []
    pos_side: int | None = None  # 1 long, -1 short
    open_ts = None
    open_price = None
    open_qty = None

    for _, r in btc.iterrows():
        side = int(r["side"])  # 1 buy, 2 sell
        qty = float(r["last_qty"])
        price = float(r["last_price"])
        ts = r["traded_at"]
        pnl = float(r["realized_pnl"])

        if pos_side is None:
            pos_side = 1 if side == 1 else -1
            open_ts, open_price, open_qty = ts, price, qty
            continue

        # closing when sell after long or buy after short
        closing = (pos_side == 1 and side == 2) or (pos_side == -1 and side == 1)
        if closing:
            hold_s = (ts - open_ts).total_seconds()
            if pos_side == 1:
                raw_pnl = (price - open_price) * open_qty
            else:
                raw_pnl = (open_price - price) * open_qty
            rows.append({
                "open_ts": open_ts,
                "close_ts": ts,
                "hold_seconds": hold_s,
                "open_price": open_price,
                "close_price": price,
                "qty": open_qty,
                "direction": "long" if pos_side == 1 else "short",
                "realized_pnl": pnl,
                "implied_pnl": raw_pnl,
            })
            pos_side = None
            open_ts = open_price = open_qty = None

            # if same fill opens new position (flip), rare at 1-lot
            # next iteration handles fresh open

    return pd.DataFrame(rows)


def analyze(trades_path: Path, out_dir: Path) -> StrategyParams:
    out_dir.mkdir(parents=True, exist_ok=True)
    df = load_trades(trades_path)

    # Phase detection
    sym_counts = df.groupby("symbol").size().sort_values(ascending=False)
    sym_counts.to_csv(out_dir / "symbol_counts.csv")

    btc = df[df["symbol"] == "BTCUSD"].copy()
    btc["dt_prev"] = btc["traded_at"].diff().dt.total_seconds()

    # Lot size evolution
    lot_by_hour = (
        btc.set_index("traded_at")
        .resample("1h")["last_qty"]
        .agg(["median", "max", "count"])
    )
    lot_by_hour.to_csv(out_dir / "btc_lots_by_hour.csv")

    # Focus on mature 1.0-lot phase
    mature = btc[btc["last_qty"] >= 0.99].copy()
    if mature.empty:
        mature = btc.copy()

    trips = _match_round_trips(mature)
    trips.to_csv(out_dir / "round_trips.csv", index=False)

    wins = mature[mature["realized_pnl"] > 0]["realized_pnl"]
    losses = mature[mature["realized_pnl"] < 0]["realized_pnl"]

    params = StrategyParams(
        symbol="BTCUSD",
        lot_size=float(mature["last_qty"].median()),
        mode="ping_pong_scalp",
        min_hold_seconds=float(trips["hold_seconds"].min()) if len(trips) else 0.0,
        median_hold_seconds=float(trips["hold_seconds"].median()) if len(trips) else 0.0,
        p90_hold_seconds=float(trips["hold_seconds"].quantile(0.9)) if len(trips) else 0.0,
        tp_usd_median=float(wins.median()) if len(wins) else 0.0,
        tp_usd_p75=float(wins.quantile(0.75)) if len(wins) else 0.0,
        sl_usd_median=float(losses.median()) if len(losses) else 0.0,
        sl_usd_p90=float(losses.quantile(0.1)) if len(losses) else 0.0,  # 10th pct = worst tail
        trade_interval_median_s=float(mature["dt_prev"].median()),
        win_rate=float((mature["realized_pnl"] > 0).mean()),
        avg_pnl_usd=float(mature["realized_pnl"].mean()),
        trades_per_hour=float(len(mature) / max((mature["traded_at"].max() - mature["traded_at"].min()).total_seconds() / 3600, 0.01)),
        active_from=str(mature["traded_at"].min()),
        active_to=str(mature["traded_at"].max()),
        notes="High-frequency BTC 1-lot ping-pong; each API row is a closing fill with realized_pnl.",
    )

    (out_dir / "strategy_params.json").write_text(
        json.dumps(asdict(params), indent=2), encoding="utf-8"
    )

    # Hourly PnL vs player
    hourly = (
        mature.groupby(mature["traded_at"].dt.floor("h"))
        .agg(trades=("realized_pnl", "count"), pnl=("realized_pnl", "sum"))
    )
    hourly.to_csv(out_dir / "hourly_pnl.csv")

    summary = {
        "total_trades": len(df),
        "btc_trades": len(btc),
        "mature_btc_trades": len(mature),
        "round_trips": len(trips),
        "net_pnl_mature": float(mature["realized_pnl"].sum()),
        "params": asdict(params),
    }
    (out_dir / "analysis_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return params


if __name__ == "__main__":
    analyze(
        Path("research/player758/trades.json"),
        Path("research/player758"),
    )
