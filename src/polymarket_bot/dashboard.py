"""Simple monitoring dashboard for the trading bot.

Serves a lightweight HTML page (no JS framework) showing live positions,
P&L, signals, circuit breaker status, and alerts.

Architecture:
- Uses the built-in ``http.server`` (stdlib) — zero dependencies.
- Designed to be started as a daemon thread from app_multi.py.
- Pulls stats from the executor/orchestrator via shared references.

Usage:
    from polymarket_bot.dashboard import Dashboard
    dash = Dashboard(host="127.0.0.1", port=8050, executor=executor, orchestrator=orchestrator)
    dash.start()   # Starts in background thread
    dash.stop()
"""

from __future__ import annotations

import json
import logging
import threading
import time
from decimal import Decimal
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from polymarket_bot.orchestrator import StrategyOrchestrator
    from polymarket_bot.unified_executor import UnifiedExecutor

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON encoder that handles Decimal
# ---------------------------------------------------------------------------

class _DecimalEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class _DashboardHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler that serves the dashboard."""

    executor: UnifiedExecutor | None = None
    orchestrator: StrategyOrchestrator | None = None
    _start_time: float = 0.0

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress default noisy logging
        pass

    def do_GET(self) -> None:
        if self.path == "/api/stats":
            self._serve_json()
        else:
            self._serve_html()

    def _serve_json(self) -> None:
        """Serve stats as JSON for programmatic access."""
        stats = self._gather_stats()
        body = json.dumps(stats, cls=_DecimalEncoder, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self) -> None:
        """Serve self-contained HTML dashboard."""
        stats = self._gather_stats()
        html = _render_html(stats)
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _gather_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "uptime_seconds": time.time() - self._start_time,
            "timestamp": time.time(),
        }
        if self.executor:
            try:
                stats["executor"] = self.executor.get_stats()
            except Exception:
                stats["executor"] = {}
        if self.orchestrator:
            try:
                stats["orchestrator"] = self.orchestrator.get_stats()
            except Exception:
                stats["orchestrator"] = {}
        return stats


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

def _render_html(stats: dict[str, Any]) -> str:
    uptime = stats.get("uptime_seconds", 0)
    uptime_str = f"{uptime / 60:.1f} min"

    exec_stats = stats.get("executor", {})
    orch_stats = stats.get("orchestrator", {})

    total_exec = exec_stats.get("total_executions", 0)
    successful = exec_stats.get("successful", 0)
    failed = exec_stats.get("failed", 0)
    paper_profit = exec_stats.get("paper_total_profit", 0)
    paper_cost = exec_stats.get("paper_total_cost", 0)
    paper_roi = exec_stats.get("paper_roi", 0)

    cb = exec_stats.get("circuit_breaker", {})
    cb_state = cb.get("state", "unknown")
    cb_daily = cb.get("daily_loss", 0)
    cb_dd = cb.get("drawdown_pct", 0)
    cb_consec = cb.get("consecutive_losses", 0)

    portfolio = exec_stats.get("portfolio", {})
    open_pos = portfolio.get("open_positions", 0)
    realized = portfolio.get("total_realized_pnl", 0)
    unrealized = portfolio.get("total_unrealized_pnl", 0)
    total_pnl = portfolio.get("total_pnl", 0)

    signals_seen = orch_stats.get("total_signals_seen", 0)
    signals_exec = orch_stats.get("total_signals_executed", 0)
    enabled = orch_stats.get("enabled_strategies", 0)

    # Strategy breakdown
    strat_rows = ""
    by_strat = exec_stats.get("paper_trades_by_strategy", {})
    for name, data in by_strat.items():
        strat_rows += f"""
        <tr>
            <td>{name}</td>
            <td>{data.get('count', 0)}</td>
            <td>${data.get('total_profit', 0):.4f}</td>
            <td>{data.get('roi', 0):.2f}%</td>
        </tr>"""

    cb_color = "#2ecc71" if cb_state == "ARMED" else "#e74c3c"

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>Polymarket Bot Dashboard</title>
<style>
  body {{ font-family: 'Segoe UI', Tahoma, sans-serif; background: #1a1a2e; color: #eee; margin: 0; padding: 20px; }}
  h1 {{ color: #16c784; text-align: center; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }}
  .card {{ background: #16213e; border-radius: 12px; padding: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }}
  .card h2 {{ margin-top: 0; color: #e94560; font-size: 1.1em; border-bottom: 1px solid #333; padding-bottom: 8px; }}
  .metric {{ display: flex; justify-content: space-between; padding: 6px 0; }}
  .metric .label {{ color: #aaa; }}
  .metric .value {{ font-weight: bold; }}
  .positive {{ color: #16c784; }}
  .negative {{ color: #ea3943; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
  th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #333; }}
  th {{ color: #e94560; font-size: 0.9em; }}
  .cb-badge {{ display: inline-block; padding: 4px 12px; border-radius: 6px; font-weight: bold; background: {cb_color}; color: white; }}
  footer {{ text-align: center; margin-top: 24px; color: #555; font-size: 0.85em; }}
</style>
</head>
<body>
<h1>🤖 Polymarket Trading Bot</h1>

<div class="grid">

  <div class="card">
    <h2>⏱️ Runtime</h2>
    <div class="metric"><span class="label">Uptime</span><span class="value">{uptime_str}</span></div>
    <div class="metric"><span class="label">Strategies Enabled</span><span class="value">{enabled}</span></div>
    <div class="metric"><span class="label">Signals Seen</span><span class="value">{signals_seen}</span></div>
    <div class="metric"><span class="label">Signals Executed</span><span class="value">{signals_exec}</span></div>
  </div>

  <div class="card">
    <h2>📈 Execution</h2>
    <div class="metric"><span class="label">Total</span><span class="value">{total_exec}</span></div>
    <div class="metric"><span class="label">Successful</span><span class="value positive">{successful}</span></div>
    <div class="metric"><span class="label">Failed</span><span class="value negative">{failed}</span></div>
  </div>

  <div class="card">
    <h2>💰 P&L</h2>
    <div class="metric"><span class="label">Paper Profit</span><span class="value {'positive' if paper_profit >= 0 else 'negative'}">${paper_profit:.4f}</span></div>
    <div class="metric"><span class="label">Paper Cost</span><span class="value">${paper_cost:.2f}</span></div>
    <div class="metric"><span class="label">Paper ROI</span><span class="value">{paper_roi:.2f}%</span></div>
    <div class="metric"><span class="label">Realized P&L</span><span class="value {'positive' if realized >= 0 else 'negative'}">${realized:.4f}</span></div>
    <div class="metric"><span class="label">Unrealized P&L</span><span class="value">${unrealized:.4f}</span></div>
    <div class="metric"><span class="label">Total P&L</span><span class="value {'positive' if total_pnl >= 0 else 'negative'}">${total_pnl:.4f}</span></div>
  </div>

  <div class="card">
    <h2>💼 Portfolio</h2>
    <div class="metric"><span class="label">Open Positions</span><span class="value">{open_pos}</span></div>
  </div>

  <div class="card">
    <h2>🔌 Circuit Breaker</h2>
    <div class="metric"><span class="label">State</span><span class="cb-badge">{cb_state}</span></div>
    <div class="metric"><span class="label">Daily Loss</span><span class="value">${cb_daily:.2f}</span></div>
    <div class="metric"><span class="label">Drawdown</span><span class="value">{cb_dd:.1f}%</span></div>
    <div class="metric"><span class="label">Consecutive Losses</span><span class="value">{cb_consec}</span></div>
  </div>

  <div class="card" style="grid-column: 1 / -1;">
    <h2>📊 Strategy Breakdown</h2>
    <table>
      <tr><th>Strategy</th><th>Trades</th><th>Profit</th><th>ROI</th></tr>
      {strat_rows if strat_rows else '<tr><td colspan="4" style="text-align:center;color:#555;">No trades yet</td></tr>'}
    </table>
  </div>

</div>

<footer>Auto-refreshes every 10 seconds &bull; <a href="/api/stats" style="color:#e94560;">JSON API</a></footer>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Dashboard server
# ---------------------------------------------------------------------------

class Dashboard:
    """Background HTTP server for bot monitoring.

    Usage:
        dash = Dashboard(host, port, executor, orchestrator)
        dash.start()   # non-blocking
        dash.stop()
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8050,
        executor: UnifiedExecutor | None = None,
        orchestrator: StrategyOrchestrator | None = None,
    ) -> None:
        self.host = host
        self.port = port

        # Create handler class with references injected.
        handler = type(
            "_Handler",
            (_DashboardHandler,),
            {
                "executor": executor,
                "orchestrator": orchestrator,
                "_start_time": time.time(),
            },
        )

        self._server = HTTPServer((host, port), handler)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.info("📊 Dashboard running at http://%s:%d", self.host, self.port)

    def stop(self) -> None:
        self._server.shutdown()
        log.info("Dashboard stopped")
