"""MoMQ live system configuration.

Reads from .env / environment variables via Pydantic BaseSettings.
Module-level constants (ROUND_SCHEDULE, SYMBOL_MAP, CONTRACT_SIZES, ALL_SYMBOLS)
are defined below the Settings class.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("live/.env", ".env"),   # check live/.env first, then repo root .env
        env_file_encoding="utf-8",
        extra="ignore",
    )

    MT5_LOGIN: int = 0
    MT5_PASSWORD: SecretStr = SecretStr("")
    MT5_SERVER: str = "CompetitionBroker-Live"
    MT5_TERMINAL_PATH: str = r"C:\Program Files\MetaTrader 5\terminal64.exe"

    BRIDGE_HOST: str = "127.0.0.1"
    BRIDGE_PORT: int = 8765

    # Safety gate — must be explicitly true to send real orders
    ENTRY_ENABLED: bool = False

    KILL_SWITCH_FILE: Path = Path("live_state/kill.flag")
    STRATEGY_MODE_FILE: Path = Path("live_state/strategy.mode")
    STATE_DIR: Path = Path("live_state")

    # BTC ping-pong scalp (separate fast loop — see live/brain/btc_scalp_loop.py)
    # TP/SL auto-scale from player #758 medians per lot unless SCALP_TP_USD / SCALP_SL_USD > 0
    SCALP_SYMBOL: str = "BTCUSD"
    SCALP_LOT_BTC: float = 0.1
    SCALP_TP_USD: float = 0.0
    SCALP_SL_USD: float = 0.0
    SCALP_TP_USD_PER_LOT: float = 3.67
    SCALP_SL_USD_PER_LOT: float = 1.585
    SCALP_MAX_HOLD_S: float = 4.5
    SCALP_MAX_SPREAD_BPS: float = 2.0
    SCALP_POLL_S: float = 1.0
    SCALP_MIN_ORDER_INTERVAL_S: float = 1.0
    SCALP_FLIP_ON_CLOSE: bool = True
    SCALP_MOMENTUM_AFTER_SL: bool = True
    SCALP_MOMENTUM_LOOKBACK_S: float = 2.0
    SCALP_PAUSE_AFTER_SL_STREAK: int = 5
    SCALP_PAUSE_SECONDS: float = 15.0
    # HFT entry (research/player758/hft_search — mom_micro 63.8% WR on 7d backtest)
    SCALP_ENTRY_MODE: str = "flip"  # flip | mom_micro | cartea | momentum | mom_multi | microprice
    SCALP_MOM_FAST_S: float = 2.0
    SCALP_MOM_SLOW_S: float = 8.0
    SCALP_MICRO_THRESH: float = 0.60
    SCALP_MIN_RANGE_USD: float = 0.30
    SCALP_MICRO_WINDOW_S: float = 2.0
    SCALP_ADVERSE_BUFFER_USD: float = 1.0

    HISTORY_BARS: int = 300

    # Logfire token — omit to log to stderr only
    LOGFIRE_TOKEN: str = ""

    # Fixed for all round sizing per rules §12.1
    INIT_EQUITY: float = 1_000_000.0


# Module-level constant — mirrors Settings.INIT_EQUITY; used as default arg in risk.py
INIT_EQUITY: float = 1_000_000.0

# ---------------------------------------------------------------------------
# Round schedule
# All times are UTC (BST = UTC+1, so 22:00 BST = 21:00 UTC)
# ---------------------------------------------------------------------------
def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


ROUND_SCHEDULE: list[tuple[str, datetime, datetime]] = [
    # (round_kind, t0_utc, t1_utc)
    # Stage 3 (R3) opened 10:00 BST = 09:00 UTC on 23 Jun per organiser announcement.
    ("r1",     _utc(2026, 6, 21, 21, 0),  _utc(2026, 6, 22, 21, 0)),
    ("r2",     _utc(2026, 6, 22, 21, 0),  _utc(2026, 6, 23,  9, 0)),
    ("r3",     _utc(2026, 6, 23,  9, 0),  _utc(2026, 6, 24, 21, 0)),
    ("finals", _utc(2026, 6, 24, 21, 0),  _utc(2026, 6, 26, 21, 0)),
]

# Stage 3 / Finals: platform requires >30 valid executed orders or DD/Sharpe uncalculated.
MIN_ROUND_TRADES: int = 31
MIN_OBS_FOR_FULL_SHARPE: int = 8

# ---------------------------------------------------------------------------
# Symbol map: MT5 symbol → canonical name
# QUESTION: Verify exact symbol names on the competition server.
# Some brokers suffix symbols with .r, .m, .pro etc.
# e.g. "EURUSD" might be "EURUSD.r" or "EURUSDm" on competition MT5.
# ---------------------------------------------------------------------------
SYMBOL_MAP: dict[str, str] = {
    # Forex (COC sleeve)
    "EURUSD": "EURUSD",
    "GBPUSD": "GBPUSD",
    "USDCAD": "USDCAD",
    "USDJPY": "USDJPY",
    "AUDUSD": "AUDUSD",
    "USDCHF": "USDCHF",
    "EURGBP": "EURGBP",
    "EURCHF": "EURCHF",
    # Crypto (MTF sleeve)
    # QUESTION: Verify crypto symbol names on competition MT5.
    "BTCUSD": "BTCUSD",
    "ETHUSD": "ETHUSD",
    "SOLUSD": "SOLUSD",
    "XRPUSD": "XRPUSD",
    # QUESTION: Verify BAR token symbol name — may not exist on competition MT5.
    # Possible alternatives: BARUSD, BAR/USD, BARETH, BARUSDT
    "BARUSD": "BARUSD",
    # CSS spread pairs
    "XAUUSD": "XAUUSD",
    "XAGUSD": "XAGUSD",
}

# ---------------------------------------------------------------------------
# Contract sizes: notional per 1 standard lot in base currency
# ---------------------------------------------------------------------------
CONTRACT_SIZES: dict[str, float] = {
    # Forex: 100,000 units of base currency (standard)
    "EURUSD": 100_000,
    "GBPUSD": 100_000,
    "USDCAD": 100_000,
    "USDJPY": 100_000,
    "AUDUSD": 100_000,
    "USDCHF": 100_000,
    "EURGBP": 100_000,
    "EURCHF": 100_000,
    # Metals
    "XAUUSD": 100,      # 100 troy oz per lot
    "XAGUSD": 5_000,    # 5,000 troy oz per lot
    # Crypto: QUESTION — verify contract sizes on competition MT5.
    # Most MetaTrader crypto CFDs use 1 coin per lot, but some use 0.1 or 10.
    # Do NOT go live without confirming these with the competition admin.
    "BTCUSD": 1,        # QUESTION: 1 BTC per lot? Verify.
    "ETHUSD": 1,        # QUESTION: 1 ETH per lot? Verify.
    "SOLUSD": 1,        # QUESTION: 1 SOL per lot? Verify.
    "XRPUSD": 1,        # QUESTION: 1 XRP per lot? Verify.
    "BARUSD": 1,        # QUESTION: 1 BAR per lot? Verify. Symbol may not exist.
}

# ---------------------------------------------------------------------------
# All competition symbols (union of all sleeves)
# ---------------------------------------------------------------------------
# Finals MTF: BTC + metals (no alt-crypto — broker volume caps clip ETH/XRP etc.)
FINALS_MTF_SYMBOLS: list[str] = ["BTCUSD", "XAUUSD", "XAGUSD"]

COC_SYMBOLS: list[str] = [
    "EURUSD", "GBPUSD", "USDCAD", "USDJPY", "AUDUSD", "USDCHF", "EURGBP", "EURCHF",
]
MTF_SYMBOLS: list[str] = ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BARUSD", "XAUUSD", "XAGUSD"]
CSS_SYMBOLS: list[str] = ["EURUSD", "USDCHF", "AUDUSD", "USDCAD"]

ALL_SYMBOLS: list[str] = list(
    dict.fromkeys(COC_SYMBOLS + MTF_SYMBOLS + CSS_SYMBOLS)
)
