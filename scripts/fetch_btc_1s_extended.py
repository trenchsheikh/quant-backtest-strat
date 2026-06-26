"""Download Binance 1s BTC in daily chunks with resume + merge."""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "1s"
LIMIT = 1000


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fetch_1s_klines(start: datetime, end: datetime) -> pd.DataFrame:
    cursor = _ms(start)
    end_ms = _ms(end)
    rows: list[list] = []
    session = requests.Session()

    while cursor < end_ms:
        for attempt in range(5):
            try:
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
                break
            except requests.RequestException:
                if attempt == 4:
                    raise
                time.sleep(1.5 * (attempt + 1))
        else:
            break

        if not batch:
            break
        rows.extend(batch)
        cursor = int(batch[-1][0]) + 1000
        if len(batch) < LIMIT:
            break
        time.sleep(0.06)

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


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_range_chunked(start: datetime, end: datetime, chunk_dir: Path) -> list[Path]:
    chunk_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    if day < start:
        day = start.replace(hour=0, minute=0, second=0, microsecond=0)

    cur = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur < end:
        day_end = min(cur + timedelta(days=1), end)
        if day_end <= start:
            cur += timedelta(days=1)
            continue
        chunk_start = max(cur, start)
        fname = chunk_dir / f"btc_1s_{chunk_start.strftime('%Y%m%d')}.parquet"
        if fname.exists():
            n = len(pd.read_parquet(fname))
            print(f"skip {fname.name} ({n} rows)")
            paths.append(fname)
            cur += timedelta(days=1)
            continue

        print(f"fetch {chunk_start.date()} -> {day_end.date()} ...")
        df = fetch_1s_klines(chunk_start, day_end)
        if df.empty:
            print(f"  WARNING: no data for {chunk_start.date()}")
        else:
            df.to_parquet(fname, index=False)
            print(f"  wrote {len(df)} rows -> {fname.name}")
            paths.append(fname)
        cur += timedelta(days=1)

    return paths


def merge_chunks(paths: list[Path], out: Path) -> pd.DataFrame:
    frames = [pd.read_parquet(p) for p in sorted(paths)]
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    print(f"merged {len(df)} rows -> {out}")
    return df


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--chunk-dir", default="research/player758/btc_1s_chunks")
    p.add_argument("--out", default="research/player758/btc_1s_extended.parquet")
    args = p.parse_args()

    start = _parse_dt(args.start)
    end = _parse_dt(args.end)
    paths = fetch_range_chunked(start, end, Path(args.chunk_dir))
    if paths:
        merge_chunks(paths, Path(args.out))


if __name__ == "__main__":
    main()
