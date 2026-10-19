"""Minimal FastAPI dashboard for the live runner's state.

Read-only by design. It never sends orders and never even sees the broker — it
only renders what :class:`~nornweave_forex.live.state.LiveState` says, which is
exactly what a paper run and a real run both persist.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse
except ImportError:  # pragma: no cover - optional extra
    FastAPI = None  # type: ignore
    HTTPException = Exception  # type: ignore
    HTMLResponse = None  # type: ignore

__all__ = ["build_app"]

_PAGE = """<!doctype html>
<html><head><title>NornWeaveForex</title>
<style>
body{font-family:system-ui,sans-serif;background:#0d1117;color:#c9d1d9;padding:24px}
h1{color:#58a6ff}table{border-collapse:collapse;width:100%;margin-top:16px}
th,td{padding:8px 12px;border-bottom:1px solid #21262d;text-align:left}
th{color:#8b949e}.pos{color:#3fb950}.neg{color:#f85149}.flat{color:#8b949e}
</style></head><body>
<h1>NornWeaveForex</h1>
<div id="summary"></div>
<table id="positions"></table>
<script>
async function tick(){
  const r = await fetch("/api/state");
  const s = await r.json();
  const eq = s.equity !== undefined ? s.equity.toFixed(2) : "—";
  document.getElementById("summary").innerHTML =
    `<p>Equity: <b>${eq}</b> | Open: ${s.open_symbols.join(", ") || "flat"} ` +
    `| Trades closed: ${s.trades_closed} | Realized: ` +
    `<span class="${s.realized_pnl>=0?'pos':'neg'}">${s.realized_pnl.toFixed(2)}</span></p>`;
  const rows = s.positions.map(p =>
    `<tr><td>${p.symbol}</td><td class="${p.direction===1?'pos':'neg'}">${p.side}</td>` +
    `<td>${p.qty.toFixed(2)}</td><td>${p.entry_price}</td>` +
    `<td>${p.stop}</td><td>${p.tp_prices.filter(x=>x>0).join(", ")}</td></tr>`).join("");
  document.getElementById("positions").innerHTML =
    `<tr><th>Symbol</th><th>Side</th><th>Lots</th><th>Entry</th><th>Stop</th><th>Targets</th></tr>` + rows;
}
tick(); setInterval(tick, 5000);
</script></body></html>"""


def build_app(state_path: Path) -> Any:
    """Construct the FastAPI app. ``state_path`` is the runner's state JSON."""
    if FastAPI is None:  # pragma: no cover
        raise RuntimeError("fastapi is not installed; install nornweave-forex[dashboard]")

    app = FastAPI(title="NornWeaveForex")

    def _read_state() -> dict[str, Any]:
        if not state_path.exists():
            raise HTTPException(status_code=404, detail="state file not found")
        return json.loads(state_path.read_text(encoding="utf-8"))

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _PAGE

    @app.get("/api/state")
    def api_state() -> dict[str, Any]:
        raw = _read_state()
        positions = []
        for sym, p in (raw.get("positions") or {}).items():
            positions.append({
                "symbol": sym,
                "direction": p.get("direction", 0),
                "side": {1: "long", -1: "short", 0: "flat"}.get(int(p.get("direction", 0)), "?"),
                "qty": p.get("qty", 0.0),
                "entry_price": p.get("entry_price", 0.0),
                "stop": p.get("stop", 0.0),
                "tp_prices": [x for x in (p.get("tp_prices") or []) if x > 0],
            })
        # A flat state file has no equity field; synthesize it from cash.
        equity = raw.get("equity", raw.get("cash", 0.0))
        return {
            "equity": float(equity),
            "cash": float(raw.get("cash", 0.0)),
            "open_symbols": sorted((raw.get("positions") or {}).keys()),
            "positions": positions,
            "trades_closed": int(raw.get("trades_closed", 0)),
            "realized_pnl": float(raw.get("realized_pnl", 0.0)),
            "halted": bool(raw.get("halted", False)),
            "halt_reason": raw.get("halt_reason"),
            "day": raw.get("day", ""),
        }

    @app.get("/api/trades")
    def api_trades() -> list[dict[str, Any]]:
        raw = _read_state()
        return raw.get("trade_log") or []

    return app
