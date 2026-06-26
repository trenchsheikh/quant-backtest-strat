"""Rolling 15-minute bar buffer for the MoMQ live brain.

Maintains a deque of close prices and spread estimates for each symbol.
Used by signal generators identically to backtest DataFrames.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime

import pandas as pd


# Default spread estimates (bps) when no live tick spread is available
_DEFAULT_SPREAD_BPS: dict[str, float] = {
    # Forex: typically 1-2 bps on a competition account
    "EURUSD": 1.5,
    "GBPUSD": 1.5,
    "USDCAD": 1.5,
    "USDJPY": 1.5,
    "AUDUSD": 1.5,
    "USDCHF": 1.5,
    # Metals
    "XAUUSD": 5.0,
    "XAGUSD": 5.0,
    # Crypto: wider spreads
    "BTCUSD": 5.0,
    "ETHUSD": 5.0,
    "SOLUSD": 5.0,
    "XRPUSD": 5.0,
    "BARUSD": 5.0,
}
_FOREX_DEFAULT = 1.5
_METAL_DEFAULT = 5.0
_CRYPTO_DEFAULT = 5.0

_CRYPTO_NAMES = {"BTC", "ETH", "SOL", "XRP", "BAR"}
_METAL_NAMES = {"XAU", "XAG"}


def _default_spread(symbol: str) -> float:
    if _DEFAULT_SPREAD_BPS.get(symbol):
        return _DEFAULT_SPREAD_BPS[symbol]
    prefix = symbol[:3].upper()
    if prefix in _CRYPTO_NAMES:
        return _CRYPTO_DEFAULT
    if prefix in _METAL_NAMES:
        return _METAL_DEFAULT
    return _FOREX_DEFAULT


class BarBuffer:
    """Thread-unsafe rolling bar buffer (single-threaded brain loop only)."""

    def __init__(self, symbols: list[str], max_bars: int = 400) -> None:
        self.symbols = list(symbols)
        self.max_bars = max_bars
        # Deques of (ts, close) tuples
        self._ts: dict[str, deque[datetime]] = {s: deque(maxlen=max_bars) for s in symbols}
        self._mids: dict[str, deque[float]] = {s: deque(maxlen=max_bars) for s in symbols}
        self._spreads: dict[str, deque[float]] = {s: deque(maxlen=max_bars) for s in symbols}

    def seed(
        self,
        symbol: str,
        ts_col: list[datetime],
        close_col: list[float],
        spread_bps_col: list[float] | None = None,
    ) -> None:
        """Bulk-load historical data from MT5 rates endpoint.

        Args:
            symbol: symbol string (must be in self.symbols)
            ts_col: list of bar timestamps (UTC, tz-aware)
            close_col: list of close prices (same length as ts_col)
            spread_bps_col: optional spread estimates; falls back to defaults
        """
        if symbol not in self._mids:
            return
        n = len(close_col)
        for i in range(n):
            ts = ts_col[i]
            price = close_col[i]
            spread = spread_bps_col[i] if spread_bps_col else _default_spread(symbol)
            self._ts[symbol].append(ts)
            self._mids[symbol].append(price)
            self._spreads[symbol].append(spread)

    def push(
        self,
        ts: datetime,
        prices: dict[str, float],
        spreads: dict[str, float] | None = None,
    ) -> None:
        """Record a new 15m bar close for multiple symbols.

        If the timestamp matches the latest bar, update in place (avoids
        duplicate index labels when the seeded forming bar is finalized).
        """
        for symbol in self.symbols:
            if symbol not in prices:
                continue
            price = prices[symbol]
            if price <= 0:
                continue
            spread = (spreads or {}).get(symbol, _default_spread(symbol))
            ts_q = self._ts[symbol]
            if ts_q and ts_q[-1] == ts:
                self._mids[symbol][-1] = price
                self._spreads[symbol][-1] = spread
            else:
                ts_q.append(ts)
                self._mids[symbol].append(price)
                self._spreads[symbol].append(spread)

    def get_mids(self) -> pd.DataFrame:
        """Return DataFrame of close prices: index=bar position (0=oldest), columns=symbols.

        Returns a DataFrame aligned to the symbol with the most bars.
        Symbols with fewer bars get NaN-padded at the front.
        """
        max_len = max((len(self._mids[s]) for s in self.symbols), default=0)
        if max_len == 0:
            return pd.DataFrame(columns=self.symbols)

        data: dict[str, list[float | None]] = {}
        ts_index: list[datetime] | None = None

        for symbol in self.symbols:
            q = list(self._mids[symbol])
            padding = max_len - len(q)
            padded: list[float | None] = [None] * padding + q
            data[symbol] = padded
            # Use the longest symbol's timestamps as the index
            if ts_index is None or len(q) == max_len:
                ts_list = list(self._ts[symbol])
                ts_index = [None] * padding + ts_list  # type: ignore[assignment]

        df = pd.DataFrame(data, index=ts_index)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df

    def get_spreads(self) -> pd.DataFrame:
        """Return DataFrame of spread_bps aligned to get_mids() index."""
        mids_df = self.get_mids()
        if mids_df.empty:
            return pd.DataFrame(columns=self.symbols)

        data: dict[str, list[float]] = {}
        for symbol in self.symbols:
            q = list(self._spreads[symbol])
            padding = len(mids_df) - len(q)
            padded = [_default_spread(symbol)] * max(padding, 0) + q
            data[symbol] = padded[-len(mids_df):]

        return pd.DataFrame(data, index=mids_df.index)

    def n_bars(self, symbol: str) -> int:
        """Return number of bars currently stored for a symbol."""
        return len(self._mids.get(symbol, []))

    def latest_ts(self, symbol: str) -> datetime | None:
        """Return the timestamp of the most recent bar for a symbol."""
        q = self._ts.get(symbol)
        if not q:
            return None
        return q[-1]
