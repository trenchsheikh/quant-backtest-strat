"""Tournament configuration — optimized for rules.md leaderboard scoring.

Final Score = 70% Return Rank + 15% Drawdown Rank + 10% Sharpe Rank + 5% Risk Discipline

Strategy — three sleeves, each targeting a different alpha source:
  Sleeve 1 (COC 18x): contrarian open carry on forex + gold (proven mean-reversion)
  Sleeve 2 (MTF 6x):  crypto EMA momentum (BTC/ETH/SOL trend within the round)
  Sleeve 3 (CSS 3x):  dollar-neutral correlation spread (Sharpe smoother, R1/R2 only)

Total max gross: 24x (safe margin below 28x RD penalty threshold).
Even at -10% equity drawdown the effective leverage is 24/0.9 = 26.7x — still clean.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum

COMPETITION_FOREX: list[str] = [
    "AUDUSD", "EURCHF", "EURGBP", "EURUSD", "GBPUSD",
    "USDCAD", "USDCHF", "USDJPY",
]

# COC ranking universe — all 8 competition forex pairs.
PRIMARY_SYMBOLS: list[str] = [
    "EURUSD", "GBPUSD", "USDCAD", "USDJPY",
    "AUDUSD", "USDCHF", "EURGBP", "EURCHF",
]

# Crypto universe — MTF momentum sleeve.
CRYPTO_SYMBOLS: list[str] = [
    "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BARUSD",
]

# Metals — same EMA momentum logic as crypto (trend follow, not COC mean-reversion).
METAL_SYMBOLS: list[str] = ["XAUUSD", "XAGUSD"]

# Combined momentum universe for MTF signal generator.
MOMENTUM_SYMBOLS: list[str] = CRYPTO_SYMBOLS + METAL_SYMBOLS

# Correlation-researched spread pairs per round type (dollar-neutral, 2 legs).
# Running BOTH pairs simultaneously in R1/R2 adds a second independent P&L stream →
# Sharpe improves via sqrt(N) diversification. Budget is split equally between pairs.
# R1: EURUSD/USDCHF (anti-correlated −0.85) + AUDUSD/USDCAD (commodity dollars)
# R2: same pairs, slightly lower budget per pair (0.7× scale applied in engine)
# R3: CSS disabled — COC flat; MTF carries R3
ROUND_CORR_SPREADS: dict[str, list[tuple[str, str]]] = {
    "round_1": [("EURUSD", "USDCHF"), ("AUDUSD", "USDCAD")],
    "round_2": [("AUDUSD", "USDCAD"), ("EURUSD", "USDCHF")],
    # R3: COC is flat; CSS is the primary alpha (consistent mean-reversion beats choppy MTF)
    "round_3": [("EURUSD", "USDCHF"), ("AUDUSD", "USDCAD")],
    # Finals: COC runs first 24h then banks; CSS provides steady Sharpe in second 24h
    "finals":  [("EURUSD", "USDCHF"), ("AUDUSD", "USDCAD")],
}


class RoundKind(Enum):
    R1 = "round_1"
    R2 = "round_2"
    R3 = "round_3"
    FINALS = "finals"


ROUND_OPEN_DOW: dict[RoundKind, int] = {
    RoundKind.R1: 6,
    RoundKind.R2: 0,
    RoundKind.R3: 1,
    RoundKind.FINALS: 2,
}


@dataclass
class SleeveBudget:
    # Each value is leverage-multiple of init_equity (max gross notional / equity).
    # COC at 22x is the core return driver (70% of scoring = return rank).
    # MTF at 6x stacks on top in live (crypto data absent from backtest).
    # total_cap=25x: COC(22)+CSS(3)=25x; COC(22)+MTF(6) would be 28 which is
    # blocked by can_add() — MTF only fires when some COC capital is freed up
    # OR on R3 (where COC=0x leaving full 25x available for MTF).
    coc: float = 22.0          # COC: 3 legs vol-parity @ ~7x each
    corr_spread: float = 3.0   # CSS: 1 spread pair, re-enabled
    mtf: float = 6.0           # MTF: 1 crypto position (EMA momentum)
    total_cap: float = 25.0    # hard cap; Portfolio.can_add() enforces this


@dataclass
class TournamentConfig:
    init_equity: float = 1_000_000.0

    # ---- COC (contrarian open carry) ----------------------------------------
    coc_pre_window: int = 48        # bars of pre-round history to rank (12h)
    coc_n_long: int = 3             # long the 3 weakest (√5 diversification vs old √3)
    coc_n_short: int = 2            # short the 2 strongest
    # Active exit management: bank gains early, re-ride the full move
    # COC is mean-reversion — no tight SL (lets the trade breathe before reverting).
    # TP fires when the first portion of the reversion is captured; re-enter to ride the rest.
    coc_tp_pct: float = 0.0020      # take-profit: +0.20% price move (more frequent banking)
    coc_sl_pct: float = 0.0          # 0 = disabled (see time-deterioration stop below)
    coc_reentry_cooldown: int = 1    # bars (15 min) to wait before re-entering after TP
    # Time-deterioration stop — disabled (0.0 = off).
    # Price/time stops are counterproductive for mean-reversion: the adverse excursion
    # comes BEFORE the reversion. Risk is managed via vol-parity sizing instead.
    coc_td_start_bar: int = 16
    coc_td_threshold: float = 0.0    # 0.0 = disabled

    # ---- CSS (correlation spread) -------------------------------------------
    spread_z_window: int = 32
    spread_z_entry: float = 1.15    # R2 comeback: fire CSS more often
    spread_z_entry_recovery: float = 1.6   # stricter entry when COC is not deployed (R1 catch-up mode)
    spread_z_exit: float = 0.10
    spread_z_stop: float = 0.8      # close if |z| diverges this far past entry |z| (loss cap)
    spread_max_hold: int = 16
    spread_start_bar: int = 4       # 1h after open — let COC settle
    css_recovery_scale: float = 0.5         # CSS budget multiplier when COC not deployed
    css_single_pair_no_coc: bool = False    # R2 comeback: run both spread pairs

    # ---- COC catch-up (late round start) ------------------------------------
    coc_catchup_enabled: bool = False  # disabled after R2 partial deploy; use MTF+CSS
    coc_catchup_max_bar: int = 88   # no catch-up in final 2h of a 24h round
    round_bars: int = 96            # 24h × 4 bars/h

    # ---- MTF (crypto momentum trend follow) ---------------------------------
    mtf_fast: int = 4               # fast EMA period (bars) = 1h lookback
    mtf_slow: int = 16              # slow EMA period = 4h lookback
    mtf_start_bar: int = 8          # first 2h: let COC establish, then trade
    mtf_max_hold: int = 24          # max hold = 6h (then exit regardless)
    mtf_stop_pct: float = 0.025     # stop loss: close if MTM PnL < -2.5% of equity (more room)
    mtf_min_signal: float = 0.0025  # R2 comeback: slightly looser trend filter
    mtf_reversal: float = 0.0012    # exit if EMA signal reverses past this (0.12%)
    mtf_max_concurrent: int = 3     # crypto + metals legs (6x / 3 = 2x per leg)
    mtf_min_signal_metals: float = 0.0015  # metals move slower — lower EMA threshold
    mtf_stop_cooldown: int = 12     # bars (3h) before re-entering MTF after a stop-out (no whipsaw)
    # Profit protection (live tournament phase — bank / trail winners)
    mtf_tp_pct: float = 0.020           # hard TP: +2.0% price move on leg (0 = off)
    mtf_trail_arm_pct: float = 0.010    # start trailing after +1.0% leg MTM
    mtf_trail_pct: float = 0.004        # exit if leg gives back 0.4% from peak (after armed)
    mtf_profit_reversal_arm_pct: float = 0.008  # tighten EMA exit once leg MTM >= +0.8%
    mtf_profit_reversal: float = 0.0003  # in-profit long: exit if EMA sig < +0.03% (vs -0.12%)

    # ---- Round-specific scales ----------------------------------------------
    # R1 live: COC + CSS + momentum (crypto + metals). can_add() enforces 25x cap.
    r1_spread_scale: float = 1.0
    r1_mtf_scale: float = 1.0       # MTF + metals ON in R1 (live competition)

    # R2 comeback (live): broken COC book pivoted → MTF momentum + CSS spreads full size
    r2_coc_scale: float = 0.0       # no new COC entries rest of R2
    r2_spread_scale: float = 1.0    # full CSS budget
    r2_mtf_scale: float = 1.0       # MTF + metals ON for return chase

    # R3 live: same playbook as R2 comeback — full CSS + MTF, COC off.
    r3_coc_scale: float = 0.0
    r3_spread_scale: float = 1.0    # full CSS (both pairs)
    r3_mtf_scale: float = 1.0       # MTF + metals ON (mirror R2 comeback)

    # Finals (48h): live playbook mirrors R3 — full CSS + early MTF, COC off.
    finals_coc_scale: float = 0.0       # 0 = skip COC (missed bar-0 OK)
    finals_coc_hold_hours: int = 24     # only used when finals_coc_scale > 0
    finals_spread_scale: float = 1.0    # both spread pairs, z>=1.15
    finals_mtf_scale: float = 1.0       # crypto + XAU/XAG from bar 0

    budget: SleeveBudget = field(default_factory=SleeveBudget)

    # ---- Cost model ---------------------------------------------------------
    spread_haircut: float = 2.0
    last_look_reject_frac: float = 0.10
    extra_slippage_bps: float = 0.2

    round_open_hour: int = 22
