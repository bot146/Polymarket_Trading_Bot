"""Enhanced multi-strategy Polymarket trading bot with complete trade lifecycle.

This is the main application that orchestrates multiple trading strategies
and executes them safely with proper risk management, position tracking,
and automatic position closing.
"""

from __future__ import annotations

import logging
import os
import signal as sig
import sys
import threading
import time
import re
from decimal import Decimal
from pathlib import Path
from decimal import Decimal as _Decimal

from polymarket_bot.clob_client import build_clob_client
from polymarket_bot.config import load_settings
from polymarket_bot.dashboard import Dashboard
from polymarket_bot.log_config import setup_logging
from polymarket_bot.orchestrator import OrchestratorConfig, StrategyOrchestrator
from polymarket_bot.paper_wallet import PaperWalletController
from polymarket_bot.position_closer import PositionCloser
from polymarket_bot.position_manager import PositionManager
from polymarket_bot.resolution_monitor import ResolutionMonitor
from polymarket_bot.scanner import MarketScanner
from polymarket_bot.unified_executor import UnifiedExecutor

log = logging.getLogger(__name__)

# Global event for graceful shutdown (thread-safe, avoids time.sleep SIGINT issues)
_shutdown_event = threading.Event()
_shutdown_requested = False  # kept for backward compat; driven by _shutdown_event


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    global _shutdown_requested
    log.warning(f"Shutdown signal received: {signum}")
    _shutdown_requested = True
    _shutdown_event.set()


def print_banner():
    """Print startup banner."""
    banner = """
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║     POLYMARKET MULTI-STRATEGY TRADING BOT                   ║
║                                                              ║
║     Full Trade Lifecycle: Entry → Monitoring → Exit         ║
║     Strategies: Arbitrage, Guaranteed Win, Stat Arb         ║
║     Built with precision. Designed for profit.              ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
"""
    try:
        print(banner)
    except UnicodeEncodeError:
        fallback = (
            "\n"
            "==============================================================\n"
            "  POLYMARKET MULTI-STRATEGY TRADING BOT\n"
            "  Full Trade Lifecycle: Entry -> Monitoring -> Exit\n"
            "  Strategies: Arbitrage, Guaranteed Win, Stat Arb\n"
            "==============================================================\n"
        )
        print(fallback)


def print_stats(
    orchestrator: StrategyOrchestrator,
    executor: UnifiedExecutor,
    resolution_monitor: ResolutionMonitor,
    position_closer: PositionCloser,
    uptime: float,
    paper_wallet_snapshot: dict[str, float] | None = None,
):
    """Print comprehensive bot statistics."""
    orch_stats = orchestrator.get_stats()
    exec_stats = executor.get_stats()
    res_stats = resolution_monitor.get_stats()
    close_stats = position_closer.get_stats()
    
    log.info("=" * 70)
    log.info(f"⏱️  UPTIME: {uptime/60:.1f} minutes")
    log.info(f"📊 SIGNALS: seen={orch_stats['total_signals_seen']} executed={orch_stats['total_signals_executed']}")
    log.info(f"📈 EXECUTIONS: total={exec_stats['total_executions']} success={exec_stats['successful']} failed={exec_stats['failed']}")
    log.info(f"🎯 STRATEGIES: {orch_stats['enabled_strategies']} enabled")

    # Short-duration / recurring market visibility
    sd_count = orch_stats.get("short_duration_markets", 0)
    sd_series = orch_stats.get("short_duration_series", {})
    if sd_count > 0:
        parts = [f"{slug}={cnt}" for slug, cnt in sorted(sd_series.items())]
        log.info(f"⚡ SHORT-DURATION: {sd_count} markets — {', '.join(parts)}")
    else:
        log.info("⚡ SHORT-DURATION: 0 markets (none currently live)")

    if paper_wallet_snapshot is not None:
        log.info(
            "💼 WALLET: equity=$%.2f start=$%.2f adj=$%.2f size_mult=x%.2f dyn_max=$%.2f",
            paper_wallet_snapshot.get("equity", 0.0),
            paper_wallet_snapshot.get("starting_balance", 0.0),
            paper_wallet_snapshot.get("manual_adjustment", 0.0),
            paper_wallet_snapshot.get("multiplier", 1.0),
            paper_wallet_snapshot.get("dynamic_max_order_usdc", 0.0),
        )

    # Execution mode + lightweight risk/ops metrics
    profile = exec_stats.get("execution_profile")
    if profile:
        log.info(f"⚙️  EXECUTION PROFILE: {profile}")

    hedge = exec_stats.get("hedge") or {}
    if hedge:
        log.info(f"🛡️  HEDGING: events={hedge.get('events', 0)} forced={hedge.get('forced_events', 0)}")

    paper_open = exec_stats.get("paper_open_orders") or {}
    if paper_open:
        log.info(f"🧾 OPEN MAKER (paper): gtc_total={paper_open.get('open_gtc_total', 0)}")

        # Surface the biggest offenders to help tune caps and find stuck markets.
        by_cond = paper_open.get("open_gtc_by_condition") or {}
        if isinstance(by_cond, dict) and by_cond:
            top = sorted(by_cond.items(), key=lambda kv: kv[1], reverse=True)[:3]
            top_str = ", ".join([f"{cid[:10]}…:{cnt}" for cid, cnt in top])
            log.info(f"   Top open GTC by condition: {top_str}")

    # Inventory top offenders by cost basis
    if "portfolio" in exec_stats:
        portfolio = exec_stats["portfolio"]
        by_cond_cost = portfolio.get("cost_by_condition") if isinstance(portfolio, dict) else None
        if isinstance(by_cond_cost, dict) and by_cond_cost:
            top_cost = sorted(by_cond_cost.items(), key=lambda kv: float(kv[1]), reverse=True)[:3]
            top_cost_str = ", ".join([f"{cid[:10]}…:${float(cost):.2f}" for cid, cost in top_cost])
            log.info(f"🏦 Top inventory by condition (cost): {top_cost_str}")

    # Position information
    if "portfolio" in exec_stats:
        portfolio = exec_stats["portfolio"]
        log.info("─" * 70)
        log.info("💼 PORTFOLIO:")
        log.info(f"   Open Positions: {portfolio['open_positions']}")
        log.info(f"   Closed Positions: {portfolio['closed_positions']}")
        log.info(f"   Redeemable: {portfolio['redeemable_positions']}")
        log.info(f"   Cost Basis: ${portfolio['total_cost_basis']:.2f}")

    # Resolution monitoring
    if res_stats['resolved_markets'] > 0:
        log.info("─" * 70)
        log.info("🎯 RESOLUTION MONITOR:")
        log.info(f"   Resolved Markets: {res_stats['resolved_markets']}")
        log.info(f"   Redeemable Value: ${res_stats['redeemable_value']:.2f}")

    # Profitability (theoretical expected vs actual realized)
    log.info("─" * 70)
    log.info("💰 PROFITABILITY:")

    # Expected (theoretical)
    if exec_stats['paper_total_cost'] > 0:
        log.info(f"   Expected (theoretical):")
        log.info(f"     Profit: ${exec_stats['paper_total_profit']:.4f}")
        log.info(f"     Cost: ${exec_stats['paper_total_cost']:.2f}")
        log.info(f"     ROI: {exec_stats['paper_roi']:.2f}%")

    # Actual (realized + unrealized)
    if "portfolio" in exec_stats:
        portfolio = exec_stats["portfolio"]
        log.info(f"   Actual (from positions):")
        log.info(f"     Realized P&L: ${portfolio['total_realized_pnl']:.4f}")
        log.info(f"     Unrealized P&L: ${portfolio['total_unrealized_pnl']:.4f}")
        log.info(f"     Total P&L: ${portfolio['total_pnl']:.4f}")
        if portfolio['total_cost_basis'] > 0:
            log.info(f"     Realized ROI: {portfolio['realized_roi']:.2f}%")

    # Position closing stats
    if close_stats['total_closes'] > 0 or close_stats['total_redemptions'] > 0:
        log.info(f"   Closed: {close_stats['total_closes']} positions")
        log.info(f"   Redeemed: {close_stats['total_redemptions']} positions")
        log.info(f"   Total Realized: ${close_stats['total_realized_pnl']:.4f}")

    # Strategy breakdown
    if exec_stats.get('paper_trades_by_strategy'):
        log.info("─" * 70)
        log.info("📊 STRATEGY BREAKDOWN:")
        for strategy, data in exec_stats['paper_trades_by_strategy'].items():
            log.info(
                f"   {strategy}: {data['count']} trades, "
                f"expected profit=${data['total_profit']:.4f}, "
                f"ROI={data['roi']:.2f}%"
            )

    # Quote churn (paper)
    po = exec_stats.get("paper_orders") or {}
    if isinstance(po, dict) and (po.get("canceled") or po.get("requoted")):
        log.info("─" * 70)
        log.info(
            f"🔁 QUOTE CHURN (paper): canceled={po.get('canceled', 0)} "
            f"requoted={po.get('requoted', 0)}"
        )

    # Circuit breaker status
    cb = exec_stats.get("circuit_breaker") or {}
    if isinstance(cb, dict):
        log.info("─" * 70)
        log.info(
            f"🔌 CIRCUIT BREAKER: state={cb.get('state', 'unknown')} "
            f"daily_loss=${cb.get('daily_loss', 0):.2f} "
            f"drawdown={cb.get('drawdown_pct', 0):.1f}% "
            f"consec_losses={cb.get('consecutive_losses', 0)} "
            f"trips={cb.get('total_trips', 0)}"
        )

    # Strategy attribution (actual P&L)
    if "portfolio" in exec_stats:
        portfolio = exec_stats["portfolio"]
        by_strat = portfolio.get("by_strategy") if isinstance(portfolio, dict) else None
        if isinstance(by_strat, dict):
            realized = by_strat.get("realized") or {}
            unrealized = by_strat.get("unrealized") or {}
            cost = by_strat.get("cost") or {}
            if realized or unrealized or cost:
                log.info("─" * 70)
                log.info("🧠 STRATEGY ATTRIBUTION (actual):")
                keys = sorted(set(realized.keys()) | set(unrealized.keys()) | set(cost.keys()))
                for k in keys:
                    log.info(
                        f"   {k}: cost=${float(cost.get(k, 0.0)):.2f} "
                        f"realized=${float(realized.get(k, 0.0)):.4f} "
                        f"unrealized=${float(unrealized.get(k, 0.0)):.4f}"
                    )

    log.info("=" * 70)


def _extract_decimal_candidates(obj: object) -> list[tuple[str, _Decimal]]:
    candidates: list[tuple[str, _Decimal]] = []

    def walk(val: object, prefix: str = "") -> None:
        if isinstance(val, dict):
            for k, v in val.items():
                key = f"{prefix}.{k}" if prefix else str(k)
                walk(v, key)
            return
        if isinstance(val, (list, tuple)):
            for i, item in enumerate(val):
                key = f"{prefix}[{i}]" if prefix else f"[{i}]"
                walk(item, key)
            return
        if val is None:
            return

        s = str(val).strip()
        if not s:
            return
        # Keep plain numeric-like strings only.
        if not re.fullmatch(r"[-+]?\d+(\.\d+)?", s):
            return
        try:
            d = _Decimal(s)
        except Exception:
            return
        if d < 0:
            return
        candidates.append((prefix.lower(), d))

    walk(obj)
    return candidates


def _normalize_usdc_amount(raw: _Decimal) -> _Decimal:
    # Some APIs return micro-USDC integers.
    if raw > _Decimal("1000000"):
        return (raw / _Decimal("1000000")).quantize(_Decimal("0.000001"))
    return raw


def _extract_live_available_collateral(resp: object) -> _Decimal | None:
    if resp is None:
        return None

    payload: object = resp
    if hasattr(resp, "model_dump"):
        try:
            payload = resp.model_dump()  # type: ignore[assignment]
        except Exception:
            payload = resp
    elif hasattr(resp, "__dict__"):
        try:
            payload = dict(getattr(resp, "__dict__"))
        except Exception:
            payload = resp

    candidates = _extract_decimal_candidates(payload)
    if not candidates:
        return None

    priority = ("available", "balance", "collateral", "amount")
    for tag in priority:
        for key, value in candidates:
            if tag in key:
                return _normalize_usdc_amount(value)

    return _normalize_usdc_amount(candidates[0][1])


def _compute_multiplier_for_equity(equity: _Decimal, tier_spec: str) -> tuple[_Decimal, _Decimal]:
    multiplier = _Decimal("1")
    floor = _Decimal("0")

    tiers: list[tuple[_Decimal, _Decimal]] = []
    for raw in (tier_spec or "").split(","):
        item = raw.strip()
        if not item or ":" not in item:
            continue
        left, right = item.split(":", 1)
        try:
            eq = _Decimal(left.strip())
            mult = _Decimal(right.strip())
        except Exception:
            continue
        if eq < 0 or mult <= 0:
            continue
        tiers.append((eq, mult))

    tiers.sort(key=lambda x: x[0])
    for eq_floor, eq_mult in tiers:
        if equity >= eq_floor:
            floor = eq_floor
            multiplier = eq_mult
        else:
            break

    return multiplier, floor


class BotRunner:
    """Encapsulates all bot components and the main trading loop.

    Usage:
        runner = BotRunner(settings)
        runner.run()      # blocks until shutdown
        runner.shutdown()  # final stats
    """

    def __init__(self, settings) -> None:  # noqa: ANN001 (Settings from config)
        self.settings = settings
        self.client = None
        self.creds = None

        # ── Position manager ──────────────────────────────────────────
        storage_path = Path.home() / ".polymarket_bot" / "positions.json"
        self.position_manager = PositionManager(storage_path=str(storage_path))

        if settings.trading_mode == "paper" and settings.paper_reset_on_start:
            log.info("🧹 Paper-mode clean start: resetting positions & wallet …")
            self.position_manager.reset_all_positions()
            # Remove stale backup files so they can never be confused with live state.
            bot_dir = storage_path.parent
            for bak in bot_dir.glob("positions_bak_*"):
                try:
                    bak.unlink()
                except OSError:
                    pass
        log.info("✅ Position manager initialized (%d positions loaded)", len(self.position_manager.positions))

        # ── CLOB client ───────────────────────────────────────────────
        if settings.poly_private_key:
            try:
                self.client, self.creds = build_clob_client(settings)
                log.info("✅ CLOB client initialized")
            except Exception as e:
                log.warning("⚠️  CLOB client init failed (running in data-only mode): %s", e)
        else:
            log.warning("⚠️  No private key provided - running in data-only mode")

        # ── Scanner & monitors ────────────────────────────────────────
        self.scanner = MarketScanner()
        self.resolution_monitor = ResolutionMonitor(
            position_manager=self.position_manager,
            scanner=self.scanner,
            check_interval=60.0,
        )
        log.info("✅ Resolution monitor initialized")

        self.position_closer = PositionCloser(
            client=self.client,
            settings=settings,
            position_manager=self.position_manager,
            resolution_monitor=self.resolution_monitor,
        )
        log.info("✅ Position closer initialized")

        # ── Orchestrator & executor ───────────────────────────────────
        orch_config = OrchestratorConfig(
            scan_interval=2.0,
            max_concurrent_trades=settings.max_concurrent_trades,
            enable_arbitrage=settings.enable_arbitrage,
            enable_guaranteed_win=settings.enable_guaranteed_win,
            enable_multi_outcome_arb=settings.enable_multi_outcome_arb,
            enable_stat_arb=settings.enable_stat_arb,
            enable_value_betting=settings.enable_value_betting,
            enable_sniping=settings.enable_sniping,
            enable_market_making=settings.enable_market_making,
            enable_oracle_sniping=settings.enable_oracle_sniping_strategy,
            enable_copy_trading=settings.enable_copy_trading,
            enable_conditional_arb=settings.enable_conditional_arb,
            enable_liquidity_rewards=settings.enable_liquidity_rewards,
            enable_near_resolution=settings.enable_near_resolution,
            enable_arb_stacking=settings.enable_arb_stacking,
            max_arb_stacks=settings.max_arb_stacks,
            enable_short_duration=settings.enable_short_duration_strategy,
            scan_short_duration=settings.enable_short_duration_scan,
        )
        self.orch_config = orch_config
        self.orchestrator = StrategyOrchestrator(settings, orch_config)
        self.executor = UnifiedExecutor(self.client, settings, position_manager=self.position_manager)

        # ── Paper/live wallet ─────────────────────────────────────────
        self.paper_wallet: PaperWalletController | None = None
        self.last_wallet_snapshot: dict[str, float] | None = None
        if settings.trading_mode in {"paper", "live"}:
            wallet_path = (
                Path(settings.paper_wallet_path)
                if settings.paper_wallet_path
                else (Path.home() / ".polymarket_bot" / "paper_wallet.json")
            )
            self.paper_wallet = PaperWalletController(
                file_path=wallet_path,
                default_starting_balance=settings.paper_start_balance,
                default_tier_spec=settings.paper_sizing_tiers,
                refresh_seconds=settings.paper_wallet_refresh_seconds,
            )
            if settings.trading_mode == "paper" and settings.paper_reset_on_start:
                wallet_path.parent.mkdir(parents=True, exist_ok=True)
                # Delete first to avoid stale data on failed write.
                if wallet_path.exists():
                    try:
                        wallet_path.unlink()
                    except OSError:
                        pass
                wallet_path.write_text(
                    '{\n'
                    f'  "starting_balance": "{settings.paper_start_balance}",\n'
                    '  "manual_adjustment": "0",\n'
                    '  "tiers": [\n'
                    '    {"equity": "100", "multiplier": "1.00"},\n'
                    '    {"equity": "1000", "multiplier": "1.10"},\n'
                    '    {"equity": "5000", "multiplier": "1.20"},\n'
                    '    {"equity": "10000", "multiplier": "1.30"}\n'
                    '  ]\n'
                    '}',
                    encoding="utf-8",
                )
                log.info("🧹 Paper wallet reset to $%s", settings.paper_start_balance)
            self.paper_wallet.ensure_file()
            log.info("💼 Paper wallet config: %s", wallet_path)

        # ── Restore dedup state ───────────────────────────────────────
        open_positions = [p for p in self.position_manager.get_open_positions() if p.condition_id]
        for p in open_positions:
            self.orchestrator.mark_position_active(p.condition_id)
        if open_positions:
            if settings.trading_mode == "live":
                log.info("✅ Live startup: restored %d active position entries", len(open_positions))
            else:
                log.info("✅ Restored %d active position entries from disk", len(open_positions))

        # ── Dashboard ─────────────────────────────────────────────────
        self.dashboard: Dashboard | None = None
        if settings.enable_dashboard:
            try:
                self.dashboard = Dashboard(
                    host=settings.dashboard_host,
                    port=settings.dashboard_port,
                    executor=self.executor,
                    orchestrator=self.orchestrator,
                )
                self.dashboard.start()
            except Exception as e:
                log.warning("⚠️  Dashboard failed to start: %s", e)

        log.info("🚀 Bot initialized with full trade lifecycle. Starting main loop...")

        # ── Loop timers ───────────────────────────────────────────────
        self.start_time = time.time()
        self._last_stats_time = self.start_time
        self._last_resolution_check = self.start_time
        self._last_position_close_check = self.start_time
        self._last_runtime_reload_check = 0.0
        self._last_live_wallet_refresh_check = 0.0
        self._last_live_available_collateral: _Decimal | None = None
        self._iteration = 0

    # ── Main loop ─────────────────────────────────────────────────────

    def run(self) -> None:
        """Run the main trading loop until shutdown is requested."""
        while not _shutdown_event.is_set():
            try:
                self._iteration += 1
                loop_start = time.time()

                runtime_settings = self._maybe_reload_settings(loop_start)
                self._check_resolutions(loop_start)
                self._check_and_close_positions(loop_start)
                self._update_wallet(runtime_settings, loop_start)

                signals = self.orchestrator.run_once()

                if self.settings.trading_mode == "paper":
                    self._advance_paper_fills()

                self._execute_signals(signals)
                self._maybe_print_stats()

                if signals:
                    log.info("Iteration %d: processed %d signals", self._iteration, len(signals))

                # Sleep until next scan (Event.wait avoids SIGINT issues on Windows)
                elapsed = time.time() - loop_start
                sleep_time = max(0, self.orch_config.scan_interval - elapsed)
                if sleep_time > 0:
                    _shutdown_event.wait(timeout=sleep_time)

            except KeyboardInterrupt:
                log.warning("Keyboard interrupt received")
                _shutdown_event.set()
                break
            except Exception as e:
                log.exception("Error in main loop: %s", e)
                _shutdown_event.wait(timeout=5)

    # ── Extracted helpers ─────────────────────────────────────────────

    def _maybe_reload_settings(self, loop_start: float):
        """Hot-reload .env if enabled."""
        if (
            self.settings.runtime_reload_env
            and (loop_start - self._last_runtime_reload_check) >= self.settings.runtime_reload_seconds
        ):
            self._last_runtime_reload_check = loop_start
            return load_settings()
        return self.settings

    def _check_resolutions(self, loop_start: float) -> None:
        if loop_start - self._last_resolution_check < 60:
            return
        resolution_events = self.resolution_monitor.check_resolutions()
        if resolution_events:
            log.info("🎯 Detected %d newly resolved markets", len(resolution_events))
            for ev in resolution_events:
                for _pid in ev.affected_positions:
                    if ev.condition_id:
                        self.orchestrator.mark_position_closed(ev.condition_id)
        self._last_resolution_check = loop_start

    def _check_and_close_positions(self, loop_start: float) -> None:
        if loop_start - self._last_position_close_check < self.settings.exit_check_interval_seconds:
            return

        # Build price_data from live top-of-book feed
        tob_snap = self.orchestrator.get_top_of_book_snapshot()
        price_data: dict[str, _Decimal] = {}
        bid_map = tob_snap.get("best_bid", {})
        ask_map = tob_snap.get("best_ask", {})
        for tid in set(bid_map.keys()) | set(ask_map.keys()):
            bid_v = bid_map.get(tid)
            ask_v = ask_map.get(tid)
            if bid_v is not None and ask_v is not None:
                price_data[tid] = (_Decimal(str(bid_v)) + _Decimal(str(ask_v))) / 2
            elif ask_v is not None:
                price_data[tid] = _Decimal(str(ask_v))
            elif bid_v is not None:
                price_data[tid] = _Decimal(str(bid_v))

        pos_by_id = {
            p.position_id: p.condition_id
            for p in self.position_manager.get_open_positions() + self.position_manager.get_redeemable_positions()
            if p.position_id and p.condition_id
        }

        close_results = self.position_closer.check_and_close_positions(price_data)
        if close_results:
            successful_closes = [r for r in close_results if r.success]
            if successful_closes:
                realized_batch = _Decimal("0")
                for r in successful_closes:
                    cid = pos_by_id.get(r.position_id)
                    if cid:
                        self.orchestrator.mark_position_closed(cid)
                    if r.realized_pnl is not None:
                        realized_batch += _Decimal(str(r.realized_pnl))

                if realized_batch != _Decimal("0"):
                    self.executor.record_realized_trade_pnl(realized_batch)

                log.info("✅ Closed %d positions", len(successful_closes))
        self._last_position_close_check = loop_start

    def _update_wallet(self, runtime_settings, loop_start: float) -> None:  # noqa: ANN001
        """Refresh wallet equity cap, tier multiplier, and dynamic sizing."""
        if self.settings.trading_mode not in {"paper", "live"} or self.paper_wallet is None:
            return

        portfolio = self.position_manager.get_portfolio_stats()
        available_collateral_value: _Decimal | None = None

        if self.settings.trading_mode == "paper":
            self.paper_wallet.refresh()
            snap = self.paper_wallet.snapshot(portfolio_stats=portfolio)
            self.paper_wallet.maybe_log_tier_change(snap)
            sizing_equity = snap.equity
            realized_pnl = _Decimal(str(portfolio.get("total_realized_pnl", 0)))
            equity_cap = snap.starting_balance + snap.manual_adjustment + realized_pnl
            multiplier = snap.multiplier
            starting_balance = snap.starting_balance
            manual_adjustment = snap.manual_adjustment
        else:
            # LIVE: use real collateral balance from chain.
            refresh_interval = max(1.0, float(self.settings.paper_wallet_refresh_seconds))
            if self.client is not None and (loop_start - self._last_live_wallet_refresh_check) >= refresh_interval:
                self._last_live_wallet_refresh_check = loop_start
                try:
                    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

                    resp = self.client.get_balance_allowance(
                        BalanceAllowanceParams(
                            asset_type=AssetType.COLLATERAL,
                            token_id=None,
                            signature_type=-1,
                        )
                    )
                    live_available = _extract_live_available_collateral(resp)
                    if live_available is not None:
                        self._last_live_available_collateral = live_available
                except Exception as e:
                    log.warning("Could not refresh live collateral balance; keeping last value: %s", e)

            open_cost = _Decimal(str(portfolio.get("total_cost_basis", 0)))
            unrealized_pnl = _Decimal(str(portfolio.get("total_unrealized_pnl", 0)))
            available_collateral = self._last_live_available_collateral
            if available_collateral is None:
                available_collateral = _Decimal(str(self.settings.paper_start_balance))
            available_collateral_value = available_collateral

            equity_cap = (available_collateral + open_cost).quantize(_Decimal("0.01"))
            if equity_cap < _Decimal("0"):
                equity_cap = _Decimal("0")
            sizing_equity = (available_collateral + open_cost + unrealized_pnl).quantize(_Decimal("0.01"))
            multiplier, _tier_floor = _compute_multiplier_for_equity(
                sizing_equity,
                self.settings.paper_sizing_tiers,
            )
            starting_balance = available_collateral
            manual_adjustment = _Decimal("0")

        self.executor.set_equity_cap(equity_cap)
        self.executor.update_circuit_breaker_portfolio_value(sizing_equity)
        dynamic_max = (runtime_settings.max_order_usdc * multiplier).quantize(_Decimal("0.01"))
        self.orchestrator.set_dynamic_sizing_params(
            max_order_usdc=dynamic_max,
            min_order_usdc=runtime_settings.min_order_usdc,
            initial_order_pct=runtime_settings.initial_order_pct,
        )
        self.last_wallet_snapshot = {
            "mode": self.settings.trading_mode,
            "equity": float(equity_cap),
            "sizing_equity": float(sizing_equity),
            "starting_balance": float(starting_balance),
            "manual_adjustment": float(manual_adjustment),
            "multiplier": float(multiplier),
            "dynamic_max_order_usdc": float(dynamic_max),
            "available_collateral": float(available_collateral_value) if available_collateral_value is not None else None,
        }
        self.executor.set_wallet_snapshot(self.last_wallet_snapshot)

    def _advance_paper_fills(self) -> None:
        """Feed latest top-of-book prices into the paper fill simulator."""
        tob = self.orchestrator.get_top_of_book_snapshot()
        best_bid_map = tob.get("best_bid", {})
        best_ask_map = dict(tob.get("best_ask", {}))

        clob_cache = getattr(self.orchestrator, "_clob_cache", {})
        for tid, price in clob_cache.items():
            if price is not None:
                best_ask_map[tid] = price

        if best_bid_map or best_ask_map:
            token_ids = set(best_bid_map.keys()) | set(best_ask_map.keys())
            for token_id in token_ids:
                bid = best_bid_map.get(token_id)
                ask = best_ask_map.get(token_id)
                self.executor.on_market_update(
                    token_id=token_id,
                    best_bid=_Decimal(str(bid)) if bid is not None else None,
                    best_ask=_Decimal(str(ask)) if ask is not None else None,
                    best_ask_by_token={k: _Decimal(str(v)) for k, v in best_ask_map.items() if v is not None},
                )

    def _execute_signals(self, signals: list) -> None:
        enabled_by_name = {s.name: s for s in self.orchestrator.registry.get_enabled()}
        for signal in signals:
            if _shutdown_event.is_set():
                break

            strategy = enabled_by_name.get(signal.opportunity.strategy_type.value)
            if not strategy:
                log.warning("No strategy found for signal type: %s", signal.opportunity.strategy_type)
                continue

            result = self.executor.execute_signal(signal, strategy)

            if result.success:
                condition_id = signal.opportunity.metadata.get("condition_id")
                if condition_id:
                    self.orchestrator.mark_position_active(condition_id)
                self.orchestrator.total_signals_executed += 1
                log.info(
                    "✅ Paper trade executed: %s profit=$%.4f",
                    signal.opportunity.strategy_type.value,
                    signal.opportunity.expected_profit,
                )
            else:
                log.warning("❌ Signal execution failed: %s", result.reason)

    def _maybe_print_stats(self) -> None:
        now = time.time()
        if now - self._last_stats_time >= 60:
            uptime = now - self.start_time
            print_stats(
                self.orchestrator,
                self.executor,
                self.resolution_monitor,
                self.position_closer,
                uptime,
                paper_wallet_snapshot=self.last_wallet_snapshot,
            )
            self._last_stats_time = now

    def shutdown(self) -> None:
        """Print final stats."""
        uptime = time.time() - self.start_time
        print_stats(
            self.orchestrator,
            self.executor,
            self.resolution_monitor,
            self.position_closer,
            uptime,
            paper_wallet_snapshot=self.last_wallet_snapshot,
        )
        log.info("Bot stopped. Stay profitable! 🚀")


def main() -> None:
    """Main entry point for the trading bot."""
    settings = load_settings()
    setup_logging(settings.log_level)

    print_banner()

    log.info("Starting Polymarket Multi-Strategy Trading Bot")
    log.info("Mode: %s", settings.trading_mode.upper())
    log.info("Kill Switch: %s", "ENABLED" if settings.kill_switch else "DISABLED")
    log.info("Max Order Size: $%s", settings.max_order_usdc)
    log.info("Min Edge: %s¢", settings.min_edge_cents)
    log.info("Market Fetch Limit: %s (0=use DEFAULT_FETCH_LIMIT)", settings.market_fetch_limit)
    log.info("Min Market Volume: $%s", f"{settings.min_market_volume:,.0f}")

    # Register signal handlers for graceful shutdown.
    if os.name == "nt":
        sig.signal(sig.SIGINT, sig.SIG_IGN)
    else:
        sig.signal(sig.SIGINT, signal_handler)
    sig.signal(sig.SIGTERM, signal_handler)

    runner = BotRunner(settings)
    runner.run()

    # Shutdown
    log.info("Shutting down gracefully...")
    runner.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("Fatal error: %s", e)
        sys.exit(1)
