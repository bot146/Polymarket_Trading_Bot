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
from polymarket_bot.logging import setup_logging
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
    print(banner)


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

    if paper_wallet_snapshot is not None:
        log.info(
            "💼 WALLET: equity=$%.2f start=$%.2f adj=$%.2f size_mult=x%.2f dyn_max=$%.2f",
            paper_wallet_snapshot.get("equity", 0.0),
            paper_wallet_snapshot.get("starting_balance", 0.0),
            paper_wallet_snapshot.get("manual_adjustment", 0.0),
            paper_wallet_snapshot.get("multiplier", 1.0),
            paper_wallet_snapshot.get("dynamic_max_order_usdc", 0.0),
        )


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


def main() -> None:
    """Main entry point for the trading bot."""
    # Setup
    settings = load_settings()
    setup_logging(settings.log_level)
    
    print_banner()
    
    log.info("Starting Polymarket Multi-Strategy Trading Bot")
    log.info(f"Mode: {settings.trading_mode.upper()}")
    log.info(f"Kill Switch: {'ENABLED' if settings.kill_switch else 'DISABLED'}")
    log.info(f"Max Order Size: ${settings.max_order_usdc}")
    log.info(f"Min Edge: {settings.min_edge_cents}¢")
    log.info(f"Market Fetch Limit: {settings.market_fetch_limit} (0=use DEFAULT_FETCH_LIMIT)")
    log.info(f"Min Market Volume: ${settings.min_market_volume:,.0f}")
    
    # Register signal handlers for graceful shutdown.
    # On Windows, VS Code terminals send spurious SIGINT to background
    # processes.  We ignore SIGINT and rely solely on SIGTERM (or
    # Ctrl+Break) to stop the bot.
    if os.name == "nt":
        sig.signal(sig.SIGINT, sig.SIG_IGN)
    else:
        sig.signal(sig.SIGINT, signal_handler)
    sig.signal(sig.SIGTERM, signal_handler)
    
    # Initialize position management
    storage_path = Path.home() / ".polymarket_bot" / "positions.json"
    position_manager = PositionManager(storage_path=str(storage_path))

    # Paper mode always starts clean when configured.
    if settings.trading_mode == "paper" and settings.paper_reset_on_start:
        position_manager.reset_all_positions()
    log.info(f"✅ Position manager initialized ({len(position_manager.positions)} positions loaded)")
    
    # Initialize CLOB client
    client = None
    if settings.poly_private_key:
        try:
            client, creds = build_clob_client(settings)
            log.info("✅ CLOB client initialized")
        except Exception as e:
            log.warning(f"⚠️  CLOB client init failed (running in data-only mode): {e}")
    else:
        log.warning("⚠️  No private key provided - running in data-only mode")
    
    # Initialize market scanner
    scanner = MarketScanner()
    
    # Initialize resolution monitor
    resolution_monitor = ResolutionMonitor(
        position_manager=position_manager,
        scanner=scanner,
        check_interval=60.0,  # Check every minute
    )
    log.info("✅ Resolution monitor initialized")
    
    # Initialize position closer
    position_closer = PositionCloser(
        client=client,
        settings=settings,
        position_manager=position_manager,
        resolution_monitor=resolution_monitor,
    )
    log.info("✅ Position closer initialized")
    
    # Initialize orchestrator and executor
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
        # New strategies — toggled via .env
        enable_conditional_arb=settings.enable_conditional_arb,
        enable_liquidity_rewards=settings.enable_liquidity_rewards,
        enable_near_resolution=settings.enable_near_resolution,
        enable_arb_stacking=settings.enable_arb_stacking,
        max_arb_stacks=settings.max_arb_stacks,
    )
    
    orchestrator = StrategyOrchestrator(settings, orch_config)
    executor = UnifiedExecutor(client, settings, position_manager=position_manager)

    # Paper/live wallet controller: equity-based sizing with runtime-editable tiers.
    paper_wallet: PaperWalletController | None = None
    last_wallet_snapshot: dict[str, float] | None = None
    if settings.trading_mode in {"paper", "live"}:
        wallet_path = (
            Path(settings.paper_wallet_path)
            if settings.paper_wallet_path
            else (Path.home() / ".polymarket_bot" / "paper_wallet.json")
        )
        paper_wallet = PaperWalletController(
            file_path=wallet_path,
            default_starting_balance=settings.paper_start_balance,
            default_tier_spec=settings.paper_sizing_tiers,
            refresh_seconds=settings.paper_wallet_refresh_seconds,
        )
        if settings.trading_mode == "paper" and settings.paper_reset_on_start:
            # Force clean $100 baseline on each paper restart.
            wallet_path.parent.mkdir(parents=True, exist_ok=True)
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
        paper_wallet.ensure_file()
        log.info("💼 Paper wallet config: %s", wallet_path)
    
    # Restore dedup state from persisted open positions.
    # In paper mode with reset-on-start enabled this list is empty by design.
    # In live mode we intentionally restore so restarts preserve exposure state.
    open_positions = [p for p in position_manager.get_open_positions() if p.condition_id]
    for p in open_positions:
        orchestrator.mark_position_active(p.condition_id)
    if open_positions:
        if settings.trading_mode == "live":
            log.info("✅ Live startup: restored %d active position entries", len(open_positions))
        else:
            log.info("✅ Restored %d active position entries from disk", len(open_positions))

    # Start dashboard if enabled
    dashboard: Dashboard | None = None
    if settings.enable_dashboard:
        try:
            dashboard = Dashboard(
                host=settings.dashboard_host,
                port=settings.dashboard_port,
                executor=executor,
                orchestrator=orchestrator,
            )
            dashboard.start()
        except Exception as e:
            log.warning("⚠️  Dashboard failed to start: %s", e)

    log.info("🚀 Bot initialized with full trade lifecycle. Starting main loop...")
    
    # Main loop
    start_time = time.time()
    last_stats_time = start_time
    last_resolution_check = start_time
    last_position_close_check = start_time
    last_runtime_reload_check = 0.0
    last_live_wallet_refresh_check = 0.0
    last_live_available_collateral: _Decimal | None = None
    iteration = 0
    
    while not _shutdown_event.is_set():
        try:
            iteration += 1
            loop_start = time.time()

            # Optional low-overhead runtime env reload (both paper and live).
            if settings.runtime_reload_env and (loop_start - last_runtime_reload_check) >= settings.runtime_reload_seconds:
                runtime_settings = load_settings()
                last_runtime_reload_check = loop_start
            else:
                runtime_settings = settings
            
            # Check for market resolutions
            if loop_start - last_resolution_check >= 60:  # Every minute
                resolution_events = resolution_monitor.check_resolutions()
                if resolution_events:
                    log.info(f"🎯 Detected {len(resolution_events)} newly resolved markets")
                last_resolution_check = loop_start
            
            # Check and close positions
            if loop_start - last_position_close_check >= settings.exit_check_interval_seconds:
                # Build price_data from live top-of-book feed
                tob_snap = orchestrator.get_top_of_book_snapshot()
                price_data: dict[str, _Decimal] = {}
                # Use mid-price (avg of bid/ask) for P&L; fallback to ask if no bid
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
                # Snapshot position_id -> condition_id before closes mutate storage.
                pos_by_id = {
                    p.position_id: p.condition_id
                    for p in position_manager.get_open_positions() + position_manager.get_redeemable_positions()
                    if p.position_id and p.condition_id
                }

                close_results = position_closer.check_and_close_positions(price_data)
                if close_results:
                    successful_closes = [r for r in close_results if r.success]
                    if successful_closes:
                        for r in successful_closes:
                            cid = pos_by_id.get(r.position_id)
                            if cid:
                                orchestrator.mark_position_closed(cid)
                        log.info(f"✅ Closed {len(successful_closes)} positions")
                last_position_close_check = loop_start
            
            # Run strategy scan
            if settings.trading_mode in {"paper", "live"} and paper_wallet is not None:
                portfolio = position_manager.get_portfolio_stats()

                if settings.trading_mode == "paper":
                    paper_wallet.refresh()
                    snap = paper_wallet.snapshot(portfolio_stats=portfolio)
                    paper_wallet.maybe_log_tier_change(snap)
                    equity_cap = snap.equity
                    multiplier = snap.multiplier
                    starting_balance = snap.starting_balance
                    manual_adjustment = snap.manual_adjustment
                else:
                    # LIVE: use real collateral balance as hard cap source and include
                    # currently-open cost basis to estimate account equity.
                    refresh_interval = max(1.0, float(settings.paper_wallet_refresh_seconds))
                    if client is not None and (loop_start - last_live_wallet_refresh_check) >= refresh_interval:
                        last_live_wallet_refresh_check = loop_start
                        try:
                            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

                            resp = client.get_balance_allowance(
                                BalanceAllowanceParams(
                                    asset_type=AssetType.COLLATERAL,
                                    token_id=None,
                                    signature_type=-1,
                                )
                            )
                            live_available = _extract_live_available_collateral(resp)
                            if live_available is not None:
                                last_live_available_collateral = live_available
                        except Exception as e:
                            log.warning("Could not refresh live collateral balance; keeping last value: %s", e)

                    open_cost = _Decimal(str(portfolio.get("total_cost_basis", 0)))
                    unrealized_pnl = _Decimal(str(portfolio.get("total_unrealized_pnl", 0)))
                    available_collateral = last_live_available_collateral
                    if available_collateral is None:
                        # Conservative fallback until first successful wallet read.
                        available_collateral = _Decimal(str(settings.paper_start_balance))

                    # Mark-to-market live equity cap:
                    # cash/collateral + marked value of open positions.
                    equity_cap = (available_collateral + open_cost + unrealized_pnl).quantize(_Decimal("0.01"))
                    if equity_cap < _Decimal("0"):
                        equity_cap = _Decimal("0")
                    multiplier, _tier_floor = _compute_multiplier_for_equity(
                        equity_cap,
                        settings.paper_sizing_tiers,
                    )
                    starting_balance = available_collateral
                    manual_adjustment = _Decimal("0")

                executor.set_equity_cap(equity_cap)
                dynamic_max = (runtime_settings.max_order_usdc * multiplier).quantize(_Decimal("0.01"))
                orchestrator.set_dynamic_sizing_params(
                    max_order_usdc=dynamic_max,
                    min_order_usdc=runtime_settings.min_order_usdc,
                    initial_order_pct=runtime_settings.initial_order_pct,
                )
                last_wallet_snapshot = {
                    "equity": float(equity_cap),
                    "starting_balance": float(starting_balance),
                    "manual_adjustment": float(manual_adjustment),
                    "multiplier": float(multiplier),
                    "dynamic_max_order_usdc": float(dynamic_max),
                }

            signals = orchestrator.run_once()

            # In PAPER mode, advance the paper fill simulator for resting maker
            # orders using the latest top-of-book snapshot.
            if settings.trading_mode == "paper":
                tob = orchestrator.get_top_of_book_snapshot()
                best_bid_map = tob.get("best_bid", {})
                best_ask_map = dict(tob.get("best_ask", {}))

                # Merge CLOB cache into ask map so paper FOK fills can see
                # executable ask prices for negRisk tokens.
                # CLOB books are authoritative — they represent real executable
                # asks, while WSS mid-prices can be far off for low-probability
                # brackets (e.g. WSS mid=0.999 but CLOB ask=0.021).
                clob_cache = getattr(orchestrator, "_clob_cache", {})
                for tid, price in clob_cache.items():
                    if price is not None:
                        best_ask_map[tid] = price  # CLOB always overrides WSS

                if best_bid_map or best_ask_map:
                    token_ids = set(best_bid_map.keys()) | set(best_ask_map.keys())
                    for token_id in token_ids:
                        bid = best_bid_map.get(token_id)
                        ask = best_ask_map.get(token_id)
                        executor.on_market_update(
                            token_id=token_id,
                            best_bid=_Decimal(str(bid)) if bid is not None else None,
                            best_ask=_Decimal(str(ask)) if ask is not None else None,
                            best_ask_by_token={k: _Decimal(str(v)) for k, v in best_ask_map.items() if v is not None},
                        )
            
            # Execute signals
            enabled_by_name = {s.name: s for s in orchestrator.registry.get_enabled()}
            for signal in signals:
                if _shutdown_event.is_set():
                    break
                
                # Get the strategy that generated this signal
                strategy = enabled_by_name.get(signal.opportunity.strategy_type.value)
                
                if not strategy:
                    log.warning(f"No strategy found for signal type: {signal.opportunity.strategy_type}")
                    continue

                # Execute
                result = executor.execute_signal(signal, strategy)
                
                if result.success:
                    condition_id = signal.opportunity.metadata.get("condition_id")
                    if condition_id:
                        orchestrator.mark_position_active(condition_id)
                    orchestrator.total_signals_executed += 1
                    log.info(f"✅ Paper trade executed: {signal.opportunity.strategy_type.value} profit=${signal.opportunity.expected_profit:.4f}")
                else:
                    log.warning(f"❌ Signal execution failed: {result.reason}")
            
            # Print stats periodically
            now = time.time()
            if now - last_stats_time >= 60:  # Every minute
                uptime = now - start_time
                print_stats(
                    orchestrator,
                    executor,
                    resolution_monitor,
                    position_closer,
                    uptime,
                    paper_wallet_snapshot=last_wallet_snapshot,
                )
                last_stats_time = now
            
            # Log iteration info
            if signals:
                log.info(f"Iteration {iteration}: processed {len(signals)} signals")
            
            # Sleep until next scan (use Event.wait instead of time.sleep
            # to avoid KeyboardInterrupt / spurious SIGINT on Windows)
            elapsed = time.time() - loop_start
            sleep_time = max(0, orch_config.scan_interval - elapsed)
            if sleep_time > 0:
                _shutdown_event.wait(timeout=sleep_time)
                
        except KeyboardInterrupt:
            log.warning("Keyboard interrupt received")
            _shutdown_event.set()
            break
        except Exception as e:
            log.exception(f"Error in main loop: {e}")
            _shutdown_event.wait(timeout=5)  # Back off on errors
    
    # Shutdown
    log.info("Shutting down gracefully...")
    uptime = time.time() - start_time
    print_stats(
        orchestrator,
        executor,
        resolution_monitor,
        position_closer,
        uptime,
        paper_wallet_snapshot=last_wallet_snapshot,
    )
    log.info("Bot stopped. Stay profitable! 🚀")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        sys.exit(1)
