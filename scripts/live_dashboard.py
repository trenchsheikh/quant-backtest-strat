"""Read-only MoMQ live dashboard — does not trade or write state files.

Usage (separate terminal; port 8799 — does not touch bridge 8765):
    py -3.10 scripts/live_dashboard.py

Open http://127.0.0.1:8799 in your browser. Auto-refreshes every 30s.

Data: live_state/*.json (read), bridge GET /account and /positions only.
"""
from __future__ import annotations

import json
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import httpx

from live.brain.round_metrics import (
    compute_round_metrics,
    summarize_round_activity,
    RECONCILE_ONLY_EXITS,
)
from live.config import ROUND_SCHEDULE, Settings
from live.dashboard_score import compute_live_competition_score
from live.state.store import StateStore
from live.state.trade_journal import TradeEvent, TradeJournal
from live.strategy_mode import get_strategy_mode

BAR_SECONDS = 900
INIT_EQUITY = 1_000_000.0
HOST = "127.0.0.1"
PORT = 8799


def _current_round() -> tuple[str, datetime, datetime, int] | None:
    now = datetime.now(timezone.utc)
    for kind, t0, t1 in ROUND_SCHEDULE:
        if t0 <= now < t1:
            bar_i = int((now - t0).total_seconds() / BAR_SECONDS)
            return kind, t0, t1, bar_i
    return None


def _parse_ts(raw: str | datetime) -> datetime:
    if isinstance(raw, datetime):
        ts = raw
    else:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def build_snapshot() -> dict:
    settings = Settings()
    bridge = f"http://{settings.BRIDGE_HOST}:{settings.BRIDGE_PORT}"
    round_info = _current_round()
    round_kind = round_info[0] if round_info else None
    round_t0 = round_info[1] if round_info else None
    bar_i = round_info[3] if round_info else None

    try:
        mode = get_strategy_mode(settings).value
    except Exception:
        mode = "unknown"

    state = None
    state_err = None
    try:
        state = StateStore(settings.STATE_DIR).load()
    except Exception as exc:
        state_err = str(exc)

    events_raw: list[dict] = []
    try:
        journal = TradeJournal(settings.STATE_DIR)
        events_raw = [e.model_dump(mode="json") for e in journal.load().events]
    except Exception as exc:
        state_err = state_err or f"journal: {exc}"

    account: dict | None = None
    positions: list = []
    bridge_err = None
    try:
        with httpx.Client(base_url=bridge, timeout=5.0) as client:
            ar = client.get("/account")
            if ar.status_code == 200:
                account = ar.json()
            pr = client.get("/positions")
            if pr.status_code == 200:
                positions = pr.json()
    except httpx.HTTPError as exc:
        bridge_err = str(exc)

    since = round_t0
    if since and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    trade_count = 0
    activity = {"opens": 0, "closes": 0, "reconcile_closes": 0, "executed_orders": 0}
    parsed: list[TradeEvent] = []
    if since:
        for e in events_raw:
            try:
                parsed.append(TradeEvent.model_validate(e))
            except Exception:
                continue
        activity = summarize_round_activity(parsed, since)
        trade_count = activity["executed_orders"]
    elif state:
        trade_count = state.round_trade_count

    anchor = state.round_equity_anchor if state and state.round_equity_anchor > 0 else INIT_EQUITY
    # Use round anchor + brain samples + live equity for charts/metrics
    samples = list(state.equity_samples) if state and state.equity_samples else []
    if state and state.round_kind == round_kind and state.round_t0 == round_t0:
        chart_eq = [anchor] + samples if samples else [anchor]
    else:
        chart_eq = [anchor]
    live_equity = float(account["equity"]) if account and account.get("equity") is not None else None
    if live_equity is not None:
        if not chart_eq or abs(chart_eq[-1] - live_equity) > 0.01:
            chart_eq.append(live_equity)

    metrics = compute_round_metrics(chart_eq, round_equity_anchor=anchor, trade_count=trade_count)

    closes: list[dict] = []
    reconcile_closes: list[dict] = []
    sleeve_totals: dict[str, dict] = {}
    for e in events_raw:
        if e.get("event") != "close" or e.get("paper"):
            continue
        ts = _parse_ts(e["ts"])
        if since and ts < since:
            continue
        exit_reason = e.get("exit_reason", "") or ""
        pnl = e.get("pnl")
        sleeve = e.get("sleeve") or "unknown"
        row = {
            "time": ts.isoformat(),
            "ticket": e.get("ticket"),
            "sleeve": sleeve,
            "symbol": e.get("symbol"),
            "side": "L" if int(e.get("side", 0)) == 1 else "S",
            "lots": e.get("lots"),
            "pnl": pnl,
            "exit": exit_reason,
            "signal": (e.get("signal_info") or "")[:80],
        }
        if exit_reason in RECONCILE_ONLY_EXITS:
            reconcile_closes.append(row)
            continue
        closes.append(row)
        if pnl is not None:
            bucket = sleeve_totals.setdefault(sleeve, {"trades": 0, "wins": 0, "net_pnl": 0.0})
            bucket["trades"] += 1
            bucket["net_pnl"] += float(pnl)
            if float(pnl) > 0:
                bucket["wins"] += 1

    closes.sort(key=lambda r: r["time"], reverse=True)
    cum: list[dict] = []
    running = 0.0
    for row in sorted(closes, key=lambda r: r["time"]):
        if row["pnl"] is not None:
            running += float(row["pnl"])
            cum.append({"time": row["time"], "cum_pnl": round(running, 2)})

    rd_score = state.rd_score if state else 100.0
    comp_score = compute_live_competition_score(
        live_equity=live_equity,
        equity_samples=chart_eq,
        round_equity_anchor=anchor,
        trade_count=trade_count,
        rd_score=rd_score,
    )

    no_real_trades = activity["opens"] == 0 and activity["closes"] == 0

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "round_kind": round_kind,
        "bar_i": bar_i,
        "round_t0": round_t0.isoformat() if round_t0 else None,
        "strategy_mode": mode,
        "equity": live_equity,
        "balance": float(account["balance"]) if account and account.get("balance") is not None else None,
        "margin_level": account.get("margin_level") if account else None,
        "round_return_pct": round(metrics.round_return_pct, 3),
        "round_max_dd_pct": round(metrics.round_max_dd_pct, 3),
        "sharpe_15m": round(metrics.sharpe_15m, 4),
        "trade_count": trade_count,
        "round_activity": activity,
        "no_real_trades_yet": no_real_trades,
        "trades_needed": metrics.trades_needed,
        "sharpe_eligible": metrics.sharpe_eligible,
        "sharpe_rank_capped": metrics.sharpe_rank_capped,
        "equity_anchor": anchor,
        "equity_series": chart_eq,
        "cum_pnl_series": cum,
        "sleeve_pnl": sleeve_totals,
        "rd_score": rd_score,
        "competition_score": comp_score.to_dict(),
        "state_updated": state.updated_at.isoformat() if state and state.updated_at else None,
        "recent_closes": closes[:50],
        "reconcile_closes": reconcile_closes[:20],
        "positions": positions,
        "errors": {"state": state_err, "bridge": bridge_err},
    }


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>MoMQ Live Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; margin: 0; padding: 16px 20px; background: #0f1419; color: #e7e9ea; }
    h1 { margin: 0 0 4px; font-size: 1.4rem; }
    .sub { color: #71767b; font-size: 0.85rem; margin-bottom: 16px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 10px; margin-bottom: 16px; }
    .card { background: #16181c; border: 1px solid #2f3336; border-radius: 8px; padding: 12px; }
    .card .label { color: #71767b; font-size: 0.75rem; text-transform: uppercase; }
    .card .val { font-size: 1.25rem; font-weight: 600; margin-top: 4px; }
    .pos { color: #00ba7c; } .neg { color: #f4212e; }
    .charts { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
    @media (max-width: 900px) { .charts { grid-template-columns: 1fr; } }
    .panel { background: #16181c; border: 1px solid #2f3336; border-radius: 8px; padding: 12px; }
    .panel h2 { margin: 0 0 10px; font-size: 0.95rem; }
    table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
    th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #2f3336; }
    th { color: #71767b; font-weight: 500; }
    .warn { background: #3d2f00; border: 1px solid #785a00; color: #ffd666; padding: 8px 12px; border-radius: 6px; margin-bottom: 12px; font-size: 0.85rem; }
    .info { background: #0d2137; border: 1px solid #1d4ed8; color: #93c5fd; padding: 8px 12px; border-radius: 6px; margin-bottom: 12px; font-size: 0.85rem; }
    .score-hero { background: linear-gradient(135deg, #1a2332 0%, #16181c 100%); border: 1px solid #3b82f6; border-radius: 12px; padding: 16px 20px; margin-bottom: 16px; display: grid; grid-template-columns: 140px 1fr; gap: 16px; align-items: center; }
    .score-big { font-size: 2.5rem; font-weight: 700; color: #3b82f6; line-height: 1; }
    .score-big span { font-size: 0.9rem; color: #71767b; font-weight: 400; }
    .score-breakdown { font-size: 0.82rem; color: #aab8c2; line-height: 1.6; }
    .score-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 8px; }
    .score-pill { background: #0f1419; border-radius: 6px; padding: 8px; text-align: center; }
    .score-pill .w { color: #71767b; font-size: 0.7rem; }
    .score-pill .v { font-weight: 600; font-size: 1rem; margin-top: 2px; }
    canvas { max-height: 260px; }
  </style>
</head>
<body>
  <h1>MoMQ Live Dashboard</h1>
  <p class="sub">Read-only · port 8799 · no orders · refresh <span id="countdown">30</span>s</p>
  <div id="alerts"></div>
  <div id="scoreHero"></div>
  <div class="grid" id="kpis"></div>
  <div class="charts">
    <div class="panel"><h2>Equity (15m samples + live)</h2><canvas id="eqChart"></canvas></div>
    <div class="panel"><h2>Cumulative realized PnL</h2><canvas id="pnlChart"></canvas></div>
  </div>
  <div class="charts">
    <div class="panel"><h2>PnL by sleeve (this round)</h2><div id="sleeveTable"></div></div>
    <div class="panel"><h2>Open positions (MT5)</h2><div id="posTable"></div></div>
  </div>
  <div class="panel"><h2>Recent closes</h2><div id="closeNote"></div><div id="closeTable"></div></div>
  <div class="panel" id="reconcilePanel" style="display:none"><h2>Reconcile cleanup (not scored trades)</h2><div id="reconcileTable"></div></div>
<script>
let eqChart, pnlChart;
const fmtUsd = n => n == null ? '—' : '$' + Number(n).toLocaleString(undefined, {maximumFractionDigits: 0});
const fmtPct = n => n == null ? '—' : (n >= 0 ? '+' : '') + Number(n).toFixed(2) + '%';
const cls = n => n == null ? '' : (n >= 0 ? 'pos' : 'neg');

function tableFromRows(rows, cols) {
  if (!rows.length) return '<p style="color:#71767b">No data</p>';
  let h = '<table><tr>' + cols.map(c => '<th>' + c.label + '</th>').join('') + '</tr>';
  for (const r of rows) {
    h += '<tr>' + cols.map(c => '<td>' + (c.fmt ? c.fmt(r[c.key]) : (r[c.key] ?? '')) + '</td>').join('') + '</tr>';
  }
  return h + '</table>';
}

function render(d) {
  const s = d.competition_score || {};
  document.getElementById('scoreHero').innerHTML = `
    <div class="score-hero">
      <div>
        <div class="score-big">${s.final_score != null ? s.final_score.toFixed(1) : '—'}<span> / 100</span></div>
        <div style="color:#71767b;font-size:0.75rem;margin-top:4px">Est. live Final Score</div>
      </div>
      <div>
        <div class="score-breakdown">${s.score_formula || ''}</div>
        <div class="score-row">
          <div class="score-pill"><div class="w">Return rank (70%)</div><div class="v">${s.return_rank != null ? s.return_rank.toFixed(1) : '—'}</div><div class="w">${fmtPct(s.competition_return_pct)} vs $1M</div></div>
          <div class="score-pill"><div class="w">DD rank (15%)</div><div class="v">${s.dd_rank != null ? s.dd_rank.toFixed(1) : '—'}</div><div class="w">MaxDD ${fmtPct(s.round_max_dd_pct)}</div></div>
          <div class="score-pill"><div class="w">Sharpe rank (10%)</div><div class="v">${s.sharpe_rank != null ? s.sharpe_rank.toFixed(1) : '—'}</div><div class="w">Sharpe ${s.sharpe_15m != null ? Number(s.sharpe_15m).toFixed(3) : '—'}</div></div>
          <div class="score-pill"><div class="w">Risk disc. (5%)</div><div class="v">${s.risk_discipline != null ? s.risk_discipline.toFixed(0) : '—'}</div><div class="w">RD / 100</div></div>
        </div>
      </div>
    </div>`;

  const k = [
    { label: 'Equity', val: fmtUsd(d.equity) },
    { label: 'Comp return', val: fmtPct(s.competition_return_pct), c: cls(s.competition_return_pct) },
    { label: 'Round return', val: fmtPct(d.round_return_pct), c: cls(d.round_return_pct) },
    { label: 'Max DD (15m)', val: fmtPct(d.round_max_dd_pct), c: cls(d.round_max_dd_pct) },
    { label: 'Sharpe (15m)', val: d.sharpe_15m == null ? '—' : Number(d.sharpe_15m).toFixed(3), c: cls(d.sharpe_15m) },
    { label: 'Scored orders', val: d.trade_count + ' / 31' },
    { label: 'Opens / closes', val: (d.round_activity?.opens ?? 0) + ' / ' + (d.round_activity?.closes ?? 0) },
    { label: 'Round', val: (d.round_kind || '—') + ' bar ' + (d.bar_i ?? '—') },
    { label: 'Mode', val: d.strategy_mode },
    { label: 'RD score', val: d.rd_score ?? '—' },
  ];
  document.getElementById('kpis').innerHTML = k.map(x =>
    '<div class="card"><div class="label">' + x.label + '</div><div class="val ' + (x.c||'') + '">' + x.val + '</div></div>'
  ).join('');

  let alerts = '';
  (s.notes || []).forEach(n => { alerts += '<div class="info">' + n + '</div>'; });
  if (d.no_real_trades_yet) {
    const rc = d.round_activity?.reconcile_closes ?? 0;
    const extra = rc > 0
      ? ' (' + rc + ' stale-position reconcile entries in journal — not real fills)'
      : '';
    alerts += '<div class="info">No Finals trades yet' + extra + '. Waiting for bar-brain signals on 15m closes.</div>';
  }
  if (!d.sharpe_eligible) alerts += '<div class="warn">&lt; 31 scored orders this round — platform may not score DD/Sharpe.</div>';
  if (d.sharpe_rank_capped) alerts += '<div class="info">&lt; 8 equity samples — Sharpe rank capped at 50 on platform.</div>';
  if (d.errors?.bridge) alerts += '<div class="warn">Bridge: ' + d.errors.bridge + '</div>';
  document.getElementById('alerts').innerHTML = alerts;

  const eqLabels = d.equity_series.map((_, i) => i === d.equity_series.length - 1 && d.equity ? 'live' : String(i));
  if (eqChart) eqChart.destroy();
  eqChart = new Chart(document.getElementById('eqChart'), {
    type: 'line',
    data: { labels: eqLabels, datasets: [{ data: d.equity_series, borderColor: '#3b82f6', tension: 0.2, pointRadius: 2 }] },
    options: { plugins: { legend: { display: false } }, scales: { y: { ticks: { callback: v => '$' + (v/1e6).toFixed(2) + 'M' } } } }
  });

  const cum = d.cum_pnl_series || [];
  if (pnlChart) pnlChart.destroy();
  pnlChart = new Chart(document.getElementById('pnlChart'), {
    type: 'line',
    data: { labels: cum.map(x => x.time.slice(11, 16)), datasets: [{ data: cum.map(x => x.cum_pnl), borderColor: '#22c55e', fill: true, tension: 0.2 }] },
    options: { plugins: { legend: { display: false } }, scales: { y: { ticks: { callback: v => '$' + v } } } }
  });

  const sleeves = Object.entries(d.sleeve_pnl || {}).map(([sleeve, v]) => ({ sleeve, ...v }));
  document.getElementById('sleeveTable').innerHTML = tableFromRows(sleeves, [
    { key: 'sleeve', label: 'Sleeve' },
    { key: 'trades', label: 'Trades' },
    { key: 'wins', label: 'Wins' },
    { key: 'net_pnl', label: 'Net PnL', fmt: v => '<span class="' + cls(v) + '">' + fmtUsd(v) + '</span>' },
  ]);

  document.getElementById('posTable').innerHTML = tableFromRows(d.positions || [], [
    { key: 'symbol', label: 'Symbol' },
    { key: 'lots', label: 'Lots' },
    { key: 'price_open', label: 'Open' },
    { key: 'price_current', label: 'Now' },
    { key: 'profit', label: 'PnL', fmt: v => '<span class="' + cls(v) + '">' + fmtUsd(v) + '</span>' },
    { key: 'comment', label: 'Comment' },
  ]);

  document.getElementById('closeNote').innerHTML = (d.recent_closes || []).length
    ? ''
    : '<p style="color:#71767b;margin:0 0 8px">No real closes this round yet.</p>';

  document.getElementById('closeTable').innerHTML = tableFromRows(d.recent_closes || [], [
    { key: 'time', label: 'Time', fmt: v => v ? v.replace('T', ' ').slice(0, 19) : '' },
    { key: 'sleeve', label: 'Sleeve' },
    { key: 'symbol', label: 'Sym' },
    { key: 'side', label: 'Side' },
    { key: 'pnl', label: 'PnL', fmt: v => v == null ? '' : '<span class="' + cls(v) + '">' + fmtUsd(v) + '</span>' },
    { key: 'exit', label: 'Exit' },
  ]);

  const recon = d.reconcile_closes || [];
  const reconPanel = document.getElementById('reconcilePanel');
  reconPanel.style.display = recon.length ? 'block' : 'none';
  document.getElementById('reconcileTable').innerHTML = tableFromRows(recon, [
    { key: 'time', label: 'Time', fmt: v => v ? v.replace('T', ' ').slice(0, 19) : '' },
    { key: 'sleeve', label: 'Sleeve' },
    { key: 'symbol', label: 'Sym' },
    { key: 'side', label: 'Side' },
    { key: 'exit', label: 'Exit' },
    { key: 'signal', label: 'Note' },
  ]);
}

async function refresh() {
  try {
    const r = await fetch('/api/snapshot');
    render(await r.json());
  } catch (e) { console.error(e); }
}

let sec = 30;
setInterval(() => {
  sec -= 1;
  document.getElementById('countdown').textContent = sec;
  if (sec <= 0) { sec = 30; refresh(); }
}, 1000);
refresh();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        pass  # quiet

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/snapshot":
            body = json.dumps(build_snapshot(), default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def main() -> None:
    server = HTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"MoMQ dashboard (read-only): {url}")
    print("Ctrl+C to stop. Does not send orders or write state.")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
