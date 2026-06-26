"""MoMQ Bridge — FastAPI server exposing MT5 operations over HTTP.

Runs on 127.0.0.1:8765. Only the brain process communicates with it.
Every order is gated by ENTRY_ENABLED and the kill switch.

Usage:
    uvicorn live.bridge.app:app --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import logfire
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from live.bridge.mt5_client import MT5Client, MT5Error
from live.bridge.symbol_specs import SymbolSpec
from live.config import Settings, ALL_SYMBOLS

# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------
_mt5: MT5Client | None = None
_settings: Settings | None = None
_kill_switch: bool = False


def _get_mt5() -> MT5Client:
    if _mt5 is None:
        raise HTTPException(status_code=503, detail="MT5 client not initialised")
    if not _mt5._connected:
        raise HTTPException(status_code=503, detail="MT5 not connected")
    return _mt5


def _get_settings() -> Settings:
    if _settings is None:
        raise HTTPException(status_code=503, detail="Settings not loaded")
    return _settings


def _kill_switch_active() -> bool:
    """Sync in-memory kill flag with live_state/kill.flag (file may change at runtime)."""
    global _kill_switch
    settings = _settings
    if settings is not None and settings.KILL_SWITCH_FILE.exists():
        _kill_switch = True
    elif settings is not None and not settings.KILL_SWITCH_FILE.exists():
        _kill_switch = False
    return _kill_switch


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _mt5, _settings, _kill_switch

    # Load settings
    _settings = Settings()

    # Configure logfire
    if _settings.LOGFIRE_TOKEN:
        logfire.configure(token=_settings.LOGFIRE_TOKEN, service_name="momq-bridge")
    else:
        logfire.configure(send_to_logfire=False, service_name="momq-bridge")

    logfire.info(
        "bridge.startup",
        entry_enabled=_settings.ENTRY_ENABLED,
        bridge_port=_settings.BRIDGE_PORT,
    )

    # Load kill switch state from file
    if _settings.KILL_SWITCH_FILE.exists():
        _kill_switch = True
        logfire.warn("bridge.kill_switch_active_on_startup", file=str(_settings.KILL_SWITCH_FILE))

    # Connect MT5
    _mt5 = MT5Client(
        login=_settings.MT5_LOGIN,
        password=_settings.MT5_PASSWORD.get_secret_value(),
        server=_settings.MT5_SERVER,
        terminal_path=_settings.MT5_TERMINAL_PATH,
    )
    try:
        connected = _mt5.connect()
        if not connected:
            logfire.error("bridge.mt5_connect_failed_on_startup")
    except MT5Error as exc:
        logfire.error("bridge.mt5_connect_error", error=str(exc))

    yield

    # Shutdown
    if _mt5 is not None:
        _mt5.disconnect()
    logfire.info("bridge.shutdown")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="MoMQ Bridge", version="1.0.0", lifespan=lifespan)
logfire.instrument_fastapi(app)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str
    mt5_connected: bool
    mt5_trade_allowed: bool
    kill_switch: bool
    entry_enabled: bool
    equity: float
    ts: str


class AccountInfo(BaseModel):
    equity: float
    balance: float
    margin: float
    margin_level: float
    free_margin: float
    currency: str
    leverage: int
    login: int
    server: str


class Quote(BaseModel):
    symbol: str
    bid: float
    ask: float
    mid: float
    spread_bps: float
    ts: str


class Position(BaseModel):
    ticket: int
    symbol: str
    type: int           # 0=buy, 1=sell
    lots: float
    price_open: float
    price_current: float
    profit: float
    sl: float
    tp: float
    comment: str
    magic: int
    time: str


class SymbolSpecResponse(BaseModel):
    symbol: str
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float


class OrderRequest(BaseModel):
    symbol: str
    side: int           # +1 buy, -1 sell
    lots: float
    sl_price: float = 0.0
    tp_price: float = 0.0
    comment: str = ""
    magic: int = 20260621


class OrderResult(BaseModel):
    ticket: Optional[int] = None
    paper: bool = False
    ok: bool
    message: str
    lots: Optional[float] = None
    fill_price: Optional[float] = None
    requested_lots: Optional[float] = None


class LimitRequest(BaseModel):
    symbol: str
    kind: str           # buy_limit | sell_limit | buy_stop | sell_stop
    lots: float
    price: float
    sl_price: float = 0.0
    tp_price: float = 0.0
    comment: str = ""
    magic: int = 20260624
    expiry_s: int = 0


class PendingOrder(BaseModel):
    ticket: int
    symbol: str
    type: int
    lots: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    comment: str
    magic: int
    time_setup: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness + status check. Returns immediately using cached MT5 state."""
    settings = _get_settings()
    connected = _mt5._connected if _mt5 is not None else False
    equity = 0.0
    if connected and _mt5 is not None:
        try:
            info = _mt5.account_info()
            equity = float(info["equity"])
        except MT5Error:
            connected = False

    return HealthResponse(
        status="ok" if connected else "degraded",
        mt5_connected=connected,
        mt5_trade_allowed=_mt5.terminal_trade_allowed() if connected and _mt5 else False,
        kill_switch=_kill_switch_active(),
        entry_enabled=settings.ENTRY_ENABLED,
        equity=equity,
        ts=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/account", response_model=AccountInfo)
async def account() -> AccountInfo:
    """Return live account metrics."""
    mt5 = _get_mt5()
    try:
        info = mt5.account_info()
        return AccountInfo(**info)
    except MT5Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/symbols/specs", response_model=list[SymbolSpecResponse])
async def symbol_specs() -> list[SymbolSpecResponse]:
    """Broker contract size and volume limits per symbol."""
    mt5 = _get_mt5()
    out: list[SymbolSpecResponse] = []
    try:
        for sym in ALL_SYMBOLS:
            spec = mt5.symbol_spec(sym)
            out.append(SymbolSpecResponse(
                symbol=spec.symbol,
                contract_size=spec.contract_size,
                volume_min=spec.volume_min,
                volume_max=spec.volume_max,
                volume_step=spec.volume_step,
            ))
    except MT5Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return out


@app.get("/positions", response_model=list[Position])
async def positions() -> list[Position]:
    """Return all open positions."""
    mt5 = _get_mt5()
    try:
        raw = mt5.get_positions()
        return [
            Position(
                ticket=p["ticket"],
                symbol=p["symbol"],
                type=p["type"],
                lots=p["lots"],
                price_open=p["price_open"],
                price_current=p["price_current"],
                profit=p["profit"],
                sl=p["sl"],
                tp=p["tp"],
                comment=p["comment"],
                magic=p["magic"],
                time=p["time"].isoformat() if isinstance(p["time"], datetime) else str(p["time"]),
            )
            for p in raw
        ]
    except MT5Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/quote/{symbol}", response_model=Quote)
async def quote(symbol: str) -> Quote:
    """Return current bid/ask/mid/spread for a symbol."""
    mt5 = _get_mt5()
    try:
        q = mt5.get_quote(symbol)
        return Quote(**q)
    except MT5Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/rates/{symbol}", response_model=list[dict])
async def rates(symbol: str, limit: int = Query(default=200, ge=1, le=1000)) -> list[dict]:
    """Return last `limit` 15-minute OHLCV bars for a symbol."""
    mt5 = _get_mt5()
    try:
        df = mt5.get_rates(symbol, n_bars=limit)
        # Convert timestamps to ISO strings for JSON serialisation
        df["ts"] = df["ts"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        return df.to_dict(orient="records")
    except MT5Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/order", response_model=OrderResult)
async def place_order(req: OrderRequest) -> OrderResult:
    """Place a market order.

    Gated by kill switch and ENTRY_ENABLED. If either blocks execution,
    returns a paper result without touching MT5.
    """
    settings = _get_settings()

    if _kill_switch_active():
        logfire.warn("bridge.order_blocked_kill_switch", symbol=req.symbol, lots=req.lots)
        return OrderResult(
            ticket=None,
            paper=True,
            ok=False,
            message="Kill switch active — order blocked",
        )

    if not settings.ENTRY_ENABLED:
        logfire.info(
            "bridge.order_paper",
            symbol=req.symbol,
            side=req.side,
            lots=req.lots,
            comment=req.comment,
        )
        return OrderResult(
            ticket=None,
            paper=True,
            ok=True,
            message=f"PAPER: would {('buy' if req.side == 1 else 'sell')} {req.lots} lots {req.symbol}",
        )

    mt5 = _get_mt5()
    try:
        fill = mt5.place_market_order(
            symbol=req.symbol,
            side=req.side,
            lots=req.lots,
            sl_price=req.sl_price,
            tp_price=req.tp_price,
            comment=req.comment,
            magic=req.magic,
        )
        if fill is None:
            detail = getattr(mt5, "last_order_error", "") or "Order rejected by MT5"
            return OrderResult(ticket=None, paper=False, ok=False, message=detail)
        return OrderResult(
            ticket=fill.ticket,
            paper=False,
            ok=True,
            message=f"Filled ticket={fill.ticket}",
            lots=fill.lots,
            fill_price=fill.price,
            requested_lots=fill.requested_lots,
        )
    except MT5Error as exc:
        logfire.error("bridge.order_error", error=str(exc), symbol=req.symbol)
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/limit", response_model=OrderResult)
async def place_limit(req: LimitRequest) -> OrderResult:
    """Place a pending (limit/stop) order. Gated by kill switch and ENTRY_ENABLED."""
    settings = _get_settings()
    if _kill_switch_active():
        return OrderResult(ticket=None, paper=True, ok=False, message="Kill switch active — limit blocked")
    if not settings.ENTRY_ENABLED:
        return OrderResult(
            ticket=None, paper=True, ok=True,
            message=f"PAPER: {req.kind} {req.lots} {req.symbol} @ {req.price}",
        )
    mt5 = _get_mt5()
    try:
        ticket = mt5.place_pending_order(
            symbol=req.symbol, kind=req.kind, lots=req.lots, price=req.price,
            sl_price=req.sl_price, tp_price=req.tp_price, comment=req.comment,
            magic=req.magic, expiry_s=req.expiry_s,
        )
        if ticket is None:
            detail = getattr(mt5, "last_order_error", "") or "Pending rejected by MT5"
            return OrderResult(ticket=None, paper=False, ok=False, message=detail)
        return OrderResult(ticket=ticket, paper=False, ok=True, message=f"Pending {req.kind} ticket={ticket}")
    except MT5Error as exc:
        logfire.error("bridge.limit_error", error=str(exc), symbol=req.symbol)
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/orders", response_model=list[PendingOrder])
async def list_orders() -> list[PendingOrder]:
    """Return all pending (limit/stop) orders."""
    mt5 = _get_mt5()
    try:
        return [PendingOrder(**o) for o in mt5.get_orders()]
    except MT5Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/cancel/{ticket}", response_model=dict)
async def cancel_order_ep(ticket: int) -> dict:
    """Cancel a pending order by ticket."""
    mt5 = _get_mt5()
    try:
        ok = mt5.cancel_order(ticket)
        return {"ticket": ticket, "cancelled": ok}
    except MT5Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/cancel_all", response_model=dict)
async def cancel_all_ep(symbol: Optional[str] = None, magic: Optional[int] = None) -> dict:
    """Cancel all pending orders, optionally filtered by symbol/magic."""
    mt5 = _get_mt5()
    try:
        n = mt5.cancel_all_orders(symbol=symbol, magic=magic)
        return {"cancelled": n}
    except MT5Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/close/{ticket}", response_model=dict)
async def close_position(ticket: int) -> dict:
    """Close a specific position by ticket number."""
    mt5 = _get_mt5()
    try:
        ok = mt5.close_position(ticket)
        return {"ticket": ticket, "closed": ok}
    except MT5Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/close_all", response_model=dict)
async def close_all() -> dict:
    """Close all open positions. Always executes regardless of kill switch."""
    mt5 = _get_mt5()
    try:
        count = mt5.close_all_positions()
        logfire.info("bridge.close_all", count=count)
        return {"closed": count}
    except MT5Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/kill", response_model=dict)
async def kill() -> dict:
    """Activate kill switch: block all new orders and close all positions."""
    global _kill_switch
    settings = _get_settings()

    _kill_switch = True

    # Write kill flag file
    settings.KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    settings.KILL_SWITCH_FILE.write_text(
        f"kill activated at {datetime.now(timezone.utc).isoformat()}\n"
    )

    logfire.warn("bridge.kill_switch_activated")

    # Close all positions
    closed = 0
    mt5 = _mt5
    if mt5 is not None:
        try:
            closed = mt5.close_all_positions()
        except MT5Error as exc:
            logfire.error("bridge.kill_close_all_error", error=str(exc))

    return {
        "kill_switch": True,
        "positions_closed": closed,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
