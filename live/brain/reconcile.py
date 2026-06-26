"""Position reconciliation between saved state and live MT5 state.

On startup (or after any crash), we compare what we think is open (from the
JSON state file) with what MT5 actually has. MT5 is ground truth.

Outcomes per position:
  - Match: keep saved metadata (sleeve, signal_info, pair_id, etc.)
  - In state but gone from MT5: position was closed externally → remove from state
  - In MT5 but not in state: orphan → add with sleeve='unknown' (may be manual trades)
  - Size mismatch: trust MT5 lots, keep saved metadata
"""
from __future__ import annotations

from datetime import datetime, timezone

import logfire
from pydantic import BaseModel

from live.config import CONTRACT_SIZES
from live.state.store import LivePosition


def _position_notional_usd(symbol: str, lots: float, price: float) -> float:
    if lots <= 0 or price <= 0:
        return 0.0
    contract = CONTRACT_SIZES.get(symbol, 100_000)
    return abs(lots * contract * price)


class ReconcileResult(BaseModel):
    closed_in_mt5: list[int] = []                         # tickets in state, gone from MT5
    orphaned_in_mt5: list[int] = []                       # tickets in MT5, not in state
    size_mismatches: list[tuple[int, float, float]] = []  # (ticket, saved_lots, actual_lots)
    ok: bool = True


def reconcile(
    saved: list[LivePosition],
    live_mt5: list[dict],
    logger_ctx: dict | None = None,
) -> tuple[ReconcileResult, list[LivePosition]]:
    """Compare saved state positions to live MT5 positions.

    Returns (ReconcileResult, reconciled_positions).

    Trust MT5 as ground truth for what is open.
    Preserve saved metadata (sleeve, pair_id, signal_info, etc.) for matched positions.
    Orphan positions (in MT5 but not in state) get sleeve='unknown'.
    """
    ctx = logger_ctx or {}
    result = ReconcileResult()

    # Index saved positions by ticket
    saved_by_ticket: dict[int, LivePosition] = {p.ticket: p for p in saved}
    # Index MT5 positions by ticket
    mt5_by_ticket: dict[int, dict] = {p["ticket"]: p for p in live_mt5}

    reconciled: list[LivePosition] = []

    # 1. Check every saved position against MT5
    for ticket, saved_pos in saved_by_ticket.items():
        mt5_pos = mt5_by_ticket.get(ticket)

        if mt5_pos is None:
            # Position was closed externally (TP/SL hit, manual close, etc.)
            result.closed_in_mt5.append(ticket)
            logfire.info(
                "reconcile.position_closed_externally",
                ticket=ticket,
                symbol=saved_pos.symbol,
                sleeve=saved_pos.sleeve,
                **ctx,
            )
            continue

        # Refresh lots / notional from MT5 (manual hedges, partial fills)
        actual_lots = float(mt5_pos["lots"])
        price = float(
            mt5_pos.get("price_current", mt5_pos.get("price_open", saved_pos.entry_price))
        )
        actual_notional = _position_notional_usd(saved_pos.symbol, actual_lots, price)

        if abs(actual_lots - saved_pos.lots) > 1e-4:
            result.size_mismatches.append((ticket, saved_pos.lots, actual_lots))
            logfire.warn(
                "reconcile.size_mismatch",
                ticket=ticket,
                symbol=saved_pos.symbol,
                saved_lots=saved_pos.lots,
                actual_lots=actual_lots,
                **ctx,
            )
        reconciled.append(
            saved_pos.model_copy(
                update={"lots": actual_lots, "notional_usd": actual_notional}
            )
        )

    # 2. Find orphans — in MT5 but not in saved state
    for ticket, mt5_pos in mt5_by_ticket.items():
        if ticket not in saved_by_ticket:
            result.orphaned_in_mt5.append(ticket)
            logfire.warn(
                "reconcile.orphan_position",
                ticket=ticket,
                symbol=mt5_pos["symbol"],
                lots=mt5_pos["lots"],
                **ctx,
            )
            # Add as unknown sleeve so risk engine can account for it
            side = 1 if mt5_pos["type"] == 0 else -1  # MT5 type 0=buy, 1=sell
            price = mt5_pos.get("price_open", mt5_pos.get("price_current", 0.0))
            orphan = LivePosition(
                ticket=ticket,
                symbol=mt5_pos["symbol"],
                side=side,
                lots=mt5_pos["lots"],
                notional_usd=_position_notional_usd(
                    mt5_pos["symbol"], float(mt5_pos["lots"]), float(price)
                ),
                entry_price=price,
                entry_ts=mt5_pos.get("time", datetime.now(timezone.utc)),
                sleeve="unknown",
                signal_info="orphan:reconciled",
            )
            reconciled.append(orphan)

    # Determine overall health
    result.ok = (
        len(result.closed_in_mt5) == 0
        and len(result.orphaned_in_mt5) == 0
        and len(result.size_mismatches) == 0
    )

    if result.ok:
        logfire.info(
            "reconcile.clean",
            n_positions=len(reconciled),
            **ctx,
        )
    else:
        logfire.warn(
            "reconcile.discrepancies",
            closed_externally=len(result.closed_in_mt5),
            orphans=len(result.orphaned_in_mt5),
            size_mismatches=len(result.size_mismatches),
            **ctx,
        )

    return result, reconciled
