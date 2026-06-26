"""Self-healing process manager for MoMQ live system.

Manages two child processes:
  bridge — FastAPI + MT5 (bridge/app.py via uvicorn)
  brain  — 15-min bar loop (brain/loop.py)

Restarts crashed processes with exponential backoff.
Handles SIGTERM/SIGINT cleanly.

Usage:
    python live/watchdog.py
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).parent.parent.resolve()
LIVE_DIR = Path(__file__).parent.resolve()
STATE_DIR = REPO_ROOT / "live_state"

BRIDGE_CMD: list[str] = [
    sys.executable, "-m", "uvicorn",
    "live.bridge.app:app",
    "--host", "127.0.0.1",
    "--port", "8765",
    "--log-level", "warning",
]
BRAIN_CMD: list[str] = [sys.executable, "-m", "live.brain.loop"]

HEALTH_URL = "http://127.0.0.1:8765/health"
HEALTH_INTERVAL = 10    # seconds between health checks
MAX_BACKOFF = 60        # max restart delay in seconds
BRIDGE_STARTUP_WAIT = 60  # max seconds to wait for bridge on first start


class ManagedProcess:
    def __init__(self, name: str, cmd: list[str]) -> None:
        self.name = name
        self.cmd = cmd
        self.proc: subprocess.Popen | None = None
        self.restarts: int = 0
        self.backoff: int = 5

    def start(self) -> None:
        log(f"Starting {self.name}...")
        env = {**os.environ}
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=str(REPO_ROOT),
            env=env,
        )
        # Write PID file
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        pid_path = STATE_DIR / f"{self.name}.pid"
        pid_path.write_text(str(self.proc.pid))
        log(f"  {self.name} started (PID {self.proc.pid})")

    def is_alive(self) -> bool:
        if self.proc is None:
            return False
        return self.proc.poll() is None

    def restart(self) -> None:
        self.restarts += 1
        exit_code = self.proc.returncode if self.proc and self.proc.poll() is not None else "?"
        log(
            f"  {self.name} died (exit={exit_code}, restart #{self.restarts}), "
            f"waiting {self.backoff}s..."
        )
        time.sleep(self.backoff)
        self.backoff = min(self.backoff * 2, MAX_BACKOFF)
        self.start()

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.is_alive():
            log(f"  Stopping {self.name} (PID {self.proc.pid})...")
            # Use taskkill /T on Windows to kill the whole process tree,
            # preventing orphaned bridge/brain processes on Ctrl+C.
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                    capture_output=True,
                )
            except Exception:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
                log(f"  {self.name} stopped.")
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        # Remove PID file
        pid_path = STATE_DIR / f"{self.name}.pid"
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass


def _clear_port(port: int) -> None:
    """Kill any process listening on the given port (Windows netstat approach)."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = int(parts[-1])
                if pid > 0:
                    log(f"  Clearing stale process PID {pid} from port {port}")
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True)
    except Exception:
        pass


def log(msg: str) -> None:
    print(f"[watchdog {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def bridge_healthy() -> bool:
    try:
        r = httpx.get(HEALTH_URL, timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    bridge = ManagedProcess("bridge", BRIDGE_CMD)
    brain = ManagedProcess("brain", BRAIN_CMD)

    shutdown = False

    def handle_signal(sig: int, frame) -> None:
        nonlocal shutdown
        log(f"Received signal {sig} — initiating graceful shutdown...")
        shutdown = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log("=" * 60)
    log("MoMQ Watchdog starting")
    log(f"  Repo root: {REPO_ROOT}")
    log(f"  State dir: {STATE_DIR}")
    log("=" * 60)

    # Kill any orphaned process on port 8765 before starting
    _clear_port(8765)

    # Start bridge first
    bridge.start()

    # Wait for bridge to become healthy before starting brain
    log(f"Waiting up to {BRIDGE_STARTUP_WAIT}s for bridge to become healthy...")
    bridge_ready = False
    for attempt in range(BRIDGE_STARTUP_WAIT // 2):
        if not bridge.is_alive():
            log("ERROR: Bridge died during startup!")
            # Attempt a restart and keep waiting
            bridge.restart()
        if bridge_healthy():
            log("Bridge is healthy — starting brain.")
            bridge_ready = True
            break
        time.sleep(2)

    if not bridge_ready:
        log(f"WARNING: Bridge did not become healthy in {BRIDGE_STARTUP_WAIT}s — starting brain anyway")

    brain.start()

    # Main supervision loop
    while not shutdown:
        time.sleep(HEALTH_INTERVAL)

        if shutdown:
            break

        # Check bridge
        if not bridge.is_alive():
            log("Bridge process died!")
            bridge.backoff = max(bridge.backoff, 5)
            bridge.restart()
            # Brief pause for bridge to initialise before brain tries to connect
            time.sleep(5)

        # Check brain
        if not brain.is_alive():
            log("Brain process died!")
            brain.backoff = max(brain.backoff, 5)
            # If bridge is also unhealthy, wait for it first
            if not bridge_healthy():
                log("Waiting for bridge to recover before restarting brain...")
                for _ in range(15):
                    if bridge_healthy():
                        log("Bridge recovered — restarting brain.")
                        break
                    time.sleep(2)
                else:
                    log("WARNING: Bridge still not healthy — restarting brain anyway")
            brain.restart()

    # Graceful shutdown — stop brain first, then bridge
    log("Watchdog shutting down...")
    brain.stop()
    # Small delay to let bridge finish any in-flight requests
    time.sleep(2)
    bridge.stop()
    log("All processes stopped. Goodbye.")


if __name__ == "__main__":
    main()
