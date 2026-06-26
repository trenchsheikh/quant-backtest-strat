"""Heartbeat monitor — runs in a daemon thread inside the brain process.

Polls the bridge every `interval_secs` seconds for account/position/health
data, logs structured events via logfire, and writes a heartbeat.json file
to disk for external monitoring (e.g. a simple cron alert).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import logfire


class Heartbeat:
    def __init__(
        self,
        bridge_url: str,
        state_dir: Path,
        interval_secs: int = 60,
        init_equity: float = 1_000_000.0,
    ) -> None:
        self.bridge_url = bridge_url
        self.state_dir = Path(state_dir)
        self.interval = interval_secs
        self.init_equity = init_equity
        self._client = httpx.Client(base_url=bridge_url, timeout=10.0)
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="heartbeat"
        )
        self._stop = threading.Event()

    def start(self) -> None:
        """Start the background heartbeat thread."""
        self._thread.start()
        logfire.info("heartbeat.started", interval_secs=self.interval)

    def stop(self) -> None:
        """Signal the heartbeat thread to stop."""
        self._stop.set()

    def _loop(self) -> None:
        """Main loop — fires a beat then waits for the interval."""
        while not self._stop.wait(self.interval):
            self._beat()

    def _beat(self) -> None:
        """Collect metrics from bridge and write them to logfire + heartbeat.json."""
        try:
            health_resp = self._client.get("/health")
            account_resp = self._client.get("/account")
            positions_resp = self._client.get("/positions")

            health: dict = health_resp.json() if health_resp.status_code == 200 else {}
            account: dict = account_resp.json() if account_resp.status_code == 200 else {}
            positions: list[dict] = (
                positions_resp.json() if positions_resp.status_code == 200 else []
            )

            equity = float(account.get("equity", 0.0))
            balance = float(account.get("balance", 0.0))
            n_pos = len(positions)

            # Rough gross notional estimate — assumes forex 100k per lot, adjusts for crypto
            # (exact lot sizing is done in the brain; this is for monitoring only)
            gross_notional = 0.0
            for p in positions:
                sym = p.get("symbol", "")
                lots = float(p.get("lots", 0.0))
                is_crypto = any(c in sym for c in ["BTC", "ETH", "SOL", "XRP", "BAR"])
                is_gold = "XAU" in sym
                is_silver = "XAG" in sym
                mid = float(p.get("price_current", p.get("price_open", 0.0)))
                if is_crypto:
                    gross_notional += lots * mid          # 1 coin per lot (approx)
                elif is_gold:
                    gross_notional += lots * 100 * mid    # 100 oz per lot
                elif is_silver:
                    gross_notional += lots * 5_000 * mid  # 5000 oz per lot
                else:
                    gross_notional += lots * 100_000      # standard forex lot

            leverage = gross_notional / self.init_equity if self.init_equity > 0 else 0.0
            drawdown_pct = (equity / self.init_equity - 1.0) * 100 if self.init_equity > 0 else 0.0

            beat: dict = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "equity": equity,
                "balance": balance,
                "drawdown_pct": round(drawdown_pct, 3),
                "n_positions": n_pos,
                "est_gross_notional": round(gross_notional, 2),
                "est_leverage": round(leverage, 3),
                "mt5_connected": health.get("mt5_connected", False),
                "kill_switch": health.get("kill_switch", False),
                "entry_enabled": health.get("entry_enabled", False),
            }

            # Structured log — always goes to logfire / stderr
            logfire.info("heartbeat", **beat)

            # Threshold warnings
            if leverage > 24.0:
                logfire.warn(
                    "heartbeat.leverage_warn",
                    leverage=round(leverage, 3),
                    threshold=24.0,
                )
            if leverage > 28.0:
                logfire.error(
                    "heartbeat.leverage_hard",
                    leverage=round(leverage, 3),
                    threshold=28.0,
                    note="RD PENALTY ZONE — reduce positions immediately",
                )
            if drawdown_pct < -5.0:
                logfire.warn(
                    "heartbeat.drawdown_warn",
                    drawdown_pct=round(drawdown_pct, 3),
                )
            if drawdown_pct < -10.0:
                logfire.error(
                    "heartbeat.drawdown_severe",
                    drawdown_pct=round(drawdown_pct, 3),
                    note="Consider activating kill switch",
                )
            if not health.get("mt5_connected", True):
                logfire.error("heartbeat.mt5_disconnected")
            if health.get("kill_switch", False):
                logfire.warn("heartbeat.kill_switch_active")

            # Write heartbeat file for external watchdog / alerting
            self.state_dir.mkdir(parents=True, exist_ok=True)
            hb_path = self.state_dir / "heartbeat.json"
            hb_path.write_text(json.dumps(beat, indent=2), encoding="utf-8")

        except httpx.ConnectError:
            logfire.error("heartbeat.bridge_unreachable", bridge_url=self.bridge_url)
            # Write degraded heartbeat file
            try:
                beat_err = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "error": "bridge_unreachable",
                    "mt5_connected": False,
                }
                hb_path = self.state_dir / "heartbeat.json"
                hb_path.write_text(json.dumps(beat_err, indent=2), encoding="utf-8")
            except Exception:
                pass

        except Exception as exc:
            logfire.error("heartbeat.error", error=str(exc), exc_info=True)
