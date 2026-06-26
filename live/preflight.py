"""Pre-flight checks. Run before starting the live system.

Usage:
    python -m live.preflight
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from live.config import Settings, ROUND_SCHEDULE, ALL_SYMBOLS
from live.state.store import StateStore


def run_preflight() -> bool:
    """Run all pre-flight checks. Returns True if system is safe to start."""
    settings = Settings()
    bridge_url = f"http://{settings.BRIDGE_HOST}:{settings.BRIDGE_PORT}"
    passed = 0
    failed = 0

    def check(name: str, ok: bool, detail: str = "", warn: bool = False) -> bool:
        nonlocal passed, failed
        if not ok:
            tag = "FAIL"
            failed += 1
        elif warn:
            tag = "WARN"
            passed += 1
        else:
            tag = "PASS"
            passed += 1
        detail_str = f": {detail}" if detail else ""
        print(f"  [{tag}] {name}{detail_str}")
        return ok

    print("=" * 60)
    print("MoMQ Live System — Pre-flight Checks")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    # ------------------------------------------------------------------
    # 1. Kill switch file
    # ------------------------------------------------------------------
    print("\n  [Startup Safety]")
    kill_exists = settings.KILL_SWITCH_FILE.exists()
    check(
        "Kill switch clear",
        not kill_exists,
        f"Kill file exists at {settings.KILL_SWITCH_FILE} — remove it to proceed"
        if kill_exists else "",
    )

    # ------------------------------------------------------------------
    # 2. Bridge + MT5 connectivity
    # ------------------------------------------------------------------
    print("\n  [Bridge + MT5]")
    equity = 0.0
    try:
        r = httpx.get(f"{bridge_url}/health", timeout=5)
        health = r.json()
        check("Bridge connectivity", r.status_code == 200, f"HTTP {r.status_code}")
        check("MT5 connected", health.get("mt5_connected", False), health.get("status", ""))
        equity = health.get("equity", 0.0)
        check("Account equity >= 900k", equity >= 900_000, f"${equity:,.0f}")
        check(
            "Account equity >= 950k (healthy buffer)",
            equity >= 950_000,
            f"${equity:,.0f}",
            warn=True,
        )
    except Exception as exc:
        check("Bridge connectivity", False, str(exc))
        check("MT5 connected", False, "Bridge unreachable")
        check("Account equity >= 900k", False, "Bridge unreachable")
        check("Account equity >= 950k", False, "Bridge unreachable")

    # ------------------------------------------------------------------
    # 3. Symbol quotes + spreads
    # ------------------------------------------------------------------
    print(f"\n  [Symbols — checking {len(ALL_SYMBOLS)} instruments]")
    for sym in ALL_SYMBOLS:
        try:
            r = httpx.get(f"{bridge_url}/quote/{sym}", timeout=5)
            if r.status_code == 200:
                q = r.json()
                spread = float(q.get("spread_bps", 999))
                is_crypto = any(c in sym for c in ["BTC", "ETH", "SOL", "XRP", "BAR"])
                max_spread = 200.0 if is_crypto else 20.0
                spread_ok = spread < max_spread
                check(
                    f"  {sym} quote + spread",
                    spread_ok,
                    f"spread={spread:.1f}bps (limit {'200' if is_crypto else '20'}bps)",
                )
            else:
                check(f"  {sym} quote", False, f"HTTP {r.status_code}")
        except Exception as exc:
            check(f"  {sym} quote", False, str(exc))

    # ------------------------------------------------------------------
    # 4. Historical bars (need >= 300 for signal warmup)
    # ------------------------------------------------------------------
    print("\n  [Historical bar depth]")
    for sym in ALL_SYMBOLS[:3]:  # Spot-check 3 symbols
        try:
            r = httpx.get(f"{bridge_url}/rates/{sym}", params={"limit": 300}, timeout=10)
            if r.status_code == 200:
                bars = r.json()
                n = len(bars)
                check(
                    f"  {sym} bar depth",
                    n >= 200,
                    f"{n} bars (need >= 200 for warmup)",
                )
            else:
                check(f"  {sym} bar depth", False, f"HTTP {r.status_code}")
        except Exception as exc:
            check(f"  {sym} bar depth", False, str(exc))

    # ------------------------------------------------------------------
    # 5. Round calendar
    # ------------------------------------------------------------------
    now = datetime.now(timezone.utc)
    print(f"\n  [Round Calendar — now = {now.strftime('%Y-%m-%d %H:%M UTC')}]")
    active_round: str | None = None
    for kind, t0, t1 in ROUND_SCHEDULE:
        if t0 <= now < t1:
            bar_i = int((now - t0).total_seconds() / 900)
            elapsed_h = (now - t0).total_seconds() / 3600
            remaining_h = (t1 - now).total_seconds() / 3600
            print(
                f"  [ACTIVE] {kind.upper():6s}: "
                f"{t0.strftime('%b %d %H:%M')} -> {t1.strftime('%b %d %H:%M')} UTC  "
                f"(bar {bar_i}, elapsed {elapsed_h:.1f}h, remaining {remaining_h:.1f}h)"
            )
            active_round = kind
        elif now < t0:
            delta = t0 - now
            total_secs = int(delta.total_seconds())
            h = total_secs // 3600
            m = (total_secs % 3600) // 60
            print(
                f"  [FUTURE] {kind.upper():6s}: "
                f"{t0.strftime('%b %d %H:%M')} -> {t1.strftime('%b %d %H:%M')} UTC  "
                f"(starts in {h}h {m}m)"
            )
        else:
            duration_h = (t1 - t0).total_seconds() / 3600
            print(
                f"  [DONE  ] {kind.upper():6s}: "
                f"{t0.strftime('%b %d %H:%M')} -> {t1.strftime('%b %d %H:%M')} UTC  "
                f"({duration_h:.0f}h round)"
            )

    if active_round is None:
        print("  [IDLE  ] No active round — system will sit flat until next round")

    # ------------------------------------------------------------------
    # 6. Saved state inspection
    # ------------------------------------------------------------------
    print("\n  [Saved State]")
    try:
        store = StateStore(settings.STATE_DIR)
        state = store.load()
        print(
            f"  Saved state: round={state.round_kind}  bar={state.bar_i}  "
            f"positions={len(state.positions)}  "
            f"realized_pnl=${state.realized_pnl:,.2f}"
        )
        if state.kill_switch:
            check(
                "Saved kill switch clear",
                False,
                "kill_switch=True in state — clear state file or remove kill.flag",
            )
        else:
            check("Saved kill switch clear", True)

        if state.positions:
            print(f"  WARNING: {len(state.positions)} saved positions will be reconciled on startup:")
            for p in state.positions:
                print(
                    f"    ticket={p.ticket}  {p.symbol}  "
                    f"{'LONG' if p.side == 1 else 'SHORT'}  {p.lots}L  "
                    f"sleeve={p.sleeve}  bars_held={p.bars_held}"
                )
    except FileNotFoundError:
        print("  Saved state: not found (fresh start)")
        check("Saved kill switch clear", True, "fresh state")
    except Exception as exc:
        print(f"  Saved state: ERROR — {exc}")
        check("Saved state readable", False, str(exc))

    # ------------------------------------------------------------------
    # 7. momq package import
    # ------------------------------------------------------------------
    print("\n  [momq Package]")
    try:
        from momq.signals import signal_coc_open, signal_crypto_momentum, signal_css_spread
        check("momq.signals importable", True)
    except ImportError as exc:
        check("momq.signals importable", False, str(exc))
    try:
        from momq.tournament_config import TournamentConfig
        check("momq.tournament_config importable", True)
    except ImportError as exc:
        check("momq.tournament_config importable", False, str(exc))

    # ------------------------------------------------------------------
    # 8. Entry gate — interactive confirmation if ENTRY_ENABLED=true
    # ------------------------------------------------------------------
    print()
    if settings.ENTRY_ENABLED:
        print("  " + "!" * 56)
        print("  !!! ENTRY IS ENABLED — THIS SYSTEM WILL PLACE REAL ORDERS !!!")
        print("  " + "!" * 56)
        try:
            answer = input("  Type CONFIRM to proceed, anything else to abort: ").strip()
        except EOFError:
            answer = ""
        if answer != "CONFIRM":
            print("  Aborted by operator.")
            sys.exit(1)
        check("Entry gate confirmed by operator", True, "ENTRY_ENABLED=true — LIVE mode")
    else:
        check("Entry gate", True, "ENTRY_ENABLED=false — paper mode (safe to start)")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print(f"\n{'=' * 60}")
    print(f"  Result: {passed} passed, {failed} failed")
    print("=" * 60)

    if failed > 0:
        print("  PRE-FLIGHT FAILED. Fix the above issues before starting.")
        return False

    print("  PRE-FLIGHT PASSED.")
    print("  Safe to start: python live/watchdog.py")
    return True


if __name__ == "__main__":
    ok = run_preflight()
    sys.exit(0 if ok else 1)
