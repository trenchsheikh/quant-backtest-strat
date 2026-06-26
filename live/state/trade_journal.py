"""Append-only trade journal persisted to JSON.

Records every open and close event from the brain so competition fills
can be audited offline without digging through MT5 or log files.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from filelock import FileLock
from pydantic import BaseModel, Field


class TradeEvent(BaseModel):
    """Single trade lifecycle event (open or close)."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event: Literal["open", "close"]
    ticket: int
    symbol: str
    side: int
    lots: float
    price: float
    notional_usd: float = 0.0
    sleeve: str = ""
    pair_id: str = ""
    signal_info: str = ""
    round_kind: str | None = None
    bar_i: int = -1
    exit_reason: str = ""
    pnl: float | None = None
    bars_held: int = 0
    entry_price: float | None = None
    paper: bool = False


class TradeJournalFile(BaseModel):
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    events: list[TradeEvent] = Field(default_factory=list)


class TradeJournal:
    """Thread-safe JSON trade log at ``{state_dir}/trades.json``."""

    FILENAME = "trades.json"
    LOCK_FILENAME = "trades.lock"

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.state_dir / self.FILENAME
        self._lock = FileLock(str(self.state_dir / self.LOCK_FILENAME), timeout=10)

    def load(self) -> TradeJournalFile:
        with self._lock:
            if not self._path.exists():
                return TradeJournalFile()
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return TradeJournalFile.model_validate(data)

    def _save(self, journal: TradeJournalFile) -> None:
        journal.updated_at = datetime.now(timezone.utc)
        with self._lock:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.state_dir), prefix=".trades_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(journal.model_dump_json(indent=2))
                os.replace(tmp_path, str(self._path))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

    def append(self, event: TradeEvent) -> None:
        journal = self.load()
        journal.events.append(event)
        self._save(journal)

    def record_open(
        self,
        *,
        ticket: int,
        symbol: str,
        side: int,
        lots: float,
        price: float,
        notional_usd: float,
        sleeve: str,
        pair_id: str = "",
        signal_info: str = "",
        round_kind: str | None = None,
        bar_i: int = -1,
        paper: bool = False,
    ) -> None:
        self.append(TradeEvent(
            event="open",
            ticket=ticket,
            symbol=symbol,
            side=side,
            lots=lots,
            price=price,
            notional_usd=notional_usd,
            sleeve=sleeve,
            pair_id=pair_id,
            signal_info=signal_info,
            round_kind=round_kind,
            bar_i=bar_i,
            paper=paper,
        ))

    def record_close(
        self,
        *,
        ticket: int,
        symbol: str,
        side: int,
        lots: float,
        price: float,
        entry_price: float,
        sleeve: str = "",
        pair_id: str = "",
        signal_info: str = "",
        round_kind: str | None = None,
        bar_i: int = -1,
        exit_reason: str = "",
        pnl: float | None = None,
        bars_held: int = 0,
        paper: bool = False,
    ) -> None:
        self.append(TradeEvent(
            event="close",
            ticket=ticket,
            symbol=symbol,
            side=side,
            lots=lots,
            price=price,
            entry_price=entry_price,
            notional_usd=lots * price,
            sleeve=sleeve,
            pair_id=pair_id,
            signal_info=signal_info,
            round_kind=round_kind,
            bar_i=bar_i,
            exit_reason=exit_reason,
            pnl=pnl,
            bars_held=bars_held,
            paper=paper,
        ))

    def n_events(self) -> int:
        return len(self.load().events)

    def count_executed_since(self, since: datetime) -> int:
        """Unique filled open/close events on or after ``since`` (UTC)."""
        from live.brain.round_metrics import count_executed_orders_since

        journal = self.load()
        return count_executed_orders_since(journal.events, since, exclude_reconcile=True)
