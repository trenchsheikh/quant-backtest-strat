"""Download all trades for a Quanthack player via public API."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests

API_BASE = "https://quanthack.syphonix.com/api/v1"
DEFAULT_COMPETITION = "0e2336e4-eca5-4922-927e-dd670ee668e1"
DEFAULT_PLAYER = 758


def fetch_all_trades(
    player_id: int,
    competition_id: str,
    page_size: int = 100,
    sleep_s: float = 0.12,
) -> dict:
    session = requests.Session()
    all_trades: list[dict] = []
    page = 1
    total = None
    summary = None

    while True:
        url = (
            f"{API_BASE}/player/{player_id}/trades"
            f"?competition_id={competition_id}&page={page}&page_size={page_size}"
        )
        for attempt in range(5):
            try:
                resp = session.get(url, timeout=60)
                resp.raise_for_status()
                payload = resp.json()
                break
            except requests.RequestException:
                if attempt == 4:
                    raise
                time.sleep(1.5 * (attempt + 1))
        else:
            break

        data = payload.get("data", {})
        batch = data.get("trades", [])
        pag = data.get("pagination", {})
        summary = data.get("summary", summary)
        if total is None:
            total = pag.get("total", 0)

        if not batch:
            break

        all_trades.extend(batch)
        print(f"page {page}: +{len(batch)} trades (total fetched {len(all_trades)}/{total})")

        if len(all_trades) >= total:
            break
        page += 1
        time.sleep(sleep_s)

    return {
        "player_id": player_id,
        "competition_id": competition_id,
        "summary": summary,
        "trades": all_trades,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--player", type=int, default=DEFAULT_PLAYER)
    p.add_argument("--competition", default=DEFAULT_COMPETITION)
    p.add_argument("--out", default="research/player758/trades.json")
    args = p.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    result = fetch_all_trades(args.player, args.competition)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"wrote {len(result['trades'])} trades -> {out}")
    if result.get("summary"):
        print("summary:", json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
