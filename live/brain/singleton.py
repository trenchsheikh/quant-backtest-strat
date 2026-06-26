"""Ensure only one brain loop process runs at a time."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from filelock import FileLock, Timeout


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def acquire_process_lock(state_dir: Path, name: str = "brain") -> FileLock:
    """Acquire exclusive process lock or exit if another instance holds it."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / f"{name}.lock"
    pid_path = state_dir / f"{name}.pid"
    lock = FileLock(str(lock_path))

    try:
        lock.acquire(timeout=0)
    except Timeout:
        if pid_path.exists():
            try:
                old_pid = int(pid_path.read_text(encoding="utf-8").strip())
            except ValueError:
                old_pid = 0
            if _pid_alive(old_pid):
                print(
                    f"[{name}] Another instance is running (PID {old_pid}). Exiting.",
                    file=sys.stderr,
                )
                sys.exit(0)
        lock.acquire(timeout=0)

    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    return lock


def acquire_brain_lock(state_dir: Path) -> FileLock:
    """Acquire exclusive 15m bar brain lock."""
    return acquire_process_lock(state_dir, "brain")


def read_process_pid(state_dir: Path, name: str) -> int | None:
    pid_path = state_dir / f"{name}.pid"
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def stop_process(state_dir: Path, name: str) -> bool:
    """Terminate a locked process by pid file. Returns True if a kill was attempted."""
    pid = read_process_pid(state_dir, name)
    if pid is None or not _pid_alive(pid):
        return False
    _kill_pid(pid)
    return True


def _kill_pid(pid: int) -> None:
    if pid <= 0:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], check=False, capture_output=True)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def stop_module_processes(module_fragment: str) -> int:
    """Kill every running interpreter whose command line contains ``module_fragment``.

    Returns the number of processes a kill was attempted on (including stale pid-file
    holder). Use before starting a loop to clear orphaned consoles that lost the lock.
    """
    killed: set[int] = set()

    if sys.platform == "win32":
        try:
            result = subprocess.run(
                [
                    "wmic",
                    "process",
                    "where",
                    f"CommandLine like '%{module_fragment}%'",
                    "get",
                    "ProcessId",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    killed.add(int(line))
        except Exception:
            pass
    else:
        try:
            result = subprocess.run(
                ["pgrep", "-f", module_fragment],
                capture_output=True,
                text=True,
                check=False,
            )
            for line in result.stdout.splitlines():
                if line.strip().isdigit():
                    killed.add(int(line))
        except Exception:
            pass

    for pid in killed:
        _kill_pid(pid)
    return len(killed)
