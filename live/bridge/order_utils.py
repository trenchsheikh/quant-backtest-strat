"""MT5 order helpers — volume normalization and slippage/deviation."""
from __future__ import annotations

import math
import time
from typing import Any

# Retcodes worth retrying with a fresh tick (bar-open requotes, etc.)
RETRY_RETCODES: frozenset[int] = frozenset({
    10004,  # TRADE_RETCODE_REQUOTE
    10015,  # TRADE_RETCODE_INVALID_PRICE
    10020,  # TRADE_RETCODE_PRICE_CHANGED
    10021,  # TRADE_RETCODE_PRICE_OFF
})

ORDER_MAX_ATTEMPTS = 4
ORDER_RETRY_SLEEP_S = 0.6


def volume_decimals(step: float) -> int:
    if step <= 0:
        return 2
    text = f"{step:.8f}".rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".")[1])


def normalize_volume(lots: float, info: Any | None) -> float:
    """Clip and round volume to broker min / max / step."""
    if lots <= 0:
        return 0.0
    if info is None:
        return round(lots, 2)

    vmin = float(getattr(info, "volume_min", 0.0) or 0.01)
    vmax = float(getattr(info, "volume_max", lots) or lots)
    step = float(getattr(info, "volume_step", 0.0) or 0.01)
    if step <= 0:
        step = 0.01

    n_steps = round(lots / step)
    vol = n_steps * step
    vol = max(vmin, min(vmax, vol))
    return round(vol, volume_decimals(step))


def order_deviation_points(symbol: str, info: Any | None, price: float) -> int:
    """Slippage allowance in MT5 points (not pips)."""
    if info is None:
        return 100

    point = float(getattr(info, "point", 0.0) or 0.0)
    if point <= 0:
        point = 0.00001

    sym = symbol.upper()
    # ~20 bps crypto, ~10 bps metals, ~2 pips forex
    if any(tok in sym for tok in ("BTC", "ETH", "SOL", "XRP", "BAR")):
        bps = 0.0020
        floor_pts = 2_000
    elif any(tok in sym for tok in ("XAU", "XAG")):
        bps = 0.0010
        floor_pts = 300
    else:
        bps = 0.0002
        floor_pts = 20

    ref = price if price > 0 else float(getattr(info, "ask", 0.0) or 1.0)
    pts = int(math.ceil((ref * bps) / point))
    return max(floor_pts, pts)


def is_retryable_retcode(retcode: int) -> bool:
    return int(retcode) in RETRY_RETCODES


def retry_sleep(attempt: int) -> None:
    time.sleep(ORDER_RETRY_SLEEP_S * (attempt + 1))
