"""Download Binance 1-second BTCUSDT klines for scalp backtesting."""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "1s"
LIMIT = 1000  # max for 1s on Binance


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fetch_1s_klines(start: datetime, end: datetime) -> pd.DataFrame:
    cursor = _ms(start)
    end_ms = _ms(end)
    rows: list[list] = []
    session = requests.Session()

    while cursor < end_ms:
        resp = session.get(
            BINANCE_KLINES,
            params={
                "symbol": SYMBOL,
                "interval": INTERVAL,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": LIMIT,
            },
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        cursor = int(batch[-1][0]) + 1000  # next second
        if len(batch) < LIMIT:
            break
        if len(rows) % 50000 == 0:
            print(f"  ... {len(rows)} candles")
        time.sleep(0.08)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(
        rows,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore",
        ],
    )
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    return df[["ts", "open", "high", "low", "close", "volume"]].drop_duplicates("ts")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True, help="UTC ISO e.g. 2026-06-23T19:00:00")
    p.add_argument("--end", required=True, help="UTC ISO e.g. 2026-06-24T11:00:00")
    p.add_argument("--out", default="research/player758/btc_1s.parquet")
    args = p.parse_args()

    start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
    end = datetime.fromisoformat(args.end.replace("Z", "+00:00"))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"Fetching {SYMBOL} 1s from {start} to {end} ...")
    df = fetch_1s_klines(start, end)
    df.to_parquet(out, index=False)
    print(f"wrote {len(df)} rows -> {out}")


if __name__ == "__main__":
    main()
