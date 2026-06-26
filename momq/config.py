"""Strategy / cost / risk configuration for the MoMQ spread backtest.

Every default here is deliberately CONSERVATIVE. The competition punishes
blow-up terminally, so the backtest should if anything understate edge and
overstate cost. If the numbers look good under these settings, they're real.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class CostConfig:
    # We charge HALF the measured top-of-book spread per leg per fill (you buy
    # at ask / sell at bid -> half-spread vs mid each side). A round trip on a
    # 2-leg spread therefore pays ~ (spread_A + spread_B), i.e. 4 half-spreads.
    spread_haircut: float = 2.0      # multiply measured spread by this. The tick
                                     # feed is one raw provider; the comp's
                                     # executable spread after markup + Last Look
                                     # is wider. 2x is a blunt, safe haircut.
    last_look_reject_frac: float = 0.10  # fraction of fills assumed rejected.
                                     # Rejections hit your FAVOURABLE fills, so
                                     # we model it as a flat extra cost penalty
                                     # (bps) rather than a free re-try.
    extra_slippage_bps: float = 0.2  # per fill per leg, on top of spread.


@dataclass
class RiskConfig:
    risk_frac: float = 0.0035        # 0.35% of equity risked per trade (mid of
                                     # the 0.25-0.5% band).
    leverage_cap: float = 3.0        # gross notional / equity hard cap (R1
                                     # posture; the 30x available is irrelevant).
    z_stop: float = 3.5              # close at a loss if the spread diverges to
                                     # this z (bounds per-trade loss ~ risk_frac).
    max_concurrent_per_pair: int = 1 # one position per pair at a time.


@dataclass
class SignalConfig:
    beta_window: int = 96            # bars used for rolling hedge-ratio (1 day).
    z_window: int = 96               # bars used for rolling spread mean/std.
    z_entry: float = 2.0             # enter when |z| > this.
    z_exit: float = 0.3              # exit when |z| < this (reversion to ~0).
    min_history: int = 96            # don't trade until this many bars seen.
    time_stop_bars: int = 96         # force-close a trade after this many bars
                                     # (a spread that hasn't reverted in a day is
                                     # probably a broken relationship).
    use_log: bool = True             # build spread in log-price space.


@dataclass
class Pair:
    """A tradable spread. leg_b=None means trade the instrument's own
    z-score reversion (for already-stationary crosses like EURGBP/EURCHF)."""
    leg_a: str
    leg_b: str | None = None
    label: str = ""

    def __post_init__(self):
        if not self.label:
            self.label = self.leg_a if self.leg_b is None else f"{self.leg_a}/{self.leg_b}"


# Default pair universe — starter portfolio (backtest-ranked May–Jun 2026).
# Symbols are *backtest* parquet names; live bridge must resolve platform names.
DEFAULT_PAIRS: list[Pair] = [
    Pair("EURJPY", None, "eurjpy-revert"),
    Pair("GBPUSD", None, "gbpusd-revert"),
    Pair("USDCAD", None, "usdcad-revert"),
    Pair("EURUSD", "GBPUSD", "eur/gbp-$spread"),
]


def all_symbol_pairs(symbols: list[str]) -> list[Pair]:
    """One single-leg z-reversion strategy per symbol in the panel."""
    return [Pair(sym, None, f"{sym}-revert") for sym in sorted(symbols)]


@dataclass
class BacktestConfig:
    init_equity: float = 1_000_000.0
    cost: CostConfig = field(default_factory=CostConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    pairs: list[Pair] = field(default_factory=lambda: list(DEFAULT_PAIRS))
