"""MetaTrader5 client wrapper.

Wraps the MetaTrader5 Python library with:
- Automatic reconnection with exponential backoff
- logfire structured logging for every operation
- All errors raised as MT5Error (never raw library exceptions)
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import logfire
import pandas as pd

from live.bridge.order_utils import (
    ORDER_MAX_ATTEMPTS,
    is_retryable_retcode,
    normalize_volume,
    order_deviation_points,
    retry_sleep,
)
from live.bridge.symbol_specs import SymbolSpec

try:
    import MetaTrader5 as mt5
except ImportError:
    # Allow import on non-Windows machines for type-checking / testing
    mt5 = None  # type: ignore[assignment]


class MT5Error(Exception):
    """Raised when any MT5 operation fails."""
    pass


class OrderFill:
    """Market order fill details returned to the bridge API."""

    __slots__ = ("ticket", "lots", "price", "requested_lots")

    def __init__(
        self,
        ticket: int,
        lots: float,
        price: float,
        requested_lots: float,
    ) -> None:
        self.ticket = ticket
        self.lots = lots
        self.price = price
        self.requested_lots = requested_lots


class MT5Client:
    def __init__(
        self,
        login: int,
        password: str,
        server: str,
        terminal_path: str = "",
        timeout_ms: int = 10_000,
        reconnect_cooldown: float = 60.0,
    ) -> None:
        self.login = login
        self.password = password
        self.server = server
        self.terminal_path = terminal_path
        self.timeout_ms = timeout_ms
        self.reconnect_cooldown = reconnect_cooldown
        self._connected: bool = False
        self._last_failed_connect: float = 0.0  # epoch seconds
        self.last_order_error: str = ""

    def _send_with_fillings(self, request: dict) -> object | None:
        """Try order_send with broker-supported filling modes (IOC → FOK → RETURN)."""
        symbol = request.get("symbol", "")
        fillings: list[int] = []
        info = mt5.symbol_info(symbol) if symbol else None
        if info is not None:
            fm = int(info.filling_mode)
            if fm & 2:
                fillings.append(mt5.ORDER_FILLING_IOC)
            if fm & 1:
                fillings.append(mt5.ORDER_FILLING_FOK)
            if fm & 4:
                fillings.append(mt5.ORDER_FILLING_RETURN)
        if not fillings:
            fillings = [
                mt5.ORDER_FILLING_IOC,
                mt5.ORDER_FILLING_FOK,
                mt5.ORDER_FILLING_RETURN,
            ]

        last_result = None
        for filling in fillings:
            req = {**request, "type_filling": filling}
            result = mt5.order_send(req)
            last_result = result
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                return result
        return last_result

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Initialise MT5 terminal and log in. Returns True on success.

        Connection order (important for Algo Trading):
          1. Attach to an already-running terminal (no path) — preserves the
             user's Algo Trading button and Options settings.
          2. Launch/connect via terminal_path if nothing is running.
          3. Pass credentials only if login is still required.
        """
        if mt5 is None:
            raise MT5Error("MetaTrader5 package not installed")
        logfire.info("mt5.connect", login=self.login, server=self.server, path=self.terminal_path)

        timeout_kw = {"timeout": self.timeout_ms}

        # Attempt 1: attach to running terminal — do NOT pass path (path spawns
        # a fresh terminal where Algo Trading defaults to OFF).
        initialized = mt5.initialize(**timeout_kw)

        # Attempt 2: cold start — launch terminal at configured path
        if not initialized and self.terminal_path:
            initialized = mt5.initialize(path=self.terminal_path, **timeout_kw)

        # Attempt 3: explicit login if terminal is up but wrong account
        if not initialized and self.login:
            cred_kwargs = {**timeout_kw, "login": self.login, "password": self.password, "server": self.server}
            if self.terminal_path:
                cred_kwargs["path"] = self.terminal_path
            initialized = mt5.initialize(**cred_kwargs)

        if not initialized:
            err = mt5.last_error()
            logfire.error("mt5.initialize_failed", error_code=err[0], error_msg=err[1])
            self._connected = False
            self._last_failed_connect = time.monotonic()
            return False

        info = mt5.account_info()
        if info is None:
            err = mt5.last_error()
            logfire.error("mt5.account_info_failed_on_connect", error_code=err[0], error_msg=err[1])
            mt5.shutdown()
            self._connected = False
            self._last_failed_connect = time.monotonic()
            return False
        term = mt5.terminal_info()
        trade_allowed = bool(term.trade_allowed) if term else False
        logfire.info(
            "mt5.connected",
            login=info.login,
            server=info.server,
            balance=info.balance,
            equity=info.equity,
            currency=info.currency,
            trade_allowed=trade_allowed,
        )
        if not trade_allowed:
            logfire.warn(
                "mt5.trade_not_allowed",
                note="Enable Algo Trading in MT5 toolbar and "
                     "Tools > Options > Expert Advisors > Allow algorithmic trading",
            )
        self._connected = True
        return True

    def terminal_trade_allowed(self) -> bool:
        """Return True if MT5 terminal allows API trading (Algo Trading ON)."""
        if mt5 is None or not self._connected:
            return False
        term = mt5.terminal_info()
        return bool(term and term.trade_allowed)

    def _ensure_connected(self) -> None:
        """Ensure MT5 is connected; retry up to 5 times with exponential backoff.

        Circuit-breaker: if the last connect attempt failed less than
        reconnect_cooldown seconds ago, raise immediately without retrying.
        This prevents blocking the async event loop on every API call when
        the MT5 terminal is unavailable.
        """
        if mt5 is None:
            raise MT5Error("MetaTrader5 package not installed")
        if self._connected:
            # Trust _connected flag — the caller's MT5 operation will fail and
            # call _mark_disconnected() if the terminal actually dropped us.
            # Doing a synchronous liveness check here would block the async
            # event loop on every request.
            return

        # Circuit breaker — don't retry within cooldown window
        if self._last_failed_connect > 0:
            elapsed = time.monotonic() - self._last_failed_connect
            if elapsed < self.reconnect_cooldown:
                raise MT5Error(
                    f"MT5 not connected (cooldown {self.reconnect_cooldown - elapsed:.0f}s remaining)"
                )

        delays = [1, 2, 4, 8, 16]
        for attempt, delay in enumerate(delays, start=1):
            logfire.info("mt5.reconnect_attempt", attempt=attempt, delay_s=delay)
            if self.connect():
                self._last_failed_connect = 0.0
                return
            if attempt < len(delays):
                time.sleep(delay)
        self._last_failed_connect = time.monotonic()
        raise MT5Error(
            f"Could not connect to MT5 after {len(delays)} attempts "
            f"(login={self.login}, server={self.server})"
        )

    def _mark_disconnected(self) -> None:
        """Call when an MT5 operation returns None unexpectedly — marks for reconnect."""
        if self._connected:
            self._connected = False
            self._last_failed_connect = time.monotonic()
            logfire.warn("mt5.connection_lost_detected")

    def disconnect(self) -> None:
        if mt5 is not None and self._connected:
            mt5.shutdown()
            self._connected = False
            logfire.info("mt5.disconnected")

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def account_info(self) -> dict:
        """Return account equity, balance, margin, margin_level, free_margin, currency."""
        self._ensure_connected()
        try:
            info = mt5.account_info()
            if info is None:
                self._mark_disconnected()
                err = mt5.last_error()
                raise MT5Error(f"account_info failed: {err}")
            return {
                "equity": info.equity,
                "balance": info.balance,
                "margin": info.margin,
                "margin_level": info.margin_level,
                "free_margin": info.margin_free,
                "currency": info.currency,
                "leverage": info.leverage,
                "login": info.login,
                "server": info.server,
            }
        except MT5Error:
            raise
        except Exception as exc:
            raise MT5Error(f"account_info unexpected error: {exc}") from exc

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_positions(self) -> list[dict]:
        """Return list of open positions as dicts."""
        self._ensure_connected()
        try:
            positions = mt5.positions_get()
            if positions is None:
                err = mt5.last_error()
                # Error 1 = success with no positions
                if err[0] == 1:
                    return []
                self._mark_disconnected()
                raise MT5Error(f"positions_get failed: {err}")
            return [
                {
                    "ticket": p.ticket,
                    "symbol": p.symbol,
                    "type": p.type,          # 0=buy, 1=sell
                    "lots": p.volume,
                    "price_open": p.price_open,
                    "price_current": p.price_current,
                    "profit": p.profit,
                    "sl": p.sl,
                    "tp": p.tp,
                    "comment": p.comment,
                    "magic": p.magic,
                    "time": datetime.fromtimestamp(p.time, tz=timezone.utc),
                }
                for p in positions
            ]
        except MT5Error:
            raise
        except Exception as exc:
            raise MT5Error(f"get_positions unexpected error: {exc}") from exc

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> dict:
        """Return bid, ask, mid, spread_bps, ts for the symbol."""
        self._ensure_connected()
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                err = mt5.last_error()
                raise MT5Error(f"symbol_info_tick({symbol}) failed: {err}")
            bid = tick.bid
            ask = tick.ask
            mid = (bid + ask) / 2.0
            spread_bps = ((ask - bid) / mid * 10_000) if mid > 0 else 0.0
            ts = datetime.fromtimestamp(tick.time, tz=timezone.utc)
            return {
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread_bps": round(spread_bps, 3),
                "ts": ts.isoformat(),
            }
        except MT5Error:
            raise
        except Exception as exc:
            raise MT5Error(f"get_quote({symbol}) unexpected error: {exc}") from exc

    def get_rates(self, symbol: str, n_bars: int = 300) -> pd.DataFrame:
        """Return last n_bars of 15-minute OHLCV bars as a DataFrame.

        Columns: ts (UTC, tz-aware), open, high, low, close, volume
        """
        self._ensure_connected()
        try:
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, n_bars)
            if rates is None or len(rates) == 0:
                err = mt5.last_error()
                raise MT5Error(f"copy_rates_from_pos({symbol}) failed: {err}")
            df = pd.DataFrame(rates)
            # MT5 returns time as POSIX timestamps (UTC)
            df["ts"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df.rename(columns={"tick_volume": "volume"})[
                ["ts", "open", "high", "low", "close", "volume"]
            ]
            return df.reset_index(drop=True)
        except MT5Error:
            raise
        except Exception as exc:
            raise MT5Error(f"get_rates({symbol}) unexpected error: {exc}") from exc

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        self._ensure_connected()
        info = mt5.symbol_info(symbol) if mt5 else None
        return SymbolSpec.from_mt5_info(symbol, info)

    def place_market_order(
        self,
        symbol: str,
        side: int,
        lots: float,
        sl_price: float = 0.0,
        tp_price: float = 0.0,
        comment: str = "",
        magic: int = 42,
    ) -> OrderFill | None:
        """Place a market order. Returns fill details on success, None on failure."""
        self._ensure_connected()
        try:
            order_type = mt5.ORDER_TYPE_BUY if side == 1 else mt5.ORDER_TYPE_SELL
            sym_info = mt5.symbol_info(symbol)
            volume = normalize_volume(lots, sym_info)
            if volume <= 0:
                self.last_order_error = f"volume {lots} normalizes to 0 for {symbol}"
                logfire.error("mt5.invalid_volume", symbol=symbol, requested=lots)
                return None

            last_result = None
            for attempt in range(ORDER_MAX_ATTEMPTS):
                tick = mt5.symbol_info_tick(symbol)
                if tick is None:
                    err = mt5.last_error()
                    raise MT5Error(f"Cannot get tick for {symbol}: {err}")
                price = tick.ask if side == 1 else tick.bid
                deviation = order_deviation_points(symbol, sym_info, price)

                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": volume,
                    "type": order_type,
                    "price": price,
                    "sl": sl_price if sl_price > 0 else 0.0,
                    "tp": tp_price if tp_price > 0 else 0.0,
                    "deviation": deviation,
                    "magic": magic,
                    "comment": comment[:31],
                    "type_time": mt5.ORDER_TIME_GTC,
                }

                logfire.info(
                    "mt5.order_send",
                    symbol=symbol,
                    side="buy" if side == 1 else "sell",
                    lots=volume,
                    requested_lots=lots,
                    price=price,
                    deviation=deviation,
                    attempt=attempt + 1,
                    sl=sl_price,
                    tp=tp_price,
                    comment=comment,
                )

                result = self._send_with_fillings(request)
                last_result = result
                if result is None:
                    err = mt5.last_error()
                    self.last_order_error = str(err)
                    logfire.warn(
                        "mt5.order_send_none",
                        symbol=symbol,
                        error=err,
                        attempt=attempt + 1,
                    )
                    if attempt + 1 < ORDER_MAX_ATTEMPTS:
                        retry_sleep(attempt)
                        continue
                    return None

                if result.retcode == mt5.TRADE_RETCODE_DONE:
                    self.last_order_error = ""
                    logfire.info(
                        "mt5.order_filled",
                        ticket=result.order,
                        symbol=symbol,
                        side="buy" if side == 1 else "sell",
                        lots=volume,
                        price=result.price,
                        attempt=attempt + 1,
                    )
                    return OrderFill(
                        ticket=int(result.order),
                        lots=float(volume),
                        price=float(result.price),
                        requested_lots=float(lots),
                    )

                self.last_order_error = f"{result.retcode} {result.comment}"
                if is_retryable_retcode(result.retcode) and attempt + 1 < ORDER_MAX_ATTEMPTS:
                    logfire.warn(
                        "mt5.order_retry",
                        symbol=symbol,
                        retcode=result.retcode,
                        comment=result.comment,
                        attempt=attempt + 1,
                    )
                    retry_sleep(attempt)
                    continue

                logfire.error(
                    "mt5.order_rejected",
                    symbol=symbol,
                    retcode=result.retcode,
                    comment=result.comment,
                    lots=volume,
                    deviation=deviation,
                )
                return None

            if last_result is not None:
                self.last_order_error = f"{last_result.retcode} {last_result.comment}"
            return None

        except MT5Error:
            raise
        except Exception as exc:
            raise MT5Error(f"place_market_order({symbol}) unexpected error: {exc}") from exc

    # ------------------------------------------------------------------
    # Pending (limit/stop) orders — maker / passive execution
    # ------------------------------------------------------------------

    _PENDING_TYPES = {
        "buy_limit": "ORDER_TYPE_BUY_LIMIT",
        "sell_limit": "ORDER_TYPE_SELL_LIMIT",
        "buy_stop": "ORDER_TYPE_BUY_STOP",
        "sell_stop": "ORDER_TYPE_SELL_STOP",
    }

    def place_pending_order(
        self,
        symbol: str,
        kind: str,
        lots: float,
        price: float,
        sl_price: float = 0.0,
        tp_price: float = 0.0,
        comment: str = "",
        magic: int = 42,
        expiry_s: int = 0,
    ) -> int | None:
        """Place a pending order (buy_limit/sell_limit/buy_stop/sell_stop).

        Returns the pending-order ticket on success, None on rejection.
        expiry_s > 0 sets a broker-side expiry (ORDER_TIME_SPECIFIED); else GTC.
        """
        self._ensure_connected()
        kind = kind.lower()
        if kind not in self._PENDING_TYPES:
            raise MT5Error(f"unknown pending kind: {kind}")
        try:
            order_type = getattr(mt5, self._PENDING_TYPES[kind])
            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": symbol,
                "volume": round(lots, 2),
                "type": order_type,
                "price": float(price),
                "sl": sl_price if sl_price > 0 else 0.0,
                "tp": tp_price if tp_price > 0 else 0.0,
                "magic": magic,
                "comment": comment[:31],
            }
            if expiry_s and expiry_s > 0:
                request["type_time"] = mt5.ORDER_TIME_SPECIFIED
                request["expiration"] = int(time.time()) + int(expiry_s)
            else:
                request["type_time"] = mt5.ORDER_TIME_GTC

            result = self._send_with_fillings(request)
            if result is None:
                err = mt5.last_error()
                self.last_order_error = str(err)
                logfire.error("mt5.pending_send_none", symbol=symbol, kind=kind, error=err)
                return None
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                self.last_order_error = f"{result.retcode} {result.comment}"
                logfire.error(
                    "mt5.pending_rejected", symbol=symbol, kind=kind,
                    retcode=result.retcode, comment=result.comment, price=price, lots=lots,
                )
                return None
            self.last_order_error = ""
            logfire.info(
                "mt5.pending_placed", ticket=result.order, symbol=symbol,
                kind=kind, lots=lots, price=price,
            )
            return result.order
        except MT5Error:
            raise
        except Exception as exc:
            raise MT5Error(f"place_pending_order({symbol},{kind}) error: {exc}") from exc

    def cancel_order(self, ticket: int) -> bool:
        """Delete a pending order by ticket. Returns True on success."""
        self._ensure_connected()
        try:
            request = {"action": mt5.TRADE_ACTION_REMOVE, "order": int(ticket)}
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                retcode = result.retcode if result else "None"
                comment = result.comment if result else str(mt5.last_error())
                logfire.warn("mt5.cancel_failed", ticket=ticket, retcode=retcode, comment=comment)
                return False
            logfire.info("mt5.order_cancelled", ticket=ticket)
            return True
        except Exception as exc:
            raise MT5Error(f"cancel_order({ticket}) error: {exc}") from exc

    def get_orders(self) -> list[dict]:
        """Return pending orders (not positions) as dicts."""
        self._ensure_connected()
        try:
            orders = mt5.orders_get()
            if orders is None:
                err = mt5.last_error()
                if err[0] == 1:
                    return []
                self._mark_disconnected()
                raise MT5Error(f"orders_get failed: {err}")
            return [
                {
                    "ticket": o.ticket,
                    "symbol": o.symbol,
                    "type": o.type,
                    "lots": o.volume_current,
                    "price_open": o.price_open,
                    "price_current": o.price_current,
                    "sl": o.sl,
                    "tp": o.tp,
                    "comment": o.comment,
                    "magic": o.magic,
                    "time_setup": datetime.fromtimestamp(o.time_setup, tz=timezone.utc).isoformat(),
                }
                for o in orders
            ]
        except MT5Error:
            raise
        except Exception as exc:
            raise MT5Error(f"get_orders error: {exc}") from exc

    def cancel_all_orders(self, symbol: str | None = None, magic: int | None = None) -> int:
        """Cancel all pending orders (optionally filtered by symbol/magic)."""
        cancelled = 0
        for o in self.get_orders():
            if symbol is not None and o["symbol"] != symbol:
                continue
            if magic is not None and o["magic"] != magic:
                continue
            try:
                if self.cancel_order(o["ticket"]):
                    cancelled += 1
            except MT5Error:
                pass
        return cancelled

    def close_position(self, ticket: int) -> bool:
        """Close a specific position by ticket. Returns True on success."""
        self._ensure_connected()
        try:
            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                err = mt5.last_error()
                logfire.warn("mt5.close_position_not_found", ticket=ticket, error=err)
                return False

            pos = positions[0]
            # Opposite type to close
            close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
            tick = mt5.symbol_info_tick(pos.symbol)
            if tick is None:
                raise MT5Error(f"Cannot get tick for {pos.symbol}")
            price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask
            sym_info = mt5.symbol_info(pos.symbol)
            deviation = order_deviation_points(pos.symbol, sym_info, price)

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": pos.volume,
                "type": close_type,
                "position": ticket,
                "price": price,
                "deviation": deviation,
                "magic": pos.magic,
                "comment": f"close_{ticket}",
                "type_time": mt5.ORDER_TIME_GTC,
            }

            logfire.info(
                "mt5.close_position",
                ticket=ticket,
                symbol=pos.symbol,
                lots=pos.volume,
                price=price,
            )

            result = self._send_with_fillings(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                retcode = result.retcode if result else "None"
                comment = result.comment if result else str(mt5.last_error())
                logfire.error(
                    "mt5.close_position_failed",
                    ticket=ticket,
                    retcode=retcode,
                    comment=comment,
                )
                return False

            logfire.info("mt5.position_closed", ticket=ticket, symbol=pos.symbol)
            return True

        except MT5Error:
            raise
        except Exception as exc:
            raise MT5Error(f"close_position({ticket}) unexpected error: {exc}") from exc

    def close_all_positions(self) -> int:
        """Close all open positions. Returns count of positions closed."""
        self._ensure_connected()
        try:
            positions = mt5.positions_get()
            if positions is None:
                return 0
            closed = 0
            for pos in positions:
                try:
                    if self.close_position(pos.ticket):
                        closed += 1
                except MT5Error as exc:
                    logfire.error(
                        "mt5.close_all_error",
                        ticket=pos.ticket,
                        symbol=pos.symbol,
                        error=str(exc),
                    )
            logfire.info("mt5.close_all_done", closed=closed, total=len(positions))
            return closed
        except MT5Error:
            raise
        except Exception as exc:
            raise MT5Error(f"close_all_positions unexpected error: {exc}") from exc
