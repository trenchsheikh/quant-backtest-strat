"""Broker symbol specifications for volume and notional sizing."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from live.bridge.order_utils import normalize_volume
from live.config import CONTRACT_SIZES


@dataclass(frozen=True)
class SymbolSpec:
    symbol: str
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float

    @classmethod
    def from_mt5_info(cls, symbol: str, info: Any | None) -> "SymbolSpec":
        sym = symbol.upper()
        fallback = CONTRACT_SIZES.get(sym, 100_000)
        if info is None:
            return cls(
                symbol=sym,
                contract_size=float(fallback),
                volume_min=0.01,
                volume_max=1_000_000.0,
                volume_step=0.01,
            )
        contract = float(getattr(info, "trade_contract_size", 0.0) or fallback)
        if contract <= 0:
            contract = float(fallback)
        vmin = float(getattr(info, "volume_min", 0.0) or 0.01)
        vmax = float(getattr(info, "volume_max", 0.0) or 1_000_000.0)
        step = float(getattr(info, "volume_step", 0.0) or 0.01)
        return cls(
            symbol=sym,
            contract_size=contract,
            volume_min=vmin,
            volume_max=vmax,
            volume_step=step,
        )

    def notional_to_lots(self, notional_usd: float, price: float) -> float:
        if price <= 0 or notional_usd <= 0:
            return 0.0
        raw = notional_usd / (self.contract_size * price)
        capped = min(raw, self.volume_max) if self.volume_max > 0 else raw
        vol = normalize_volume(capped, self)
        return vol if vol >= self.volume_min else 0.0

    def lots_to_notional(self, lots: float, price: float) -> float:
        if lots <= 0 or price <= 0:
            return 0.0
        return abs(lots * self.contract_size * price)

    def volume_was_capped(self, requested_lots: float) -> bool:
        if requested_lots <= 0:
            return False
        normalized = normalize_volume(requested_lots, self)
        return normalized + 1e-9 < requested_lots
