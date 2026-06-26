"""Download crypto 15m OHLCV data from Binance public REST API.

No API key required — uses the public /api/v3/klines endpoint.
Writes panel/{SYM}_15m.parquet in the same schema as the forex panel.

Usage:
    py -3.10 fetch_crypto.py --panel .\panel
    py -3.10 fetch_crypto.py --panel .\panel --start 2026-05-01 --end 2026-06-11
"""
from __future__ import annotations
import argparse
import os
import time
import datetime as dt
import requests
import pandas as pd

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

# Binance symbol -> our canonical name (USDT pairs, treated as ~USD for backtest)
CRYPTO_MAP = {
    "BTCUSDT": "BTCUSD",
    "ETHUSDT": "ETHUSD",
    "SOLUSDT": "SOLUSD",
    "XRPUSDT": "XRPUSD",
}

# Estimated bid-ask spread in bps (Binance spot; futures tighter but reasonable proxy)
SPREAD_BPS_EST = {
    "BTCUSD": 1.0,
    "ETHUSD": 2.0,
    "SOLUSD": 5.0,
    "XRPUSD": 5.0,
}

INTERVAL = "15m"
LIMIT = 1000  # Binance max per request


def _ts_ms(date_str: str) -> int:
    """ISO date string -> millisecond epoch (UTC midnight)."""
    d = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


def _fetch_klines(binance_sym: str, start_ms: int, end_ms: int) -> list[list]:
    """Page through Binance klines API and return all raw rows."""
    rows: list[list] = []
    cursor = start_ms
    session = requests.Session()

    while cursor < end_ms:
        resp = session.get(BINANCE_KLINES, params={
            "symbol":    binance_sym,
            "interval":  INTERVAL,
            "startTime": cursor,
            "endTime":   end_ms,
            "limit":     LIMIT,
        }, timeout=20)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        # Next page starts from close_time of last candle + 1 ms
        cursor = int(batch[-1][6]) + 1
        if len(batch) < LIMIT:
            break
        time.sleep(0.15)  # polite rate limiting

    return rows


def fetch_crypto_panel(panel_dir: str,
                       start: str = "2026-05-01",
                       end: str = "2026-06-11") -> None:
    os.makedirs(panel_dir, exist_ok=True)
    start_ms = _ts_ms(start)
    end_ms   = _ts_ms(end)

    for binance_sym, sym in CRYPTO_MAP.items():
        print(f"  Binance {binance_sym} -> {sym} ...", end=" ", flush=True)

        try:
            rows = _fetch_klines(binance_sym, start_ms, end_ms)
        except Exception as exc:
            print(f"ERROR: {exc}")
            continue

        if not rows:
            print("NO DATA")
            continue

        # Binance kline columns:
        # 0  open_time (ms)
        # 1  open  2 high  3 low  4 close
        # 5  volume (base)  6 close_time  7 quote_volume  8 num_trades  ...
        df = pd.DataFrame(rows, columns=[
            "open_time", "open", "high", "low", "close",
            "volume", "close_time", "quote_vol", "n_trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ])

        # Convert types
        df["ts"]        = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True).dt.tz_convert(None)
        df["mid_open"]  = df["open"].astype(float)
        df["mid_high"]  = df["high"].astype(float)
        df["mid_low"]   = df["low"].astype(float)
        df["mid_close"] = df["close"].astype(float)
        df["n_ticks"]   = df["n_trades"].astype(int)
        df["spread_bps"] = SPREAD_BPS_EST.get(sym, 5.0)
        df["symbol"]    = sym

        df = (df[["ts", "mid_open", "mid_high", "mid_low", "mid_close",
                   "spread_bps", "n_ticks", "symbol"]]
              .query("mid_close > 0")
              .drop_duplicates("ts")
              .sort_values("ts")
              .reset_index(drop=True))

        out_path = os.path.join(panel_dir, f"{sym}_15m.parquet")
        df.to_parquet(out_path, index=False)
        print(f"{len(df)} bars  ({df['ts'].iloc[0].date()} -> {df['ts'].iloc[-1].date()})")

    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel", default=r".\panel")
    parser.add_argument("--start", default="2026-05-01")
    parser.add_argument("--end",   default="2026-06-11")
    args = parser.parse_args()
    fetch_crypto_panel(args.panel, args.start, args.end)
