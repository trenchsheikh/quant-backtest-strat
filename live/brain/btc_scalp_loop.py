"""BTC ping-pong scalp loop — fast poll on /quote, separate from 15m bar brain.

Usage:
    python -m live.brain.btc_scalp_loop

Only trades when live_state/strategy.mode is ``scalp``. Respects kill.flag.
"""
from __future__ import annotations

import logging
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import httpx
import logfire

from live.config import Settings, ROUND_SCHEDULE, CONTRACT_SIZES
from live.state.trade_journal import TradeJournal
from live.brain.singleton import acquire_process_lock
from live.strategy_mode import get_strategy_mode, StrategyMode
from momq.btc_scalp import BtcScalpParams, ScalpPosition, should_exit_live
from momq.hft_scalp_research import EntryMode, HftScalpConfig, entry_direction

SCALP_MAGIC = 20260624
log = logging.getLogger("momq.scalp")


@dataclass
class _OpenTrade:
    ticket: int
    side: int
    entry_price: float
    entry_ts: float
    lots: float
    paper: bool
    best_mtm: float = 0.0
    worst_mtm: float = 0.0


class BtcScalpLoop:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.symbol = settings.SCALP_SYMBOL
        self.bridge_url = f"http://{settings.BRIDGE_HOST}:{settings.BRIDGE_PORT}"
        self._client = httpx.Client(base_url=self.bridge_url, timeout=10.0)
        self._journal = TradeJournal(settings.STATE_DIR)
        self._params = BtcScalpParams.for_lot(
            settings.SCALP_LOT_BTC,
            symbol=settings.SCALP_SYMBOL,
            tp_usd=settings.SCALP_TP_USD,
            sl_usd=settings.SCALP_SL_USD,
            tp_per_lot=settings.SCALP_TP_USD_PER_LOT,
            sl_per_lot=settings.SCALP_SL_USD_PER_LOT,
            max_hold_s=settings.SCALP_MAX_HOLD_S,
            flip_on_close=settings.SCALP_FLIP_ON_CLOSE,
            momentum_after_sl=settings.SCALP_MOMENTUM_AFTER_SL,
            momentum_lookback_s=settings.SCALP_MOM_FAST_S,
        )
        try:
            self._entry_mode = EntryMode(settings.SCALP_ENTRY_MODE.lower())
        except ValueError:
            log.warning("Unknown SCALP_ENTRY_MODE=%s — using flip", settings.SCALP_ENTRY_MODE)
            self._entry_mode = EntryMode.FLIP
        self._hft_cfg = HftScalpConfig(
            lot_btc=settings.SCALP_LOT_BTC,
            tp_usd=self._params.tp_usd,
            sl_usd=self._params.sl_usd,
            max_hold_s=settings.SCALP_MAX_HOLD_S,
            entry_mode=self._entry_mode,
            mom_fast_s=int(settings.SCALP_MOM_FAST_S),
            mom_slow_s=int(settings.SCALP_MOM_SLOW_S),
            micro_thresh=settings.SCALP_MICRO_THRESH,
            min_range_usd=settings.SCALP_MIN_RANGE_USD,
            adverse_buffer_usd=settings.SCALP_ADVERSE_BUFFER_USD,
            momentum_after_sl=settings.SCALP_MOMENTUM_AFTER_SL,
            flip_on_close=settings.SCALP_FLIP_ON_CLOSE,
            pause_after_sl_streak=settings.SCALP_PAUSE_AFTER_SL_STREAK,
        )
        self._open: _OpenTrade | None = None
        self._pending_dir: int = 1
        hist_len = max(32, int(settings.SCALP_MOM_SLOW_S / max(settings.SCALP_POLL_S, 0.1)) + 4)
        self._mid_history: deque[tuple[float, float]] = deque(maxlen=hist_len)
        self._last_order_ts: float = 0.0
        self._paper_ticket_seq = 9_000_000
        self._last_exit_reason: str | None = None
        self._last_exit_side: int | None = None
        self._sl_streak: int = 0
        self._pause_until_ts: float = 0.0
        self._stack_warn_ts: float = 0.0

    def _contract_size(self) -> float:
        return CONTRACT_SIZES.get(self.symbol, 1.0)

    def _params_for_lots(self, lots: float) -> BtcScalpParams:
        """TP/SL boundaries scaled to the actual open volume on MT5."""
        return BtcScalpParams.for_lot(
            lots,
            symbol=self.symbol,
            tp_usd=self.settings.SCALP_TP_USD,
            sl_usd=self.settings.SCALP_SL_USD,
            tp_per_lot=self.settings.SCALP_TP_USD_PER_LOT,
            sl_per_lot=self.settings.SCALP_SL_USD_PER_LOT,
            max_hold_s=self._params.max_hold_s,
            flip_on_close=self._params.flip_on_close,
            momentum_after_sl=self._params.momentum_after_sl,
            momentum_lookback_s=self._params.momentum_lookback_s,
        )

    def _mtm_usd(self, *, bid: float, ask: float, side: int, entry: float, lots: float) -> float:
        contract = self._contract_size()
        if side == 1:
            return (bid - entry) * lots * contract
        return (entry - ask) * lots * contract

    def _fetch_positions(self) -> list[dict]:
        try:
            resp = self._client.get("/positions")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError:
            return []

    def _scalp_positions(self) -> list[dict]:
        return [
            p for p in self._fetch_positions()
            if p.get("symbol") == self.symbol and int(p.get("magic", 0)) == SCALP_MAGIC
        ]

    def _warn_lot_mismatch(self, lots: float, now_ts: float) -> None:
        expected = self._params.lot_btc
        if abs(lots - expected) <= max(0.01, expected * 0.05):
            return
        if now_ts - self._stack_warn_ts < 30.0:
            return
        self._stack_warn_ts = now_ts
        logfire.warn(
            "scalp.lot_mismatch",
            expected=expected,
            actual=lots,
            symbol=self.symbol,
            hint="stacked opens from duplicate processes — managing full size, entries blocked until flat",
        )

    def _trade_from_position(self, p: dict) -> _OpenTrade:
        side = 1 if int(p.get("type", 0)) == 0 else -1
        entry_ts = time.time()
        t_raw = p.get("time", "")
        if t_raw:
            try:
                entry_ts = datetime.fromisoformat(
                    str(t_raw).replace("Z", "+00:00"),
                ).timestamp()
            except ValueError:
                pass
        profit = float(p.get("profit", 0.0))
        lots = float(p["lots"])
        return _OpenTrade(
            ticket=int(p["ticket"]),
            side=side,
            entry_price=float(p["price_open"]),
            entry_ts=entry_ts,
            lots=lots,
            paper=False,
            best_mtm=max(0.0, profit),
            worst_mtm=min(0.0, profit),
        )

    def _sync_open_from_bridge(self) -> bool:
        """Adopt an existing scalp position from MT5 when in-memory state is empty."""
        scalp = self._scalp_positions()
        if not scalp:
            return False
        if len(scalp) > 1:
            logfire.warn(
                "scalp.multiple_positions",
                count=len(scalp),
                tickets=[int(p["ticket"]) for p in scalp],
            )
        p = max(scalp, key=lambda row: float(row.get("lots", 0.0)))
        self._open = self._trade_from_position(p)
        self._warn_lot_mismatch(self._open.lots, time.time())
        logfire.info(
            "scalp.reconciled",
            ticket=self._open.ticket,
            side=self._open.side,
            lots=self._open.lots,
        )
        return True

    def _verify_flat(self, ticket: int) -> bool:
        return not any(int(p["ticket"]) == ticket for p in self._scalp_positions())

    def run(self) -> None:
        acquire_process_lock(self.settings.STATE_DIR, "scalp")
        logfire.info(
            "scalp.run_start",
            symbol=self.symbol,
            lot=self._params.lot_btc,
            tp=self._params.tp_usd,
            sl=self._params.sl_usd,
            hold_s=self._params.max_hold_s,
            poll_s=self.settings.SCALP_POLL_S,
            entry_mode=self._entry_mode.value,
            mom_fast_s=self.settings.SCALP_MOM_FAST_S,
            mom_slow_s=self.settings.SCALP_MOM_SLOW_S,
            micro_thresh=self.settings.SCALP_MICRO_THRESH,
            momentum_after_sl=self._params.momentum_after_sl,
        )
        self._reconcile_open()

        while True:
            try:
                self._tick()
            except KeyboardInterrupt:
                logfire.info("scalp.keyboard_interrupt")
                break
            except Exception as exc:
                logfire.error("scalp.tick_error", error=str(exc), exc_info=True)
                log.exception("Scalp tick failed — retry in 5s")
                time.sleep(5)

    def _tick(self) -> None:
        poll = self.settings.SCALP_POLL_S
        if self.settings.KILL_SWITCH_FILE.exists():
            log.debug("Kill switch active — scalp idle")
            time.sleep(poll)
            return

        if get_strategy_mode(self.settings) != StrategyMode.SCALP:
            log.debug("Strategy mode is not 'scalp' — idle")
            time.sleep(poll)
            return

        round_info = self._current_round()
        if round_info is None:
            time.sleep(poll)
            return

        kind, _, _, bar_i = round_info
        quote = self._fetch_quote()
        if quote is None:
            time.sleep(poll)
            return

        bid = float(quote["bid"])
        ask = float(quote["ask"])
        mid = float(quote.get("mid", (bid + ask) / 2))
        spread_bps = float(quote.get("spread_bps", 0.0))
        now_ts = time.time()
        self._mid_history.append((now_ts, mid))

        if self._open is not None:
            self._manage_open(bid, ask, now_ts, kind, bar_i)
        else:
            if self._sync_open_from_bridge():
                self._manage_open(bid, ask, now_ts, kind, bar_i)
            else:
                self._maybe_enter(bid, ask, mid, spread_bps, now_ts, kind, bar_i)

        time.sleep(poll)

    def _mid_at_lookback_s(self, now_ts: float, seconds: float) -> float | None:
        for ts, mid in reversed(self._mid_history):
            if now_ts - ts >= seconds:
                return mid
        return None

    def _micro_features(self, now_ts: float, mid: float) -> tuple[float, float, float, float, float]:
        """CIR proxy, range USD, mom_fast, mom_slow from quote history."""
        window = self.settings.SCALP_MICRO_WINDOW_S
        mids = [m for ts, m in self._mid_history if now_ts - ts <= window]
        if not mids:
            mids = [mid]
        lo, hi = min(mids), max(mids)
        rng = hi - lo
        cir = (mid - lo) / rng if rng > 0 else 0.5

        mid_fast = self._mid_at_lookback_s(now_ts, self.settings.SCALP_MOM_FAST_S)
        mid_slow = self._mid_at_lookback_s(now_ts, self.settings.SCALP_MOM_SLOW_S)
        if mid_fast is None:
            mid_fast = self._mid_history[0][1] if self._mid_history else mid
        if mid_slow is None:
            mid_slow = mid_fast

        mom_fast = mid - mid_fast
        mom_slow = mid - mid_slow
        mid_prev = self._mid_at_lookback_s(now_ts, 1.0)
        if mid_prev is None and self._mid_history:
            mid_prev = self._mid_history[0][1]
        if mid_prev is None:
            mid_prev = mid
        adverse = (mid - mid_prev) * self._params.lot_btc
        return cir, rng, mom_fast, mom_slow, adverse

    def _resolve_entry_side(
        self,
        *,
        mid: float,
        now_ts: float,
    ) -> int | None:
        cir, rng, mom_fast, mom_slow, adverse = self._micro_features(now_ts, mid)

        if self._entry_mode != EntryMode.FLIP:
            if rng < self.settings.SCALP_MIN_RANGE_USD:
                return None

        return entry_direction(
            mode=self._entry_mode,
            pending_flip=self._pending_dir,
            mom_fast=mom_fast,
            mom_slow=mom_slow,
            cir=cir,
            last_exit_reason=self._last_exit_reason,
            last_exit_side=self._last_exit_side,
            adverse_move_usd=adverse,
            cfg=self._hft_cfg,
        )

    def _mid_at_lookback(self, now_ts: float) -> float | None:
        return self._mid_at_lookback_s(now_ts, self._params.momentum_lookback_s)

    def _manage_open(
        self,
        bid: float,
        ask: float,
        now_ts: float,
        round_kind: str,
        bar_i: int,
    ) -> None:
        assert self._open is not None
        exit_params = self._params_for_lots(self._open.lots)
        mtm = self._mtm_usd(
            bid=bid,
            ask=ask,
            side=self._open.side,
            entry=self._open.entry_price,
            lots=self._open.lots,
        )
        self._open.best_mtm = max(self._open.best_mtm, mtm)
        self._open.worst_mtm = min(self._open.worst_mtm, mtm)

        pos = ScalpPosition(
            side=self._open.side,
            entry_price=self._open.entry_price,
            entry_ts=self._open.entry_ts,
        )
        exit_now, exit_pnl, reason = should_exit_live(
            pos,
            bid=bid,
            ask=ask,
            now_ts=now_ts,
            params=exit_params,
            best_mtm=self._open.best_mtm,
            worst_mtm=self._open.worst_mtm,
        )
        if not exit_now:
            return

        closed_side = self._open.side
        exit_price = bid if self._open.side == 1 else ask
        if reason in ("tp", "sl"):
            profit = exit_pnl
        elif not self._open.paper:
            profit = self._position_profit()
            if profit == 0.0:
                profit = exit_pnl
        else:
            profit = self._paper_pnl(
                self._open.side,
                self._open.entry_price,
                exit_price,
                self._open.lots,
                self.symbol,
            )

        if not self._open.paper:
            try:
                resp = self._client.post(f"/close/{self._open.ticket}")
                if resp.status_code != 200:
                    log.warning("Close failed ticket=%s status=%s", self._open.ticket, resp.status_code)
                    return
                if not self._verify_flat(self._open.ticket):
                    log.warning("Close ack but ticket=%s still open on MT5", self._open.ticket)
                    self._sync_open_from_bridge()
                    return
            except httpx.HTTPError as exc:
                log.warning("Close HTTP error ticket=%s: %s", self._open.ticket, exc)
                return

        self._journal.record_close(
            ticket=self._open.ticket,
            symbol=self.symbol,
            side=self._open.side,
            lots=self._open.lots,
            price=exit_price,
            entry_price=self._open.entry_price,
            sleeve="scalp",
            signal_info=(
                f"tp={exit_params.tp_usd:.2f} sl={exit_params.sl_usd:.2f}"
            ),
            round_kind=round_kind,
            bar_i=bar_i,
            exit_reason=reason,
            pnl=profit,
            paper=self._open.paper,
        )
        logfire.info(
            "scalp.close",
            ticket=self._open.ticket,
            reason=reason,
            pnl=round(profit, 2),
            paper=self._open.paper,
        )

        self._last_exit_reason = reason
        self._last_exit_side = closed_side
        if reason == "sl":
            self._sl_streak += 1
            streak_limit = self.settings.SCALP_PAUSE_AFTER_SL_STREAK
            if streak_limit > 0 and self._sl_streak >= streak_limit:
                self._pause_until_ts = now_ts + self.settings.SCALP_PAUSE_SECONDS
                logfire.warn(
                    "scalp.pause_after_sl_streak",
                    streak=self._sl_streak,
                    pause_s=self.settings.SCALP_PAUSE_SECONDS,
                )
                self._sl_streak = 0
        elif reason == "tp" or profit > 0:
            self._sl_streak = 0

        if self._params.flip_on_close:
            self._pending_dir = -closed_side
        self._open = None
        self._last_order_ts = now_ts

    def _maybe_enter(
        self,
        bid: float,
        ask: float,
        mid: float,
        spread_bps: float,
        now_ts: float,
        round_kind: str,
        bar_i: int,
    ) -> None:
        if now_ts < self._pause_until_ts:
            return

        if self._scalp_positions():
            log.debug("Scalp position already open on MT5 — skip entry")
            self._sync_open_from_bridge()
            return

        if spread_bps > self.settings.SCALP_MAX_SPREAD_BPS:
            log.debug("Spread %.2f bps > max %.2f — skip entry", spread_bps, self.settings.SCALP_MAX_SPREAD_BPS)
            return

        elapsed = now_ts - self._last_order_ts
        if elapsed < self.settings.SCALP_MIN_ORDER_INTERVAL_S:
            return

        side = self._resolve_entry_side(mid=mid, now_ts=now_ts)
        if side is None:
            return

        entry_price = ask if side == 1 else bid
        lots = self._params.lot_btc

        try:
            resp = self._client.post(
                "/order",
                json={
                    "symbol": self.symbol,
                    "side": side,
                    "lots": lots,
                    "comment": "scalp",
                    "magic": SCALP_MAGIC,
                },
            )
            resp.raise_for_status()
            result = resp.json()
        except httpx.HTTPError as exc:
            log.warning("Order HTTP error: %s", exc)
            return

        if not result.get("ok"):
            log.debug("Order rejected: %s", result.get("message"))
            return

        paper = bool(result.get("paper"))
        ticket = result.get("ticket")
        if ticket is None:
            if paper:
                self._paper_ticket_seq += 1
                ticket = self._paper_ticket_seq
            else:
                log.warning("Order ok but no ticket: %s", result)
                return

        contract = self._contract_size()
        notional = lots * contract * entry_price

        self._open = _OpenTrade(
            ticket=int(ticket),
            side=side,
            entry_price=entry_price,
            entry_ts=now_ts,
            lots=lots,
            paper=paper,
            best_mtm=0.0,
            worst_mtm=0.0,
        )

        if not paper:
            time.sleep(0.15)
            live = self._scalp_positions()
            match = [p for p in live if int(p["ticket"]) == int(ticket)]
            if not match and live:
                adopted = max(live, key=lambda row: float(row.get("lots", 0.0)))
                self._open = self._trade_from_position(adopted)
                logfire.warn(
                    "scalp.adopted_net_position",
                    ticket=self._open.ticket,
                    lots=self._open.lots,
                    expected=lots,
                )
            elif match:
                live_lots = float(match[0]["lots"])
                self._open.lots = live_lots
                if abs(live_lots - lots) > max(0.01, lots * 0.05):
                    logfire.warn(
                        "scalp.fill_lot_mismatch",
                        expected=lots,
                        actual=live_lots,
                        ticket=int(ticket),
                    )

        notional = self._open.lots * contract * entry_price
        cir, rng, mom_fast, mom_slow, _ = self._micro_features(now_ts, mid)
        signal_info = (
            f"mode={self._entry_mode.value} "
            f"tp={self._params.tp_usd:.2f} sl={self._params.sl_usd:.2f} "
            f"cir={cir:.2f} rng={rng:.2f} m2={mom_fast:.2f} m8={mom_slow:.2f}"
        )
        self._journal.record_open(
            ticket=self._open.ticket,
            symbol=self.symbol,
            side=side,
            lots=self._open.lots,
            price=entry_price,
            notional_usd=notional,
            sleeve="scalp",
            signal_info=signal_info,
            round_kind=round_kind,
            bar_i=bar_i,
            paper=paper,
        )
        logfire.info(
            "scalp.open",
            ticket=self._open.ticket,
            side=side,
            price=entry_price,
            lots=self._open.lots,
            spread_bps=round(spread_bps, 2),
            tp=self._params.tp_usd,
            sl=self._params.sl_usd,
            paper=paper,
        )

    def _fetch_quote(self) -> dict | None:
        try:
            resp = self._client.get(f"/quote/{self.symbol}")
            if resp.status_code != 200:
                return None
            return resp.json()
        except httpx.HTTPError:
            return None

    def _position_profit(self) -> float:
        if self._open is None:
            return 0.0
        try:
            resp = self._client.get("/positions")
            resp.raise_for_status()
            for p in resp.json():
                if int(p["ticket"]) == self._open.ticket:
                    return float(p.get("profit", 0.0))
        except httpx.HTTPError:
            pass
        return 0.0

    @staticmethod
    def _paper_pnl(side: int, entry: float, exit_px: float, lots: float, symbol: str) -> float:
        contract = CONTRACT_SIZES.get(symbol, 1.0)
        if side == 1:
            return (exit_px - entry) * lots * contract
        return (entry - exit_px) * lots * contract

    def _reconcile_open(self) -> None:
        if self._sync_open_from_bridge():
            return

    def _current_round(self) -> tuple[str, datetime, datetime, int] | None:
        now = datetime.now(timezone.utc)
        for kind, t0, t1 in ROUND_SCHEDULE:
            if t0 <= now < t1:
                bar_i = int((now - t0).total_seconds() / 900)
                return (kind, t0, t1, bar_i)
        return None


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    settings = Settings()
    if settings.LOGFIRE_TOKEN:
        logfire.configure(token=settings.LOGFIRE_TOKEN, service_name="momq-scalp")
    else:
        logfire.configure(send_to_logfire=False, service_name="momq-scalp")

    BtcScalpLoop(settings).run()
