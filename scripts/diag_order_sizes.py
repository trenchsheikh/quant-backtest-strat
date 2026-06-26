"""Quick order-size diagnostic against live bridge."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

BASE = "http://127.0.0.1:8765"
TESTS = [
    ("EURUSD", 1, 6.6),
    ("XAUUSD", -1, 5.0),
    ("BTCUSD", 1, 33.0),
    ("BTCUSD", 1, 5.0),
]


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=20.0) as c:
        h = c.get("/health")
        print("health:", h.json() if h.status_code == 200 else h.text)
        for sym, side, lots in TESTS:
            r = c.post(
                "/order",
                json={"symbol": sym, "side": side, "lots": lots, "comment": "diag", "magic": 42},
            )
            d = r.json()
            print(f"{sym:8} {lots:5}L -> ok={d.get('ok')} msg={d.get('message', '')}")


if __name__ == "__main__":
    main()
