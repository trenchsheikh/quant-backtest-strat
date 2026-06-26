"""Live risk engine for MoMQ competition — rules.md §2, §12, §13.

Enforces:
  - Strategy gross leverage cap (25x)
  - RD pre-emptive blocks with buffers below penalty thresholds:
      leverage ≥27x, margin ≥85%, single-instrument ≥85%, net-dir ≥92%
  - Leverage = gross notional / live equity (rules §13.2)
  - Margin usage = used margin / equity (rules §13.1)
"""
from __future__ import annotations

from datetime import datetime, timezone

import logfire

from live.brain.rd_monitor import (
    LEVERAGE_ENTRY_BLOCK,
    MARGIN_ENTRY_BLOCK,
    NET_DIR_ENTRY_BLOCK,
    SINGLE_INSTR_ENTRY_BLOCK,
    STOP_OUT_MARGIN_LEVEL,
    STOP_OUT_WARN_MARGIN_LEVEL,
    normalize_margin_level,
)
from live.config import CONTRACT_SIZES, INIT_EQUITY
from live.state.store import LivePosition

TOTAL_CAP = 25.0            # Strategy gross leverage cap (below 28x RD penalty)
LEVERAGE_WARN = 24.0
LEVERAGE_HARD = 28.0        # rules §13.2: >28x for ≥30min → -20 pts
LEVERAGE_EXTREME = 29.0     # rules §13.2: >29x for ≥15min → -30 pts
SINGLE_INSTR_WARN = 0.80
SINGLE_INSTR_HARD = 0.90    # rules §13.3: >90% for ≥30min → -10 pts
NET_DIR_WARN = 0.90
NET_DIR_HARD = 0.95         # rules §13.3: >95% for ≥30min → -10 pts
MARGIN_WARN = 0.85
MARGIN_HARD = 0.90          # rules §13.1: >90% for ≥30min → -20 pts
MARGIN_EXTREME = 0.95       # rules §13.1: >95% for ≥15min → -30 pts


class LiveRiskEngine:
    def __init__(self, init_equity: float = INIT_EQUITY) -> None:
        self.init_equity = init_equity

    def notional_to_lots(self, symbol: str, notional_usd: float, price: float) -> float:
        if price <= 0 or notional_usd <= 0:
            return 0.0
        contract_size = CONTRACT_SIZES.get(symbol, 100_000)
        if contract_size <= 0:
            return 0.0
        raw_lots = notional_usd / (contract_size * price)
        rounded = round(raw_lots, 2)
        return max(rounded, 0.01) if rounded >= 0.01 else 0.0

    def lots_to_notional(self, symbol: str, lots: float, price: float) -> float:
        contract_size = CONTRACT_SIZES.get(symbol, 100_000)
        return lots * contract_size * price

    def gross_notional(self, positions: list[LivePosition]) -> float:
        return sum(abs(p.notional_usd) for p in positions)

    def effective_equity(self, live_equity: float) -> float:
        """Rules §12: sizing uses live equity; floor at init for safety."""
        return max(self.init_equity, live_equity) if live_equity > 0 else self.init_equity

    def leverage(self, positions: list[LivePosition], live_equity: float) -> float:
        """rules §13.2: Gross Notional / Equity."""
        eq = self.effective_equity(live_equity)
        return self.gross_notional(positions) / eq if eq > 0 else 0.0

    def can_add(
        self,
        positions: list[LivePosition],
        add_notional: float,
        live_equity: float,
    ) -> bool:
        eq = self.effective_equity(live_equity)
        return self.gross_notional(positions) + add_notional <= TOTAL_CAP * eq

    def single_instrument_pct(self, positions: list[LivePosition]) -> float:
        gross_by: dict[str, float] = {}
        for p in positions:
            gross_by[p.symbol] = gross_by.get(p.symbol, 0.0) + abs(p.notional_usd)
        tot = sum(gross_by.values())
        if tot <= 0:
            return 0.0
        return max(gross_by.values()) / tot

    def net_directional_pct(self, positions: list[LivePosition]) -> float:
        net_by: dict[str, float] = {}
        gross_by: dict[str, float] = {}
        for p in positions:
            net_by[p.symbol] = net_by.get(p.symbol, 0.0) + p.side * abs(p.notional_usd)
            gross_by[p.symbol] = gross_by.get(p.symbol, 0.0) + abs(p.notional_usd)
        gross_total = sum(gross_by.values())
        if gross_total <= 0:
            return 0.0
        return abs(sum(net_by.values())) / gross_total

    def margin_usage(self, used_margin: float, live_equity: float) -> float:
        """rules §13.1: Used Margin / Equity."""
        eq = self.effective_equity(live_equity)
        if eq <= 0 or used_margin < 0:
            return 0.0
        return used_margin / eq

    def portfolio_metrics(
        self,
        positions: list[LivePosition],
        live_equity: float,
        used_margin: float = 0.0,
    ) -> dict[str, float]:
        lev = self.leverage(positions, live_equity)
        return {
            "leverage": lev,
            "single_instrument": self.single_instrument_pct(positions),
            "net_directional": self.net_directional_pct(positions),
            "margin_usage": self.margin_usage(used_margin, live_equity),
            "gross_notional": self.gross_notional(positions),
        }

    def _project_positions(
        self,
        positions: list[LivePosition],
        legs: list[tuple[str, int, float]],
    ) -> list[LivePosition]:
        """Simulate positions after adding legs (symbol, side, notional_usd)."""
        projected = list(positions)
        for sym, side, notional in legs:
            projected.append(LivePosition(
                ticket=-1,
                symbol=sym,
                side=side,
                lots=0.0,
                notional_usd=notional,
                entry_price=1.0,
                entry_ts=positions[0].entry_ts if positions else datetime.now(timezone.utc),
                sleeve="projected",
            ))
        return projected

    def check_rd_warnings(
        self,
        positions: list[LivePosition],
        live_equity: float,
        used_margin: float = 0.0,
        margin_level: float | None = None,
    ) -> list[str]:
        warnings: list[str] = []
        if not positions and used_margin <= 0:
            return warnings

        m = self.portfolio_metrics(positions, live_equity, used_margin)
        lev, si, nd, margin = m["leverage"], m["single_instrument"], m["net_directional"], m["margin_usage"]

        if lev >= LEVERAGE_EXTREME:
            warnings.append(f"LEVERAGE_EXTREME: {lev:.2f}x >= {LEVERAGE_EXTREME}x (-30 pts if ≥15min)")
        elif lev >= LEVERAGE_HARD:
            warnings.append(f"LEVERAGE_HARD: {lev:.2f}x >= {LEVERAGE_HARD}x (-20 pts if ≥30min)")
        elif lev >= LEVERAGE_WARN:
            warnings.append(f"LEVERAGE_WARN: {lev:.2f}x (approaching 28x RD limit)")

        if si >= SINGLE_INSTR_HARD:
            warnings.append(f"SINGLE_INSTR_HARD: {si*100:.1f}% >= 90% (-10 pts if ≥30min)")
        elif si >= SINGLE_INSTR_WARN:
            warnings.append(f"SINGLE_INSTR_WARN: {si*100:.1f}% >= 80%")

        if nd >= NET_DIR_HARD:
            warnings.append(f"NET_DIR_HARD: {nd*100:.1f}% >= 95% (-10 pts if ≥30min)")
        elif nd >= NET_DIR_WARN:
            warnings.append(f"NET_DIR_WARN: {nd*100:.1f}% >= 90%")

        if margin >= MARGIN_EXTREME:
            warnings.append(f"MARGIN_EXTREME: {margin*100:.1f}% >= 95% (-30 pts if ≥15min)")
        elif margin >= MARGIN_HARD:
            warnings.append(f"MARGIN_HARD: {margin*100:.1f}% >= 90% (-20 pts if ≥30min)")
        elif margin >= MARGIN_WARN:
            warnings.append(f"MARGIN_WARN: {margin*100:.1f}% >= 85%")

        if margin_level is not None:
            ml = normalize_margin_level(margin_level, used_margin)
            if ml is not None:
                if ml <= STOP_OUT_MARGIN_LEVEL:
                    warnings.append(
                        f"STOP_OUT_CRITICAL: margin_level={ml:.1f}% "
                        f"(rules §2: forced liquidation at 30%)"
                    )
                elif ml <= STOP_OUT_WARN_MARGIN_LEVEL:
                    warnings.append(
                        f"STOP_OUT_WARN: margin_level={ml:.1f}% (approaching 30% stop-out)"
                    )

        if warnings:
            logfire.warn(
                "risk.rd_warnings",
                leverage=round(lev, 3),
                single_instr_pct=round(si, 3),
                net_dir_pct=round(nd, 3),
                margin_pct=round(margin, 3),
                margin_level=margin_level,
                warnings=warnings,
            )
        return warnings

    def hard_block(
        self,
        positions: list[LivePosition],
        legs: list[tuple[str, int, float]],
        live_equity: float,
        used_margin: float = 0.0,
        margin_level: float | None = None,
        entries_blocked_rd: bool = False,
        allow_incomplete_pair: bool = False,
        allow_incomplete_basket: bool = False,
        allow_directional_sleeve: bool = False,
    ) -> str | None:
        """Pre-trade compliance gate — rules.md §13 with safety buffers."""
        if entries_blocked_rd:
            return "RD_ENTRIES_BLOCKED: sustained breach streak — no new entries this bar"

        ml = normalize_margin_level(margin_level, used_margin)
        if ml is not None and ml <= STOP_OUT_MARGIN_LEVEL:
            return f"STOP_OUT: margin_level={ml:.1f}% — no new entries"

        add_total = sum(n for _, _, n in legs)
        eq = self.effective_equity(live_equity)
        if not self.can_add(positions, add_total, live_equity):
            cur = self.leverage(positions, live_equity)
            new_lev = (self.gross_notional(positions) + add_total) / eq
            return f"LEVERAGE_CAP: {cur:.2f}x → {new_lev:.2f}x exceeds {TOTAL_CAP}x strategy cap"

        projected = self._project_positions(positions, legs)
        pm = self.portfolio_metrics(projected, live_equity, used_margin)
        skip_concentration = allow_incomplete_pair or allow_incomplete_basket
        # Mid CSS/COC pair deploy: leg 1 alone is 100% net-dir until leg 2 fills.
        # MTF batch net-dir stays enforced (basket uses allow_incomplete_basket only).
        skip_net_dir = allow_incomplete_pair or allow_directional_sleeve

        if pm["leverage"] >= LEVERAGE_ENTRY_BLOCK:
            return (
                f"RD_LEVERAGE: projected {pm['leverage']:.2f}x >= {LEVERAGE_ENTRY_BLOCK}x "
                f"(buffer below 28x penalty)"
            )
        if not skip_concentration and pm["single_instrument"] >= SINGLE_INSTR_ENTRY_BLOCK:
            return (
                f"RD_CONCENTRATION: single-instrument {pm['single_instrument']*100:.1f}% "
                f">= {SINGLE_INSTR_ENTRY_BLOCK*100:.0f}% (buffer below 90% penalty)"
            )
        if not skip_net_dir and pm["net_directional"] >= NET_DIR_ENTRY_BLOCK:
            return (
                f"RD_NET_DIR: net directional {pm['net_directional']*100:.1f}% "
                f">= {NET_DIR_ENTRY_BLOCK*100:.0f}% (buffer below 95% penalty)"
            )
        if pm["margin_usage"] >= MARGIN_ENTRY_BLOCK:
            return (
                f"RD_MARGIN: projected margin usage {pm['margin_usage']*100:.1f}% "
                f">= {MARGIN_ENTRY_BLOCK*100:.0f}% (buffer below 90% penalty)"
            )

        # Block standalone single-symbol book unless mid multi-leg deploy (CSS/COC/MTF)
        symbols_after = {p.symbol for p in projected}
        if (
            len(symbols_after) == 1
            and len(legs) == 1
            and not allow_incomplete_pair
            and not allow_incomplete_basket
        ):
            return "RD_CONCENTRATION: cannot open sole single-symbol directional position"

        return None
