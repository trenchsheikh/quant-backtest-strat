"""MoMQ live brain — 15-minute bar loop.

Imports signal generators directly from the backtest momq package so that
live logic is IDENTICAL to the backtested logic. No duplication.

Entry is gated behind ENTRY_ENABLED=true. Default: paper mode.

Usage:
    python -m live.brain.loop
"""
from __future__ import annotations

import re
import sys
import time
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import replace
from typing import Any

# Add repo root to path so momq package is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import httpx
import logfire
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# momq imports — identical logic to backtests
# ---------------------------------------------------------------------------
try:
    from momq.signals import (
        SIGNAL_GENERATORS,
        build_spread_states,
        BarContext,
        TradeIntent,
        SignalKind,
    )
    from momq.tournament_config import (
        TournamentConfig,
        RoundKind,
        PRIMARY_SYMBOLS,
        CRYPTO_SYMBOLS,
        MOMENTUM_SYMBOLS,
        METAL_SYMBOLS,
    )
    MOMQ_AVAILABLE = True
except ImportError as _momq_err:
    MOMQ_AVAILABLE = False
    _momq_import_error = str(_momq_err)

# ---------------------------------------------------------------------------
# Live system imports
# ---------------------------------------------------------------------------
from live.config import Settings, ROUND_SCHEDULE, ALL_SYMBOLS, COC_SYMBOLS, MTF_SYMBOLS, CSS_SYMBOLS, MIN_ROUND_TRADES, FINALS_MTF_SYMBOLS
from live.state.store import StateStore, LiveState, LivePosition, CocReentryItem
from live.state.trade_journal import TradeJournal
from live.brain.bar_buffer import BarBuffer
from live.brain.reconcile import reconcile
from live.brain.risk import LiveRiskEngine, NET_DIR_WARN
from live.brain.rd_monitor import LiveRdMonitor, normalize_margin_level, NET_DIR_ENTRY_BLOCK
from live.brain.round_metrics import compute_round_metrics
from live.brain.singleton import acquire_brain_lock
from live.brain.manual_close import apply_manual_close_policy
from live.bridge.symbol_specs import SymbolSpec
from live.strategy_mode import StrategyMode, get_strategy_mode

BAR_SECONDS = 900           # 15 minutes
BAR_SETTLE_SECS = 8         # Wait after bar close for quotes to settle (was 2)
MAGIC_NUMBER = 20260621     # Competition-specific magic number
ORDER_RETRY_DELAYS_S = (0.0, 0.8, 1.5, 2.5)  # brain-side retries after bridge attempt

# COC signal parameters (from competition spec)
COC_PRE_WINDOW = 48
COC_N_LONG = 3
COC_N_SHORT = 2
COC_TP_PCT = 0.002
COC_REENTRY_COOLDOWN = 1

# MTF signal parameters
MTF_FAST = 4
MTF_SLOW = 16
MTF_START_BAR = 8
MTF_MAX_HOLD = 24
MTF_STOP_PCT = 0.025
MTF_MIN_SIGNAL = 0.003
MTF_REVERSAL = 0.0012
MTF_MAX_CONCURRENT = 2

# CSS spread parameters
SPREAD_Z_WINDOW = 32
SPREAD_Z_ENTRY = 1.3
SPREAD_Z_EXIT = 0.10
SPREAD_MAX_HOLD = 16
SPREAD_START_BAR = 4

log = logging.getLogger("momq.brain")


def _parse_css_entry_z(signal_info: str) -> float:
    m = re.search(r"z=([+-]?\d+\.?\d*)", signal_info)
    if not m:
        return 0.0
    try:
        return float(m.group(1))
    except ValueError:
        return 0.0


class BrainLoop:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bridge_url = f"http://{settings.BRIDGE_HOST}:{settings.BRIDGE_PORT}"
        self._client = httpx.Client(
            base_url=self.bridge_url,
            timeout=15.0,
        )
        self._store = StateStore(settings.STATE_DIR)
        self._journal = TradeJournal(settings.STATE_DIR)
        self._buffer = BarBuffer(ALL_SYMBOLS, max_bars=400)
        self._risk = LiveRiskEngine(init_equity=settings.INIT_EQUITY)
        self._rd = LiveRdMonitor()
        self._rd_entries_blocked = False
        self._live_equity = float(settings.INIT_EQUITY)
        self._used_margin = 0.0
        self._free_margin = 0.0
        self._margin_level: float | None = None
        self._seeded = False
        self._symbol_specs: dict[str, SymbolSpec] = {}

        if MOMQ_AVAILABLE:
            self._cfg = TournamentConfig()
        else:
            self._cfg = None
            logfire.warn("momq_import_failed", error=_momq_import_error)

    def _refresh_symbol_specs(self) -> None:
        """Load broker contract/volume limits from bridge (cached per brain run)."""
        try:
            resp = self._client.get("/symbols/specs", timeout=15.0)
            if resp.status_code != 200:
                logfire.warn("brain.symbol_specs_http", status=resp.status_code)
                return
            specs: dict[str, SymbolSpec] = {}
            for row in resp.json():
                sym = str(row["symbol"]).upper()
                specs[sym] = SymbolSpec(
                    symbol=sym,
                    contract_size=float(row["contract_size"]),
                    volume_min=float(row["volume_min"]),
                    volume_max=float(row["volume_max"]),
                    volume_step=float(row["volume_step"]),
                )
            if specs:
                self._symbol_specs = specs
                logfire.info("brain.symbol_specs_loaded", n=len(specs))
        except Exception as exc:
            logfire.warn("brain.symbol_specs_error", error=str(exc))

    def _symbol_spec(self, symbol: str) -> SymbolSpec | None:
        return self._symbol_specs.get(symbol.upper())

    def _lots_for_notional(self, symbol: str, notional_usd: float, price: float) -> float:
        spec = self._symbol_spec(symbol)
        if spec is not None:
            return spec.notional_to_lots(notional_usd, price)
        return self._risk.notional_to_lots(symbol, notional_usd, price)

    def _notional_from_lots(self, symbol: str, lots: float, price: float) -> float:
        spec = self._symbol_spec(symbol)
        if spec is not None:
            return spec.lots_to_notional(lots, price)
        return self._risk.lots_to_notional(symbol, lots, price)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Outer loop: restart inner loop on any exception."""
        self._brain_lock = acquire_brain_lock(self.settings.STATE_DIR)
        logfire.info("brain.run_start", entry_enabled=self.settings.ENTRY_ENABLED)
        if not MOMQ_AVAILABLE:
            logfire.error("brain.momq_unavailable", error=_momq_import_error)
            log.error(f"Cannot start brain: momq package not available: {_momq_import_error}")
            return

        while True:
            try:
                self._inner_loop()
            except KeyboardInterrupt:
                logfire.info("brain.keyboard_interrupt")
                break
            except Exception as exc:
                logfire.error("brain.outer_loop_error", error=str(exc), exc_info=True)
                log.exception("Brain inner loop crashed — restarting in 5s")
                time.sleep(5)

    def _inner_loop(self) -> None:
        """Inner loop: seed history once, then run bar-by-bar."""
        logfire.info("brain.inner_loop_start")

        # Seed history on first entry or after crash restart
        if not self._seeded:
            try:
                self._seed_history()
                self._seeded = True
                self._refresh_symbol_specs()
                logfire.info("brain.history_seeded")
            except Exception as exc:
                logfire.error("brain.seed_error", error=str(exc))
                log.error(f"Failed to seed history: {exc} — will retry in 30s")
                time.sleep(30)
                return

        while True:
            # Check kill switch — poll; resume automatically when flag is removed
            if self.settings.KILL_SWITCH_FILE.exists():
                logfire.warn("brain.kill_switch_file_detected")
                log.warning("Kill switch active — sleeping (remove kill.flag to resume)")
                time.sleep(30)
                continue

            # Only trade when strategy.mode is bar (scalp loop owns scalp mode)
            if get_strategy_mode(self.settings) != StrategyMode.BAR:
                log.debug("Strategy mode is not 'bar' — brain idle")
                time.sleep(10)
                continue

            # Find current round
            round_info = self._current_round()
            if round_info is None:
                # Not in a round — sleep until the next one
                next_start = self._next_round_start()
                if next_start:
                    wait_secs = (next_start - datetime.now(timezone.utc)).total_seconds()
                    wait_secs = max(wait_secs, 0)
                    logfire.info("brain.idle_waiting", next_round_in_secs=round(wait_secs, 0))
                    log.info(f"No active round. Next round starts in {wait_secs/3600:.1f}h")
                    # Sleep in chunks so kill switch is checked regularly
                    sleep_chunk = min(wait_secs, 60)
                    time.sleep(sleep_chunk)
                else:
                    logfire.info("brain.all_rounds_complete")
                    log.info("All rounds complete. Brain exiting.")
                    return
                continue

            kind, t0, t1, bar_i = round_info

            # Sleep until next bar close + settle
            next_bar = self._next_bar_time(t0)
            now = datetime.now(timezone.utc)
            sleep_secs = (next_bar - now).total_seconds()
            if sleep_secs > 0:
                log.debug(f"Sleeping {sleep_secs:.1f}s until next bar at {next_bar.isoformat()}")
                time.sleep(sleep_secs)

            # Recalculate bar_i after sleep
            round_info = self._current_round()
            if round_info is None:
                continue
            kind, t0, t1, bar_i = round_info

            logfire.info(
                "brain.bar_tick",
                round=kind,
                bar_i=bar_i,
                t0=t0.isoformat(),
            )

            try:
                self._on_bar(kind, t0, t1, bar_i)
            except Exception as exc:
                logfire.error(
                    "brain.bar_error",
                    bar_i=bar_i,
                    round=kind,
                    error=str(exc),
                    exc_info=True,
                )
                log.exception(f"Error processing bar {bar_i} in round {kind}")
                # Don't crash the loop — try again next bar

    # ------------------------------------------------------------------
    # Timing helpers
    # ------------------------------------------------------------------

    def _next_bar_time(self, round_t0: datetime) -> datetime:
        """Return the UTC time of the next 15m bar boundary + settle buffer."""
        now = datetime.now(timezone.utc)
        elapsed = (now - round_t0).total_seconds()
        bars_done = int(elapsed / BAR_SECONDS)
        next_bar_elapsed = (bars_done + 1) * BAR_SECONDS
        next_bar_ts = round_t0 + timedelta(seconds=next_bar_elapsed + BAR_SETTLE_SECS)
        return next_bar_ts

    def _current_round(self) -> tuple[str, datetime, datetime, int] | None:
        """Return (kind, t0, t1, bar_i) if we are inside a scheduled round."""
        now = datetime.now(timezone.utc)
        for kind, t0, t1 in ROUND_SCHEDULE:
            if t0 <= now < t1:
                bar_i = int((now - t0).total_seconds() / BAR_SECONDS)
                return (kind, t0, t1, bar_i)
        return None

    def _next_round_start(self) -> datetime | None:
        """Return the start time of the next future round, or None if all done."""
        now = datetime.now(timezone.utc)
        future = [t0 for _, t0, _ in ROUND_SCHEDULE if t0 > now]
        return min(future) if future else None

    # ------------------------------------------------------------------
    # History seeding
    # ------------------------------------------------------------------

    def _seed_history(self) -> None:
        """Fetch and load historical 15m bars for all symbols from the bridge."""
        limit = self.settings.HISTORY_BARS
        for symbol in ALL_SYMBOLS:
            try:
                resp = self._client.get(f"/rates/{symbol}", params={"limit": limit})
                if resp.status_code != 200:
                    logfire.warn("brain.seed_symbol_failed", symbol=symbol, status=resp.status_code)
                    continue
                bars: list[dict] = resp.json()
                if not bars:
                    logfire.warn("brain.seed_symbol_empty", symbol=symbol)
                    continue
                # Drop the last bar — MT5 pos 0 is the still-forming candle.
                if len(bars) > 1:
                    bars = bars[:-1]

                ts_list = [
                    datetime.fromisoformat(b["ts"].replace("Z", "+00:00"))
                    for b in bars
                ]
                close_list = [float(b["close"]) for b in bars]

                self._buffer.seed(
                    symbol=symbol,
                    ts_col=ts_list,
                    close_col=close_list,
                    spread_bps_col=None,  # Use defaults
                )
                logfire.info(
                    "brain.seed_symbol_ok",
                    symbol=symbol,
                    n_bars=len(bars),
                )
            except Exception as exc:
                logfire.error("brain.seed_symbol_error", symbol=symbol, error=str(exc))

    # ------------------------------------------------------------------
    # Per-bar quote fetch
    # ------------------------------------------------------------------

    def _fetch_bar(self, symbols: list[str]) -> tuple[dict[str, float], dict[str, float]]:
        """Fetch latest quote mid and spread for each symbol.

        Returns:
            (mids, spreads) where each is {symbol: value}
        """
        mids: dict[str, float] = {}
        spreads: dict[str, float] = {}
        for symbol in symbols:
            try:
                resp = self._client.get(f"/quote/{symbol}")
                if resp.status_code == 200:
                    q = resp.json()
                    mids[symbol] = float(q["mid"])
                    spreads[symbol] = float(q.get("spread_bps", 1.5))
            except Exception as exc:
                logfire.warn("brain.fetch_quote_error", symbol=symbol, error=str(exc))
        return mids, spreads

    # ------------------------------------------------------------------
    # Round transition
    # ------------------------------------------------------------------

    def _reset_round_state(
        self,
        state: LiveState,
        kind: str,
        t0: datetime,
        live_equity: float,
    ) -> LiveState:
        """Clear per-round flags and scoring window when a competition round starts."""
        logfire.info(
            "brain.round_transition",
            from_round=state.round_kind,
            to_round=kind,
            from_t0=state.round_t0.isoformat() if state.round_t0 else None,
            to_t0=t0.isoformat(),
            equity_anchor=live_equity,
        )
        state.coc_deployed = False
        state.coc_ever_this_round = kind in ("r2", "r3", "finals")
        state.coc_reentry_queue = {}
        state.coc_reentry_used = set()
        state.mtf_cooldown = {}
        state.css_pair_cooldown = {}
        state.mtf_side_block = {}
        state.rd_entries_blocked = False
        # Stage 3 / Finals: DD + Sharpe reset on fresh equity window (PnL carries via live equity)
        state.round_equity_anchor = live_equity
        state.equity_samples = [live_equity]
        state.rd_score = 100.0
        state.rd_streaks = {}
        state.rd_violations = []
        state.rd_compliance_flags = []
        self._rd.reset()
        state.round_trade_count = self._journal.count_executed_since(t0)
        state.round_sharpe_eligible = state.round_trade_count >= MIN_ROUND_TRADES
        return state

    def _ensure_r2_playbook(self, state: LiveState, kind: str) -> None:
        """Mid-round: unlock full CSS aggression when COC is off (R2/R3 comeback)."""
        if kind in ("r2", "r3", "finals") and not state.coc_ever_this_round:
            state.coc_ever_this_round = True

    # ------------------------------------------------------------------
    # Main per-bar logic
    # ------------------------------------------------------------------

    def _on_bar(self, kind: str, t0: datetime, t1: datetime, bar_i: int) -> None:
        """Process a single 15m bar for the active round."""
        if not self._symbol_specs:
            self._refresh_symbol_specs()

        # 1. Fetch current prices
        mids, spreads = self._fetch_bar(ALL_SYMBOLS)
        if not mids:
            logfire.error("brain.no_quotes", bar_i=bar_i)
            return

        # 2. Update bar buffer — MT5 timestamps are bar *open* times, so the
        # bar that just closed at this wake is (bar_i - 1), not bar_i.
        closed_bar_i = max(bar_i - 1, 0)
        bar_ts = t0 + timedelta(seconds=closed_bar_i * BAR_SECONDS)
        self._buffer.push(bar_ts, mids, spreads)

        # 3. Load state — fetch equity before round transition (anchor for Stage 3 reset)
        state = self._store.load()
        used_margin = 0.0
        free_margin = 0.0
        margin_level: float | None = None
        try:
            account_resp = self._client.get("/account")
            account_data = account_resp.json() if account_resp.status_code == 200 else {}
            live_equity = float(account_data.get("equity", self.settings.INIT_EQUITY))
            used_margin = float(account_data.get("margin", 0.0))
            free_margin = float(account_data.get("free_margin", 0.0))
            ml = account_data.get("margin_level")
            margin_level = normalize_margin_level(
                float(ml) if ml is not None else None,
                used_margin,
            )
        except Exception as exc:
            logfire.warn("brain.account_fetch_error", error=str(exc))
            live_equity = float(self._live_equity or self.settings.INIT_EQUITY)

        self._live_equity = live_equity
        self._used_margin = used_margin
        self._free_margin = free_margin
        self._margin_level = margin_level

        if state.round_kind != kind or (
            state.round_t0 is not None and state.round_t0 != t0
        ):
            state = self._reset_round_state(state, kind, t0, live_equity)
        elif state.round_equity_anchor <= 0:
            state.round_equity_anchor = live_equity
            if not state.equity_samples:
                state.equity_samples = [live_equity]

        state.round_kind = kind
        state.bar_i = bar_i
        state.round_t0 = t0
        state.entry_enabled = self.settings.ENTRY_ENABLED
        state.round_trade_count = self._journal.count_executed_since(t0)
        state.round_sharpe_eligible = state.round_trade_count >= MIN_ROUND_TRADES
        self._ensure_r2_playbook(state, kind)

        # Fetch live MT5 positions for reconcile
        try:
            positions_resp = self._client.get("/positions")
            live_mt5_positions: list[dict] = positions_resp.json() if positions_resp.status_code == 200 else []
        except Exception as exc:
            logfire.warn("brain.positions_fetch_error", error=str(exc))
            live_mt5_positions = []

        # Reconcile positions
        saved_before = {p.ticket: p for p in state.positions}
        recon_result, reconciled_positions = reconcile(
            saved=state.positions,
            live_mt5=live_mt5_positions,
            logger_ctx={"bar_i": bar_i, "round": kind},
        )
        for ticket in recon_result.closed_in_mt5:
            pos = saved_before[ticket]
            snap = self._position_snapshot(ticket)
            pnl = snap.get("profit")
            cfg = self._cfg or TournamentConfig(init_equity=self.settings.INIT_EQUITY)
            exit_reason = apply_manual_close_policy(
                state,
                pos,
                float(pnl) if pnl is not None else None,
                live_equity,
                bar_i,
                mtf_stop_cooldown_bars=cfg.mtf_stop_cooldown,
            )
            self._journal.record_close(
                ticket=ticket,
                symbol=pos.symbol,
                side=pos.side,
                lots=pos.lots,
                price=snap.get("price", pos.entry_price),
                entry_price=pos.entry_price,
                sleeve=pos.sleeve,
                pair_id=pos.pair_id,
                signal_info=pos.signal_info,
                round_kind=kind,
                bar_i=bar_i,
                exit_reason=exit_reason,
                pnl=pnl,
                bars_held=pos.bars_held,
            )
            if snap.get("profit") is not None:
                state.realized_pnl += float(snap["profit"])
        state.positions = reconciled_positions

        # Restore RD monitor from persisted state (survives brain restart)
        self._rd.restore_from_state(
            state.rd_score,
            state.rd_streaks,
            state.rd_violations,
            state.rd_compliance_flags,
        )

        # Increment bars_held for all open positions
        for pos in state.positions:
            pos.bars_held += 1

        # 4. Manage exits
        state = self._manage_exits(state, bar_i, kind, t0, mids, spreads, live_equity)

        # 4b. COC re-entry after take-profit (mirrors tournament_engine)
        state = self._process_coc_reentries(state, bar_i, kind, mids, live_equity)

        # 4c. RD entry gate — use post-exit portfolio + normalized margin (not stale prior bar)
        pre_metrics = self._risk.portfolio_metrics(
            state.positions, live_equity, used_margin
        )
        state = self._trim_net_directional_exposure(state, pre_metrics)
        self._rd.sync_streaks_for_gating(
            leverage=pre_metrics["leverage"],
            single_instrument=pre_metrics["single_instrument"],
            net_directional=pre_metrics["net_directional"],
            margin_usage=pre_metrics["margin_usage"],
        )
        self._rd_entries_blocked = self._rd.compute_entries_blocked(
            leverage=pre_metrics["leverage"],
            single_instrument=pre_metrics["single_instrument"],
            net_directional=pre_metrics["net_directional"],
            margin_usage=pre_metrics["margin_usage"],
            margin_level=margin_level,
            used_margin=used_margin,
        )

        # 5. Build BarContext and run signals
        if not MOMQ_AVAILABLE:
            logfire.error("brain.momq_unavailable_on_bar")
        else:
            ctx = self._build_ctx(kind, t0, t1, bar_i, mids, spreads, state, live_equity)
            if ctx is not None:
                intents = self._run_signals(ctx)
                intents = self._apply_trade_chase_policy(intents, kind, state)
                mtf_pending = [i for i in intents if i.kind == SignalKind.MTF]
                hedge_intents = self._order_bar_hedge_intents(
                    [i for i in intents if i.kind != SignalKind.MTF],
                    kind,
                )
                pre_hedge = list(state.positions)
                state = self._execute_intents(hedge_intents, state, mids, live_equity)
                mtf_kept = self._filter_mtf_rd_safe(mtf_pending, state, live_equity, mids)
                if mtf_pending:
                    n_at_open = len(
                        self._filter_mtf_rd_safe(
                            mtf_pending,
                            state.model_copy(update={"positions": pre_hedge}),
                            live_equity,
                            mids,
                        )
                    )
                    if len(mtf_kept) > n_at_open:
                        logfire.info(
                            "brain.mtf_same_bar_after_hedge",
                            n_mtf=len(mtf_kept),
                            n_at_bar_open=n_at_open,
                            n_dropped=len(mtf_pending) - len(mtf_kept),
                        )
                state = self._execute_intents(mtf_kept, state, mids, live_equity)

        # 6. RD telemetry + rules.md §13 sustained breach tracking (end-of-bar)
        metrics = self._risk.portfolio_metrics(
            state.positions, live_equity, used_margin
        )
        rd_state = self._rd.update_bar(
            bar_i,
            leverage=metrics["leverage"],
            single_instrument=metrics["single_instrument"],
            net_directional=metrics["net_directional"],
            margin_usage=metrics["margin_usage"],
            margin_level=margin_level,
        )
        state.rd_score = rd_state.score
        state.rd_streaks = dict(rd_state.streaks)
        state.rd_violations = [
            {"kind": v.kind, "penalty": v.penalty, "bar_i": v.bar_i, "detail": v.detail}
            for v in rd_state.violations
        ]
        state.rd_compliance_flags = list(rd_state.compliance_flags)
        state.rd_entries_blocked = rd_state.entries_blocked
        state.rd_last_metrics = metrics
        self._rd_entries_blocked = rd_state.entries_blocked

        warnings = self._risk.check_rd_warnings(
            state.positions, live_equity, used_margin, margin_level
        )
        if warnings:
            for w in warnings:
                log.warning(f"[RD] {w}")
        if rd_state.violations and rd_state.violations[-1].bar_i == bar_i:
            v = rd_state.violations[-1]
            logfire.error(
                "risk.rd_penalty",
                kind=v.kind,
                penalty=v.penalty,
                rd_score=rd_state.score,
                detail=v.detail,
            )

        # 7. Track equity
        state.equity_samples.append(live_equity)
        if len(state.equity_samples) > 500:
            state.equity_samples = state.equity_samples[-500:]

        # 8. Save state
        self._store.save(state)

        round_m = compute_round_metrics(
            state.equity_samples,
            round_equity_anchor=state.round_equity_anchor,
            trade_count=state.round_trade_count,
        )
        logfire.info(
            "brain.bar_complete",
            round=kind,
            bar_i=bar_i,
            n_positions=len(state.positions),
            equity=live_equity,
            leverage=round(self._risk.leverage(state.positions, live_equity), 3),
            rd_score=round(state.rd_score, 1),
            rd_blocked=state.rd_entries_blocked,
            margin_level=margin_level,
            round_trades=round_m.trade_count,
            trades_needed=round_m.trades_needed,
            sharpe_eligible=round_m.sharpe_eligible,
            round_sharpe=round(round_m.sharpe_15m, 4),
            round_dd_pct=round(round_m.round_max_dd_pct, 3),
            round_ret_pct=round(round_m.round_return_pct, 3),
        )

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    def _run_signals(self, ctx: "BarContext") -> list["TradeIntent"]:
        """Run all sleeve signal generators and return combined intent list."""
        intents: list[TradeIntent] = []
        for gen in SIGNAL_GENERATORS:
            try:
                sleeve_intents = gen(ctx)
                intents.extend(sleeve_intents)
                logfire.debug(
                    "brain.signal_gen",
                    generator=gen.__name__,
                    n=len(sleeve_intents),
                    bar_i=ctx.bar_i,
                )
            except Exception as exc:
                logfire.error(
                    "brain.signal_gen_error",
                    generator=gen.__name__,
                    error=str(exc),
                    exc_info=True,
                )
        intents.sort(key=lambda x: -x.priority)
        return intents

    def _filter_executable_intents(
        self,
        intents: list["TradeIntent"],
        state: LiveState,
        open_css_pairs: set[str],
    ) -> list["TradeIntent"]:
        busy = {p.symbol for p in state.positions}
        executable: list[TradeIntent] = []
        for intent in intents:
            if intent.kind == SignalKind.CSS and intent.pair_id in open_css_pairs:
                continue
            if intent.kind in (SignalKind.COC, SignalKind.MTF):
                if any(sym in busy for sym in intent.symbols):
                    continue
            executable.append(intent)
        return executable

    def _apply_trade_chase_policy(
        self,
        intents: list["TradeIntent"],
        kind: str,
        state: LiveState,
    ) -> list["TradeIntent"]:
        """Finals COC playbook: CSS-first until >30 orders. R3-style Finals runs full stack."""
        if kind != "finals":
            return intents

        cfg = self._cfg or TournamentConfig(init_equity=self.settings.INIT_EQUITY)
        if cfg.finals_coc_scale <= 0:
            return intents

        if state.round_trade_count >= MIN_ROUND_TRADES:
            return intents

        held = {p.symbol for p in state.positions}
        css = [i for i in intents if i.kind == SignalKind.CSS]
        if css:
            logfire.info(
                "brain.trade_chase_css_only",
                round_trades=state.round_trade_count,
                needed=MIN_ROUND_TRADES - state.round_trade_count,
                n_css=len(css),
            )
            return css

        mtf = [
            i for i in intents
            if i.kind == SignalKind.MTF
            and not any(sym in held for sym in i.symbols)
        ]
        if mtf:
            logfire.info(
                "brain.trade_chase_mtf_fallback",
                round_trades=state.round_trade_count,
                n_mtf=len(mtf),
            )
            return mtf[:1]

        logfire.warn(
            "brain.trade_chase_no_intents",
            round_trades=state.round_trade_count,
            needed=MIN_ROUND_TRADES - state.round_trade_count,
        )
        return []

    def _order_bar_hedge_intents(
        self,
        intents: list["TradeIntent"],
        kind: str,
    ) -> list["TradeIntent"]:
        """Order CSS/COC (non-MTF) intents; MTF is executed in a second pass after hedges."""
        if not intents:
            return []

        css = [i for i in intents if i.kind == SignalKind.CSS]
        rest = [i for i in intents if i.kind != SignalKind.CSS]
        cfg = self._cfg or TournamentConfig(init_equity=self.settings.INIT_EQUITY)
        if kind == "finals" and cfg.finals_coc_scale <= 0 and css:
            logfire.info(
                "brain.finals_css_first",
                n_css=len(css),
                n_other=len(rest),
            )
            ordered = css + rest
        else:
            ordered = rest + css

        ordered.sort(key=lambda x: -x.priority)
        return ordered

    def _apply_rd_compliance_policy(
        self,
        intents: list["TradeIntent"],
        kind: str,
        state: LiveState,
        equity: float,
    ) -> list["TradeIntent"]:
        """Legacy single-pass planner — prefer two-phase execution in ``_on_bar``."""
        mtf = [i for i in intents if i.kind == SignalKind.MTF]
        hedge = [i for i in intents if i.kind != SignalKind.MTF]
        kept_mtf = self._filter_mtf_rd_safe(mtf, state, equity, {})
        return self._order_bar_hedge_intents(hedge, kind) + kept_mtf

    def _intent_legs_capped(
        self,
        intent: "TradeIntent",
        mids: dict[str, float],
    ) -> list[tuple[str, int, float]]:
        """Broker-aware leg notionals (volume_max / step applied)."""
        legs: list[tuple[str, int, float]] = []
        for sym, side in zip(intent.symbols, intent.sides):
            price = mids.get(sym, 0.0)
            if price <= 0:
                continue
            lots = self._lots_for_notional(sym, intent.notional_per_leg, price)
            legs.append((sym, side, self._notional_from_lots(sym, lots, price)))
        return legs

    def _filter_mtf_rd_safe(
        self,
        mtf_intents: list["TradeIntent"],
        state: LiveState,
        equity: float,
        mids: dict[str, float],
    ) -> list["TradeIntent"]:
        """Greedily keep MTF legs while projected net-directional stays < 92%."""
        if not mtf_intents:
            return []

        used_margin = getattr(self, "_used_margin", 0.0)
        virtual = list(state.positions)
        kept: list[TradeIntent] = []
        for intent in sorted(mtf_intents, key=lambda x: -x.priority):
            legs = self._intent_legs_capped(intent, mids)
            projected = self._risk._project_positions(virtual, legs)
            pm = self._risk.portfolio_metrics(projected, equity, used_margin)
            if pm["net_directional"] < NET_DIR_ENTRY_BLOCK:
                kept.append(intent)
                virtual = projected
            else:
                logfire.warn(
                    "brain.mtf_rd_capped",
                    symbols=list(intent.symbols),
                    net_dir=round(pm["net_directional"], 4),
                    block=NET_DIR_ENTRY_BLOCK,
                )
        return kept

    def _trim_net_directional_exposure(
        self,
        state: LiveState,
        metrics: dict[str, float],
    ) -> LiveState:
        """Close weakest MTF leg when net-dir is elevated and RD streak is building."""
        net_dir = metrics["net_directional"]
        streak = int(state.rd_streaks.get("net_directional", 0))
        if net_dir < NET_DIR_WARN and streak < 1:
            return state

        mtf_positions = [p for p in state.positions if p.sleeve == "mtf"]
        if len(mtf_positions) < 2:
            return state

        has_short_hedge = any(
            p.side < 0 for p in state.positions if p.sleeve in ("css", "unknown", "coc")
        )
        if has_short_hedge and net_dir < NET_DIR_ENTRY_BLOCK:
            return state

        weakest = min(mtf_positions, key=lambda p: abs(p.notional_usd))
        logfire.warn(
            "brain.rd_trim_mtf",
            symbol=weakest.symbol,
            ticket=weakest.ticket,
            net_dir=round(net_dir, 4),
            streak=streak,
        )
        self._close_position(weakest, "rd_net_dir_trim", state)
        return state

    def _hard_block_kwargs(
        self,
        *,
        allow_basket: bool = False,
        directional_sleeve: bool = False,
    ) -> dict:
        return {
            "used_margin": getattr(self, "_used_margin", 0.0),
            "margin_level": getattr(self, "_margin_level", None),
            "entries_blocked_rd": getattr(self, "_rd_entries_blocked", False),
            "allow_incomplete_basket": allow_basket,
            "allow_directional_sleeve": directional_sleeve,
        }

    def _multi_leg_basket_status(
        self,
        executable: list["TradeIntent"],
        state: LiveState,
        equity: float,
        mids: dict[str, float],
    ) -> dict["SignalKind", bool]:
        """Pre-approve multi single-leg batches (COC/MTF) on full projected book."""
        status: dict[SignalKind, bool] = {}
        for kind in (SignalKind.COC, SignalKind.MTF):
            batch = [i for i in executable if i.kind == kind]
            if not batch:
                continue
            if len(batch) < 2 and kind != SignalKind.MTF:
                continue
            legs = [
                leg
                for intent in batch
                for leg in self._intent_legs_capped(intent, mids)
            ]
            block = self._risk.hard_block(
                state.positions,
                legs,
                equity,
                **self._hard_block_kwargs(allow_basket=True),
            )
            if block:
                logfire.warn(
                    "brain.basket_risk_blocked",
                    kind=kind.value,
                    n_legs=len(legs),
                    reason=block,
                )
                log.warning(f"Basket risk block ({kind.value}): {block}")
                status[kind] = False
            else:
                status[kind] = True
        return status

    def _execute_intents(
        self,
        intents: list["TradeIntent"],
        state: LiveState,
        mids: dict[str, float],
        equity: float,
    ) -> LiveState:
        open_css_pairs = {
            p.pair_id for p in state.positions if p.sleeve == "css" and p.pair_id
        }
        executable = self._filter_executable_intents(intents, state, open_css_pairs)
        basket_status = self._multi_leg_basket_status(executable, state, equity, mids)
        rejected_baskets = {k for k, ok in basket_status.items() if not ok}
        approved_baskets = {k for k, ok in basket_status.items() if ok}

        for intent in executable:
            if intent.kind in rejected_baskets:
                continue
            allow_basket = intent.kind in approved_baskets
            try:
                ok, new_positions = self._execute_intent(
                    intent,
                    mids,
                    state,
                    equity,
                    allow_incomplete_basket=allow_basket,
                    allow_directional_sleeve=(
                        intent.kind == SignalKind.MTF and allow_basket
                    ),
                )
                if ok and new_positions:
                    state.positions.extend(new_positions)
                    if intent.kind == SignalKind.COC:
                        state.coc_deployed = True
                        state.coc_ever_this_round = True
                        state.coc_ever_this_round = True
                    if intent.kind == SignalKind.CSS and intent.pair_id:
                        open_css_pairs.add(intent.pair_id)
            except Exception as exc:
                logfire.error(
                    "brain.intent_error",
                    intent=str(intent),
                    error=str(exc),
                    exc_info=True,
                )
        return state

    def _round_budgets(
        self, kind: str, bar_i: int, cfg: "TournamentConfig"
    ) -> tuple[float, float, float]:
        """Mirror tournament_engine per-round sleeve budgets."""
        coc_budget = cfg.budget.coc
        spread_budget = cfg.budget.corr_spread
        mtf_budget = cfg.budget.mtf

        round_kind = RoundKind[kind.upper()]
        if round_kind == RoundKind.R1:
            spread_budget *= cfg.r1_spread_scale
            mtf_budget *= cfg.r1_mtf_scale
        elif round_kind == RoundKind.R2:
            coc_budget *= cfg.r2_coc_scale
            spread_budget *= cfg.r2_spread_scale
            mtf_budget *= cfg.r2_mtf_scale
        elif round_kind == RoundKind.R3:
            coc_budget *= cfg.r3_coc_scale
            spread_budget *= cfg.r3_spread_scale
            mtf_budget *= cfg.r3_mtf_scale
        elif round_kind == RoundKind.FINALS:
            coc_budget *= cfg.finals_coc_scale
            spread_budget *= cfg.finals_spread_scale
            mtf_budget *= cfg.finals_mtf_scale
            if cfg.finals_coc_scale > 0:
                finals_coc_bars = cfg.finals_coc_hold_hours * 4
                if bar_i < finals_coc_bars:
                    mtf_budget = 0.0

        return coc_budget, spread_budget, mtf_budget

    # ------------------------------------------------------------------
    # BarContext builder
    # ------------------------------------------------------------------

    def _build_ctx(
        self,
        kind: str,
        t0: datetime,
        t1: datetime,
        bar_i: int,
        mids: dict[str, float],
        spreads: dict[str, float],
        state: LiveState,
        equity: float,
    ) -> "BarContext | None":
        """Build a momq BarContext for the current bar."""
        try:
            mids_df = self._buffer.get_mids()
            spreads_df = self._buffer.get_spreads()

            if mids_df.empty:
                logfire.warn("brain.ctx_no_data", bar_i=bar_i)
                return None

            # Use the latest bar timestamp from the buffer (MT5-aligned),
            # falling back to the round schedule if the buffer is empty.
            if len(mids_df.index) > 0:
                ts = pd.Timestamp(mids_df.index[-1])
            else:
                bar_ts = t0 + timedelta(seconds=bar_i * BAR_SECONDS)
                ts = pd.Timestamp(bar_ts)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")

            cfg = self._cfg or TournamentConfig(init_equity=self.settings.INIT_EQUITY)
            round_kind = RoundKind[kind.upper()]
            if round_kind == RoundKind.FINALS:
                cfg = replace(
                    cfg,
                    mtf_max_concurrent=min(cfg.mtf_max_concurrent, MTF_MAX_CONCURRENT),
                )
            coc_budget, spread_budget, mtf_budget = self._round_budgets(kind, bar_i, cfg)

            primary = [s for s in PRIMARY_SYMBOLS if s in mids_df.columns]
            if round_kind == RoundKind.FINALS:
                crypto = [s for s in FINALS_MTF_SYMBOLS if s in mids_df.columns]
            else:
                crypto = [
                    s for s in MOMENTUM_SYMBOLS
                    if s in mids_df.columns and s not in METAL_SYMBOLS
                ]
            spread_states = build_spread_states(round_kind, mids_df, spread_budget)

            open_mtf_symbols: set[str] = {
                p.symbol for p in state.positions if p.sleeve == "mtf"
            }
            open_css_pairs: set[str] = {
                p.pair_id for p in state.positions if p.sleeve == "css" and p.pair_id
            }
            open_coc_symbols: set[str] = {
                p.symbol for p in state.positions if p.sleeve == "coc"
            }
            open_symbols: set[str] = {p.symbol for p in state.positions}
            mtf_cooldown_syms: set[str] = {
                sym for sym, cooldown_bar in state.mtf_cooldown.items()
                if bar_i < cooldown_bar
            }
            css_pair_cooldown: set[str] = {
                pid for pid, until in state.css_pair_cooldown.items()
                if bar_i < until
            }
            blocked_mtf_sides: set[str] = {
                key for key, until in state.mtf_side_block.items()
                if bar_i < until
            }

            t0_ts = pd.Timestamp(t0)
            if t0_ts.tzinfo is None:
                t0_ts = t0_ts.tz_localize("UTC")
            t1_ts = pd.Timestamp(t1)
            if t1_ts.tzinfo is None:
                t1_ts = t1_ts.tz_localize("UTC")

            coc_deployed = bool(open_coc_symbols)

            return BarContext(
                ts=ts,
                bar_i=bar_i,
                round_kind=round_kind,
                t0=t0_ts,
                t1=t1_ts,
                mids=mids_df,
                spreads=spreads_df,
                cfg=cfg,
                coc_budget=coc_budget,
                spread_budget=spread_budget,
                mtf_budget=mtf_budget,
                spread_states=spread_states,
                open_css_pairs=open_css_pairs,
                open_coc_symbols=open_coc_symbols,
                open_symbols=open_symbols,
                coc_deployed=coc_deployed,
                coc_ever_this_round=state.coc_ever_this_round,
                primary=primary,
                crypto_symbols=crypto,
                open_mtf_symbols=open_mtf_symbols,
                mtf_stop_cooldown_syms=mtf_cooldown_syms,
                css_pair_cooldown=css_pair_cooldown,
                blocked_mtf_sides=blocked_mtf_sides,
            )
        except Exception as exc:
            logfire.error("brain.build_ctx_error", error=str(exc), exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Exit management
    # ------------------------------------------------------------------

    def _manage_exits(
        self,
        state: LiveState,
        bar_i: int,
        kind: str,
        t0: datetime,
        mids: dict[str, float],
        spreads: dict[str, float],
        equity: float,
    ) -> LiveState:
        """Check all open positions for exit conditions and close where needed."""
        positions_to_close: list[tuple[LivePosition, str]] = []

        for pos in list(state.positions):
            price = mids.get(pos.symbol, 0.0)
            if price <= 0:
                continue

            close_reason: str | None = None

            if pos.sleeve == "coc":
                # TP hit check
                if pos.tp_price > 0:
                    if pos.side == 1 and price >= pos.tp_price:
                        close_reason = f"coc_tp_long price={price:.5f} tp={pos.tp_price:.5f}"
                    elif pos.side == -1 and price <= pos.tp_price:
                        close_reason = f"coc_tp_short price={price:.5f} tp={pos.tp_price:.5f}"
                # Finals COC bank at bar 96
                if kind == "finals" and bar_i == 96:
                    close_reason = "finals_coc_bank_bar96"

            elif pos.sleeve == "mtf":
                cfg = self._cfg or TournamentConfig()
                # Stop loss: position price MTM worse than -stop_pct
                pos_mtm = (price / pos.entry_price - 1.0) * pos.side
                price_move = pos_mtm
                if price_move > pos.peak_mtm_pct:
                    pos.peak_mtm_pct = price_move

                if pos_mtm < -MTF_STOP_PCT:
                    close_reason = f"mtf_stop_loss mtm={pos_mtm*100:.2f}%"
                elif cfg.mtf_tp_pct > 0 and price_move >= cfg.mtf_tp_pct:
                    close_reason = (
                        f"mtf_tp mtm={price_move*100:.2f}% tp={cfg.mtf_tp_pct*100:.2f}%"
                    )
                elif (
                    cfg.mtf_trail_arm_pct > 0
                    and cfg.mtf_trail_pct > 0
                    and pos.peak_mtm_pct >= cfg.mtf_trail_arm_pct
                    and price_move <= pos.peak_mtm_pct - cfg.mtf_trail_pct
                ):
                    close_reason = (
                        f"mtf_trail peak={pos.peak_mtm_pct*100:.2f}% "
                        f"mtm={price_move*100:.2f}%"
                    )
                # Max hold
                elif pos.bars_held >= MTF_MAX_HOLD:
                    close_reason = f"mtf_max_hold bars={pos.bars_held}"
                # EMA reversal check (tighter when leg is in profit)
                else:
                    mids_df = self._buffer.get_mids()
                    if pos.symbol in mids_df.columns:
                        sym_series = mids_df[pos.symbol].dropna().values
                        if len(sym_series) >= MTF_SLOW:
                            fast_ema = _ema(sym_series, MTF_FAST)
                            slow_ema = _ema(sym_series, MTF_SLOW)
                            cross_signal = (
                                (fast_ema[-1] - slow_ema[-1]) / slow_ema[-1]
                                if slow_ema[-1] != 0
                                else 0
                            )
                            in_profit_mode = (
                                cfg.mtf_profit_reversal_arm_pct > 0
                                and price_move >= cfg.mtf_profit_reversal_arm_pct
                            )
                            if in_profit_mode:
                                rev = cfg.mtf_profit_reversal
                                if pos.side == 1 and cross_signal < rev:
                                    close_reason = (
                                        f"mtf_profit_reversal signal={cross_signal:.4f}"
                                    )
                                elif pos.side == -1 and cross_signal > -rev:
                                    close_reason = (
                                        f"mtf_profit_reversal signal={cross_signal:.4f}"
                                    )
                            else:
                                if pos.side == 1 and cross_signal < -MTF_REVERSAL:
                                    close_reason = f"mtf_ema_reversal signal={cross_signal:.4f}"
                                elif pos.side == -1 and cross_signal > MTF_REVERSAL:
                                    close_reason = f"mtf_ema_reversal signal={cross_signal:.4f}"

            elif pos.sleeve == "css":
                cfg = self._cfg or TournamentConfig()
                z = self._css_current_z(pos.pair_id, mids) if pos.pair_id else None
                if z is not None and abs(z) <= cfg.spread_z_exit:
                    close_reason = f"css_z_reversion z={z:.3f}"
                elif z is not None and cfg.spread_z_stop > 0:
                    entry_z = pos.entry_z or _parse_css_entry_z(pos.signal_info)
                    if entry_z != 0.0 and abs(z) >= abs(entry_z) + cfg.spread_z_stop:
                        close_reason = f"css_z_stop z={z:.3f} entry_z={entry_z:.2f}"
                if close_reason is None and pos.bars_held >= cfg.spread_max_hold:
                    close_reason = f"css_max_hold bars={pos.bars_held}"

            if close_reason is not None:
                positions_to_close.append((pos, close_reason))

        # Execute closes
        for pos, reason in positions_to_close:
            self._close_position(pos, reason, state)

        return state

    def _css_current_z(self, pair_id: str, mids: dict[str, float]) -> float | None:
        """Estimate current z-score for a CSS pair given current mid prices."""
        try:
            if "/" in pair_id:
                sym1, sym2 = pair_id.split("/", 1)
            else:
                parts = pair_id.split("_")
                if len(parts) != 2:
                    return None
                sym1, sym2 = parts[0], parts[1]
            mids_df = self._buffer.get_mids()
            if sym1 not in mids_df.columns or sym2 not in mids_df.columns:
                return None
            s1 = mids_df[sym1].dropna().values
            s2 = mids_df[sym2].dropna().values
            n = min(len(s1), len(s2), SPREAD_Z_WINDOW)
            if n < 8:
                return None
            s1 = s1[-n:]
            s2 = s2[-n:]
            # Log spread
            spread = np.log(s1) - np.log(s2)
            z = (spread[-1] - spread.mean()) / (spread.std() + 1e-10)
            return float(z)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def _post_market_order(self, payload: dict[str, Any], symbol: str) -> dict | None:
        """POST /order with retries on transient requote / price errors."""
        last_result: dict | None = None
        for attempt, delay in enumerate(ORDER_RETRY_DELAYS_S):
            if delay > 0:
                time.sleep(delay)
            try:
                resp = self._client.post("/order", json=payload)
            except httpx.HTTPError as exc:
                logfire.warn(
                    "brain.order_http_error",
                    symbol=symbol,
                    attempt=attempt + 1,
                    error=str(exc),
                )
                continue

            if resp.status_code != 200:
                logfire.error(
                    "brain.order_http_error",
                    status=resp.status_code,
                    body=resp.text[:200],
                    symbol=symbol,
                    attempt=attempt + 1,
                )
                return None

            result = resp.json()
            last_result = result
            if result.get("ok"):
                if attempt > 0:
                    logfire.info(
                        "brain.order_filled_after_retry",
                        symbol=symbol,
                        attempt=attempt + 1,
                        ticket=result.get("ticket"),
                    )
                return result

            msg = str(result.get("message", ""))
            retryable = any(
                tok in msg
                for tok in ("10004", "10015", "10020", "10021", "Requote", "requote", "price")
            )
            if retryable and attempt + 1 < len(ORDER_RETRY_DELAYS_S):
                logfire.warn(
                    "brain.order_retry",
                    symbol=symbol,
                    message=msg[:120],
                    attempt=attempt + 1,
                )
                continue

            logfire.warn(
                "brain.order_rejected",
                symbol=symbol,
                message=msg,
                lots=payload.get("lots"),
            )
            return result

        return last_result

    def _execute_intent(
        self,
        intent: "TradeIntent",
        mids: dict[str, float],
        state: LiveState,
        equity: float,
        allow_incomplete_basket: bool = False,
        allow_directional_sleeve: bool = False,
    ) -> tuple[bool, list[LivePosition]]:
        """Execute a momq TradeIntent via the bridge."""
        if state.kill_switch or self.settings.KILL_SWITCH_FILE.exists():
            logfire.warn("brain.intent_blocked_kill_switch", kind=intent.kind.value)
            return False, []

        if not self.settings.ENTRY_ENABLED:
            logfire.info("brain.intent_paper", intent=str(intent))
            log.info(f"PAPER: {intent}")
            return False, []

        sleeve = intent.kind.value
        legs = self._intent_legs_capped(intent, mids)
        if not legs:
            return False, []

        block = self._risk.hard_block(
            state.positions,
            legs,
            equity,
            **self._hard_block_kwargs(
                allow_basket=allow_incomplete_basket,
                directional_sleeve=allow_directional_sleeve,
            ),
        )
        if block:
            logfire.warn("brain.intent_risk_blocked", reason=block, kind=sleeve)
            log.warning(f"Risk block: {block}")
            return False, []

        new_positions: list[LivePosition] = []
        for sym, side in zip(intent.symbols, intent.sides):
            price = mids.get(sym, 0.0)
            if price <= 0:
                logfire.warn("brain.intent_no_price", symbol=sym)
                self._rollback_positions(new_positions, state)
                return False, []

            # Per-leg RD check after prior legs in this intent filled
            lots = self._lots_for_notional(sym, intent.notional_per_leg, price)
            capped_notional = self._notional_from_lots(sym, lots, price)
            spec = self._symbol_spec(sym)
            if spec is not None:
                raw_lots = intent.notional_per_leg / (spec.contract_size * price)
                if lots + 1e-9 < raw_lots:
                    logfire.warn(
                        "brain.volume_capped",
                        symbol=sym,
                        requested_notional=intent.notional_per_leg,
                        capped_notional=capped_notional,
                        requested_lots=round(raw_lots, 4),
                        capped_lots=lots,
                        volume_max=spec.volume_max,
                    )

            leg_block = self._risk.hard_block(
                state.positions + new_positions,
                [(sym, side, capped_notional)],
                equity,
                used_margin=getattr(self, "_used_margin", 0.0),
                margin_level=getattr(self, "_margin_level", None),
                entries_blocked_rd=getattr(self, "_rd_entries_blocked", False),
                allow_incomplete_pair=len(intent.symbols) > 1,
                allow_incomplete_basket=allow_incomplete_basket,
                allow_directional_sleeve=allow_directional_sleeve,
            )
            if leg_block:
                logfire.warn("brain.leg_risk_blocked", reason=leg_block, symbol=sym)
                self._rollback_positions(new_positions, state)
                return False, []

            if lots <= 0:
                logfire.warn(
                    "brain.intent_zero_lots",
                    symbol=sym,
                    notional=intent.notional_per_leg,
                    price=price,
                )
                return False, []

            sl_price = 0.0
            tp_price = 0.0
            if sleeve == "coc" and COC_TP_PCT > 0:
                tp_price = price * (1 + COC_TP_PCT) if side == 1 else price * (1 - COC_TP_PCT)

            comment = f"{sleeve}:{intent.reason[:20]}" if intent.reason else sleeve

            payload = {
                "symbol": sym,
                "side": side,
                "lots": lots,
                "sl_price": sl_price,
                "tp_price": tp_price,
                "comment": comment[:31],
                "magic": MAGIC_NUMBER,
            }
            result = self._post_market_order(payload, sym)
            if result is None:
                self._rollback_positions(new_positions, state)
                return False, []

            if not result.get("ok"):
                self._rollback_positions(new_positions, state)
                return False, []

            ticket = result.get("ticket")
            is_paper = result.get("paper", False)
            fill_lots = float(result.get("lots") or lots)
            fill_price = float(result.get("fill_price") or price)
            actual_notional = self._notional_from_lots(sym, fill_lots, fill_price)

            new_positions.append(LivePosition(
                ticket=ticket if ticket is not None else -1,
                symbol=sym,
                side=side,
                lots=fill_lots,
                notional_usd=actual_notional,
                entry_price=fill_price,
                entry_ts=datetime.now(timezone.utc),
                sleeve=sleeve,
                pair_id=intent.pair_id,
                sl_price=sl_price,
                tp_price=tp_price,
                bars_held=0,
                signal_info=intent.reason,
                entry_z=_parse_css_entry_z(intent.reason) if sleeve == "css" else 0.0,
            ))

            logfire.info(
                "brain.order_placed",
                ticket=ticket,
                symbol=sym,
                side=side,
                lots=fill_lots,
                notional_usd=actual_notional,
                sleeve=sleeve,
                paper=is_paper,
                price=fill_price,
                pair_id=intent.pair_id,
            )
            if ticket is not None and ticket > 0:
                self._journal.record_open(
                    ticket=ticket,
                    symbol=sym,
                    side=side,
                    lots=fill_lots,
                    price=fill_price,
                    notional_usd=actual_notional,
                    sleeve=sleeve,
                    pair_id=intent.pair_id,
                    signal_info=intent.reason,
                    round_kind=state.round_kind,
                    bar_i=state.bar_i,
                    paper=is_paper,
                )
            time.sleep(0.15)  # brief pause between multi-leg intent orders

        return bool(new_positions), new_positions

    def _position_snapshot(self, ticket: int) -> dict[str, float | None]:
        """Fetch current price and unrealised PnL for a position ticket."""
        try:
            resp = self._client.get("/positions")
            if resp.status_code != 200:
                return {}
            for p in resp.json():
                if int(p.get("ticket", 0)) == ticket:
                    return {
                        "price": float(p.get("price_current", p.get("price_open", 0))),
                        "profit": float(p.get("profit", 0)),
                    }
        except Exception:
            pass
        return {}

    def _rollback_positions(
        self, positions: list[LivePosition], state: LiveState
    ) -> None:
        """Close any legs already opened when a multi-leg intent fails partway."""
        for pos in positions:
            if pos.ticket <= 0:
                continue
            snap = self._position_snapshot(pos.ticket)
            try:
                resp = self._client.post(f"/close/{pos.ticket}")
                if resp.status_code == 200 and resp.json().get("closed"):
                    logfire.info("brain.rollback_closed", ticket=pos.ticket, symbol=pos.symbol)
                    self._journal.record_close(
                        ticket=pos.ticket,
                        symbol=pos.symbol,
                        side=pos.side,
                        lots=pos.lots,
                        price=snap.get("price") or pos.entry_price,
                        entry_price=pos.entry_price,
                        sleeve=pos.sleeve,
                        pair_id=pos.pair_id,
                        signal_info=pos.signal_info,
                        round_kind=state.round_kind,
                        bar_i=state.bar_i,
                        exit_reason="rollback",
                        pnl=snap.get("profit"),
                        bars_held=pos.bars_held,
                    )
                    if snap.get("profit") is not None:
                        state.realized_pnl += float(snap["profit"])
                else:
                    logfire.warn("brain.rollback_failed", ticket=pos.ticket, symbol=pos.symbol)
            except Exception as exc:
                logfire.error("brain.rollback_error", ticket=pos.ticket, error=str(exc))

    # ------------------------------------------------------------------
    # Close helper
    # ------------------------------------------------------------------

    def _close_position(
        self,
        pos: LivePosition,
        reason: str,
        state: LiveState,
    ) -> bool:
        """Close a position via bridge and update state."""
        if pos.ticket <= 0:
            # Paper position — just remove from state
            state.positions = [p for p in state.positions if p.ticket != pos.ticket]
            logfire.info(
                "brain.paper_position_closed",
                ticket=pos.ticket,
                symbol=pos.symbol,
                reason=reason,
            )
            return True

        try:
            snap = self._position_snapshot(pos.ticket)
            resp = self._client.post(f"/close/{pos.ticket}")
            ok = resp.status_code == 200 and resp.json().get("closed", False)
        except Exception as exc:
            logfire.error(
                "brain.close_request_error",
                ticket=pos.ticket,
                symbol=pos.symbol,
                error=str(exc),
            )
            ok = False
            snap = {}

        if ok:
            state.positions = [p for p in state.positions if p.ticket != pos.ticket]
            exit_price = snap.get("price") or pos.entry_price
            pnl = snap.get("profit")
            if pos.sleeve == "coc" and reason.startswith("coc_tp"):
                if pos.symbol not in state.coc_reentry_used:
                    state.coc_reentry_queue[pos.symbol] = CocReentryItem(
                        reentry_bar=state.bar_i + COC_REENTRY_COOLDOWN,
                        side=pos.side,
                        notional_usd=pos.notional_usd,
                        signal_info=pos.signal_info,
                    )
                    state.coc_reentry_used.add(pos.symbol)
            if pos.sleeve == "mtf" and "mtf_stop" in reason:
                cfg = self._cfg or TournamentConfig()
                state.mtf_cooldown[pos.symbol] = state.bar_i + cfg.mtf_stop_cooldown
            self._journal.record_close(
                ticket=pos.ticket,
                symbol=pos.symbol,
                side=pos.side,
                lots=pos.lots,
                price=exit_price,
                entry_price=pos.entry_price,
                sleeve=pos.sleeve,
                pair_id=pos.pair_id,
                signal_info=pos.signal_info,
                round_kind=state.round_kind,
                bar_i=state.bar_i,
                exit_reason=reason,
                pnl=pnl,
                bars_held=pos.bars_held,
            )
            if pnl is not None:
                state.realized_pnl += float(pnl)
            logfire.info(
                "brain.position_closed",
                ticket=pos.ticket,
                symbol=pos.symbol,
                sleeve=pos.sleeve,
                bars_held=pos.bars_held,
                reason=reason,
                entry_price=pos.entry_price,
                pnl=pnl,
            )
        else:
            logfire.error(
                "brain.close_failed",
                ticket=pos.ticket,
                symbol=pos.symbol,
                reason=reason,
            )

        return ok

    def _process_coc_reentries(
        self,
        state: LiveState,
        bar_i: int,
        kind: str,
        mids: dict[str, float],
        equity: float,
    ) -> LiveState:
        """Execute queued COC re-entries after take-profit cooldown."""
        if not state.coc_reentry_queue:
            return state

        busy = {p.symbol for p in state.positions}
        for sym in list(state.coc_reentry_queue.keys()):
            item = state.coc_reentry_queue[sym]
            if bar_i < item.reentry_bar or sym in busy:
                continue
            if sym not in mids or mids[sym] <= 0:
                del state.coc_reentry_queue[sym]
                continue

            intent = TradeIntent(
                SignalKind.COC,
                (sym,),
                (item.side,),
                item.notional_usd,
                priority=10.0,
                reason=item.signal_info + " [re-entry after TP]",
            )
            ok, new_positions = self._execute_intent(intent, mids, state, equity)
            del state.coc_reentry_queue[sym]
            if ok and new_positions:
                state.positions.extend(new_positions)
                state.coc_deployed = True
                state.coc_ever_this_round = True
                logfire.info("brain.coc_reentry_executed", symbol=sym, bar_i=bar_i)
            else:
                logfire.warn("brain.coc_reentry_failed", symbol=sym, bar_i=bar_i)

        return state


# ------------------------------------------------------------------
# EMA helper (used for MTF exit check, not importing from momq to avoid
# circular dependency or import errors on startup)
# ------------------------------------------------------------------

def _ema(values: np.ndarray, span: int) -> np.ndarray:
    """Compute exponential moving average with the same alpha as pandas ewm(span=...)."""
    alpha = 2.0 / (span + 1)
    result = np.empty_like(values, dtype=float)
    result[0] = values[0]
    for i in range(1, len(values)):
        result[i] = alpha * values[i] + (1 - alpha) * result[i - 1]
    return result


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    settings = Settings()

    if settings.LOGFIRE_TOKEN:
        logfire.configure(token=settings.LOGFIRE_TOKEN, service_name="momq-brain")
    else:
        logfire.configure(send_to_logfire=False, service_name="momq-brain")

    logfire.info(
        "brain.start",
        entry_enabled=settings.ENTRY_ENABLED,
        bridge=f"{settings.BRIDGE_HOST}:{settings.BRIDGE_PORT}",
    )

    loop = BrainLoop(settings)
    loop.run()
