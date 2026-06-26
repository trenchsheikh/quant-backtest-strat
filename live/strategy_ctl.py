"""Switch between 15m bar brain and BTC scalp loop; manage kill flag and processes.

Usage:
    py -3.10 -m live.strategy_ctl status
    py -3.10 -m live.strategy_ctl kill on|off
    py -3.10 -m live.strategy_ctl bar [--start]
    py -3.10 -m live.strategy_ctl scalp [--start]
    py -3.10 -m live.strategy_ctl stop
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from live.config import Settings
from live.strategy_mode import StrategyMode, get_strategy_mode, set_strategy_mode
from live.brain.singleton import read_process_pid, stop_process, stop_module_processes, _pid_alive

REPO_ROOT = Path(__file__).resolve().parent.parent
BRAIN_MODULE = "live.brain.loop"
SCALP_MODULE = "live.brain.btc_scalp_loop"


def _kill_on(settings: Settings) -> None:
    settings.KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    settings.KILL_SWITCH_FILE.write_text(
        f"halt at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n",
        encoding="utf-8",
    )
    print(f"kill.flag ON  ({settings.KILL_SWITCH_FILE})")


def _kill_off(settings: Settings) -> None:
    if settings.KILL_SWITCH_FILE.exists():
        settings.KILL_SWITCH_FILE.unlink()
    print("kill.flag OFF")


def _stop_scalp_all(settings: Settings) -> None:
    """Stop scalp via pid file and kill any orphaned btc_scalp_loop consoles."""
    stop_process(settings.STATE_DIR, "scalp")
    extra = stop_module_processes(SCALP_MODULE)
    if extra:
        print(f"stopped {extra} orphaned scalp process(es)")


def _stop_both(settings: Settings) -> None:
    if stop_process(settings.STATE_DIR, "brain"):
        print("stopped brain")
    else:
        print("brain not running")
    _stop_scalp_all(settings)


def _start_module(module: str) -> None:
    kwargs: dict = {"cwd": str(REPO_ROOT)}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    subprocess.Popen([sys.executable, "-m", module], **kwargs)
    print(f"started {module} (new console)")


def _safe_switch(settings: Settings, *, use_kill: bool) -> None:
    """Brief kill.flag while stopping processes so neither strategy fires orders."""
    if use_kill:
        _kill_on(settings)
        time.sleep(0.5)
    _stop_both(settings)
    if use_kill:
        time.sleep(0.5)
        _kill_off(settings)


def cmd_status(settings: Settings) -> None:
    mode = get_strategy_mode(settings)
    kill = settings.KILL_SWITCH_FILE.exists()
    brain_pid = read_process_pid(settings.STATE_DIR, "brain")
    scalp_pid = read_process_pid(settings.STATE_DIR, "scalp")
    brain_up = brain_pid is not None and _pid_alive(brain_pid)
    scalp_up = scalp_pid is not None and _pid_alive(scalp_pid)
    print(f"strategy.mode : {mode.value}")
    print(f"kill.flag     : {'ON' if kill else 'off'}")
    print(f"brain         : {'running pid=' + str(brain_pid) if brain_up else 'stopped'}")
    print(f"scalp         : {'running pid=' + str(scalp_pid) if scalp_up else 'stopped'}")


def cmd_kill(settings: Settings, state: str) -> None:
    if state == "on":
        _kill_on(settings)
        _stop_both(settings)
    else:
        _kill_off(settings)


def cmd_bar(settings: Settings, start: bool, safe: bool) -> None:
    _safe_switch(settings, use_kill=safe)
    set_strategy_mode(StrategyMode.BAR, settings)
    print("strategy.mode -> bar")
    if start:
        _start_module(BRAIN_MODULE)


def cmd_scalp(settings: Settings, start: bool, safe: bool) -> None:
    if safe:
        _kill_on(settings)
        time.sleep(0.5)
    _stop_scalp_all(settings)
    if stop_process(settings.STATE_DIR, "brain"):
        print("stopped brain")
    if safe:
        time.sleep(0.5)
        _kill_off(settings)
    set_strategy_mode(StrategyMode.SCALP, settings)
    print("strategy.mode -> scalp")
    if start:
        _start_module(SCALP_MODULE)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="MoMQ strategy mode control")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show mode, kill flag, and process PIDs")

    kill_p = sub.add_parser("kill", help="Toggle kill.flag (on also stops both loops)")
    kill_p.add_argument("state", choices=["on", "off"])

    for name, mod in (("bar", BRAIN_MODULE), ("scalp", SCALP_MODULE)):
        p = sub.add_parser(name, help=f"Switch to {name} strategy")
        p.add_argument(
            "--start",
            action="store_true",
            help=f"Start {name} loop after switching",
        )
        p.add_argument(
            "--no-safe",
            action="store_true",
            help="Skip brief kill.flag while stopping the other loop",
        )

    sub.add_parser("stop", help="Stop both brain and scalp processes")

    args = parser.parse_args(argv)
    settings = Settings()

    if args.command == "status":
        cmd_status(settings)
    elif args.command == "kill":
        cmd_kill(settings, args.state)
    elif args.command == "bar":
        cmd_bar(settings, start=args.start, safe=not args.no_safe)
    elif args.command == "scalp":
        cmd_scalp(settings, start=args.start, safe=not args.no_safe)
    elif args.command == "stop":
        _stop_both(settings)


if __name__ == "__main__":
    main()
