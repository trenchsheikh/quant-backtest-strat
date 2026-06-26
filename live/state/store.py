"""State persistence for the MoMQ live system.

Uses atomic file writes (tmp → rename) with a FileLock to prevent
corruption from concurrent access or crashes mid-write.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock
from pydantic import BaseModel, Field


class LivePosition(BaseModel):
    ticket: int
    symbol: str
    side: int           # +1 long, -1 short
    lots: float
    notional_usd: float
    entry_price: float
    entry_ts: datetime
    sleeve: str         # coc / mtf / css / unknown
    pair_id: str = ""   # CSS spread pair identifier e.g. "EURUSD_USDCHF"
    sl_price: float = 0.0
    tp_price: float = 0.0
    bars_held: int = 0
    signal_info: str = ""
    entry_z: float = 0.0   # CSS: z-score at entry (for z-stop)
    peak_mtm_pct: float = 0.0  # MTF: peak price-based MTM for trailing stop


class CocReentryItem(BaseModel):
    reentry_bar: int
    side: int
    notional_usd: float
    signal_info: str = ""


class LiveState(BaseModel):
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    round_kind: str | None = None
    bar_i: int = -1
    round_t0: datetime | None = None
    positions: list[LivePosition] = []
    realized_pnl: float = 0.0
    equity_samples: list[float] = []
    kill_switch: bool = False
    entry_enabled: bool = False
    coc_deployed: bool = False   # True once any COC leg opened this round
    coc_ever_this_round: bool = False  # True once COC deployed (unlocks full CSS after close)
    coc_reentry_queue: dict[str, CocReentryItem] = {}
    mtf_cooldown: dict[str, int] = {}
    css_pair_cooldown: dict[str, int] = Field(default_factory=dict)
    mtf_side_block: dict[str, int] = Field(default_factory=dict)
    coc_reentry_used: set[str] = Field(default_factory=set)
    # Risk Discipline — rules.md §13 (persisted across restarts)
    rd_score: float = 100.0
    rd_streaks: dict[str, int] = Field(default_factory=dict)
    rd_violations: list[dict[str, object]] = Field(default_factory=list)
    rd_compliance_flags: list[str] = Field(default_factory=list)
    rd_entries_blocked: bool = False
    rd_last_metrics: dict[str, float] = Field(default_factory=dict)
    # Per-round scoring window (Stage 3: DD + Sharpe reset; PnL carries via live equity)
    round_equity_anchor: float = 0.0
    round_trade_count: int = 0
    round_sharpe_eligible: bool = False

    model_config = {"arbitrary_types_allowed": True}

    def model_dump_json_safe(self) -> str:
        """Serialize to JSON, handling set fields that Pydantic stores as sets."""
        d = self.model_dump(mode="json")
        # Ensure set is serialized as list
        if isinstance(d.get("coc_reentry_used"), set):
            d["coc_reentry_used"] = list(d["coc_reentry_used"])
        return json.dumps(d, default=str)

    @classmethod
    def model_validate_from_file(cls, data: dict[str, Any]) -> "LiveState":
        """Load from dict, converting list back to set for coc_reentry_used."""
        if isinstance(data.get("coc_reentry_used"), list):
            data["coc_reentry_used"] = set(data["coc_reentry_used"])
        return cls.model_validate(data)


class StateStore:
    """Thread-safe, crash-safe state persistence."""

    STATE_FILENAME = "live_state.json"
    LOCK_FILENAME = "state.lock"

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self.state_dir / self.STATE_FILENAME
        self._lock_path = self.state_dir / self.LOCK_FILENAME
        self._lock = FileLock(str(self._lock_path), timeout=10)

    def load(self) -> LiveState:
        """Load state from disk. Returns a fresh LiveState if the file doesn't exist."""
        with self._lock:
            if not self._state_path.exists():
                return LiveState()
            try:
                raw = self._state_path.read_text(encoding="utf-8")
                data = json.loads(raw)
                return LiveState.model_validate_from_file(data)
            except Exception as exc:
                # Corrupt state — back it up and return fresh
                backup = self._state_path.with_suffix(".json.corrupt")
                try:
                    self._state_path.rename(backup)
                except OSError:
                    pass
                raise RuntimeError(
                    f"State file corrupt (backed up to {backup}): {exc}"
                ) from exc

    def save(self, state: LiveState) -> None:
        """Atomically save state: write to .tmp then rename over the real file."""
        state.updated_at = datetime.now(timezone.utc)
        with self._lock:
            # Write to a temp file in the same directory so rename is atomic
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.state_dir), prefix=".state_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(state.model_dump_json_safe())
                # Atomic replace (works on POSIX; on Windows this overwrites)
                os.replace(tmp_path, str(self._state_path))
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
