"""Active live strategy selector: 15m bar brain vs BTC scalp loop."""
from __future__ import annotations

from enum import Enum
from pathlib import Path

from live.config import Settings


class StrategyMode(str, Enum):
    BAR = "bar"
    SCALP = "scalp"


def strategy_mode_path(settings: Settings | None = None) -> Path:
    s = settings or Settings()
    return Path(s.STRATEGY_MODE_FILE)


def get_strategy_mode(settings: Settings | None = None) -> StrategyMode:
    path = strategy_mode_path(settings)
    if not path.exists():
        return StrategyMode.BAR
    raw = path.read_text(encoding="utf-8").strip().lower()
    if raw == StrategyMode.SCALP.value:
        return StrategyMode.SCALP
    return StrategyMode.BAR


def set_strategy_mode(mode: StrategyMode, settings: Settings | None = None) -> None:
    path = strategy_mode_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(mode.value + "\n", encoding="utf-8")
