"""Stage 1: tick parquet -> 15-min panel, via DuckDB (handles the ~20GB without
loading it into RAM).

Input parquet schema (per your files): time, sym, bid, ask, ... (+ L2 depth we
ignore for now). Output per symbol: a 15-min bar table with mid OHLC, the
MEASURED median top-of-book spread in bps, and tick count.

Symbol naming note: your gold file is XAUKUSD (gold per KILOGRAM, ~$139k).
We pass an alias map so the panel uses canonical names (XAUUSD) -- but this is
ONLY for research. Live sizing must read contract specs from MT5 at runtime and
must NOT inherit any price scale from these files (32x scale trap).
"""
from __future__ import annotations
import glob
import os
import re
import duckdb
import pandas as pd

# Map raw parquet symbol -> canonical research symbol.
DEFAULT_ALIASES = {
    "XAUKUSD": "XAUUSD",   # gold per-kg in backtest; per-oz live (rescaled by us)
    "XAGKUSD": "XAGUSD",   # silver, if present with a K
}


def _canon(sym: str, aliases: dict[str, str]) -> str:
    return aliases.get(sym, sym)


def resample_file(path: str, con: duckdb.DuckDBPyConnection,
                  bar: str = "15 minutes") -> pd.DataFrame:
    """Resample one tick parquet to bars. Mid = (bid+ask)/2, computed per tick.
    Locked/crossed rows (ask <= bid) are dropped before any spread maths."""
    q = f"""
    WITH ticks AS (
        SELECT
            CAST(time AS TIMESTAMP)                AS ts,
            bid, ask,
            (bid + ask) / 2.0                      AS mid,
            (ask - bid) / ((ask + bid) / 2.0) * 1e4 AS spread_bps
        FROM read_parquet('{path}')
        WHERE ask > bid            -- drop locked/crossed (phantom zero-cost fills)
          AND bid > 0 AND ask > 0
    )
    SELECT
        time_bucket(INTERVAL '{bar}', ts) AS ts,
        first(mid ORDER BY ts)  AS mid_open,
        max(mid)                AS mid_high,
        min(mid)                AS mid_low,
        last(mid ORDER BY ts)   AS mid_close,
        median(spread_bps)      AS spread_bps,   -- robust executable-spread proxy
        count(*)                AS n_ticks
    FROM ticks
    GROUP BY 1
    ORDER BY 1
    """
    return con.execute(q).df()


def scan_data_range(data_dir: str) -> dict:
    """Summarise tick parquet coverage from SYMBOL_YYYY_MM_DD.parquet filenames."""
    files = glob.glob(os.path.join(data_dir, "*.parquet"))
    dates: set[str] = set()
    symbols: set[str] = set()
    for path in files:
        base = os.path.basename(path)
        m = re.match(r"([A-Za-z]+)_(\d{4})_(\d{2})_(\d{2})\.parquet", base)
        if not m:
            continue
        symbols.add(m.group(1))
        dates.add(f"{m.group(2)}-{m.group(3)}-{m.group(4)}")
    if not dates:
        return {"files": len(files), "symbols": 0, "days": 0,
                "start": None, "end": None}
    return {
        "files": len(files),
        "symbols": len(symbols),
        "days": len(dates),
        "start": min(dates),
        "end": max(dates),
    }


def build_panel(data_dir: str, out_dir: str,
                aliases: dict[str, str] | None = None,
                bar: str = "15 minutes") -> dict[str, str]:
    """Resample every *.parquet in data_dir, concatenating per-symbol across days.

    Files are assumed named like SYMBOL_YYYY_MM_DD.parquet. Returns
    {canonical_symbol: output_parquet_path}.
    """
    aliases = aliases or DEFAULT_ALIASES
    os.makedirs(out_dir, exist_ok=True)
    con = duckdb.connect()

    files = sorted(glob.glob(os.path.join(data_dir, "*.parquet")))
    by_symbol: dict[str, list[pd.DataFrame]] = {}
    for path in files:
        base = os.path.basename(path)
        m = re.match(r"([A-Za-z]+)_", base)
        raw_sym = m.group(1) if m else base.split(".")[0]
        sym = _canon(raw_sym, aliases)
        df = resample_file(path, con, bar=bar)
        df["symbol"] = sym
        by_symbol.setdefault(sym, []).append(df)

    outputs: dict[str, str] = {}
    for sym, parts in by_symbol.items():
        full = pd.concat(parts).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        out_path = os.path.join(out_dir, f"{sym}_15m.parquet")
        full.to_parquet(out_path, index=False)
        outputs[sym] = out_path
    con.close()
    return outputs


def load_aligned(panel_paths: dict[str, str]):
    """Load per-symbol 15-min files and align onto one common 15-min grid.

    Returns (mids, spreads_bps): two DataFrames indexed by ts, columns=symbols.
    """
    mids, spr = {}, {}
    for sym, path in panel_paths.items():
        df = pd.read_parquet(path).set_index("ts")
        mids[sym] = df["mid_close"]
        spr[sym] = df["spread_bps"]
    mids = pd.DataFrame(mids).sort_index()
    spreads = pd.DataFrame(spr).sort_index()
    # union grid; a missing bar (gap / metals break) stays NaN -> engine skips it.
    grid = mids.index.union(spreads.index)
    return mids.reindex(grid), spreads.reindex(grid)
