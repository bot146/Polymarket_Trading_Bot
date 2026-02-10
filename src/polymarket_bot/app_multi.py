"""Enhanced multi-strategy Polymarket trading bot with complete trade lifecycle.

This is the main application that orchestrates multiple trading strategies
and executes them safely with proper risk management, position tracking,
and automatic position closing.
"""

from __future__ import annotations

import logging
import signal as sig
import sys
import time
from decimal import Decimal
from pathlib import Path
from decimal import Decimal as _Decimal

from polymarket_bot.clob_client import build_clob_client
from polymarket_bot.config import load_settings
from polymarket_bot.dashboard import Dashboard
from polymarket_bot.logging import setup_logging
from polymarket_bot.orchestrator import OrchestratorConfig, StrategyOrchestrator
from polymarket_bot.position_closer import PositionCloser
from polymarket_bot.position_manager import PositionManager
from polymarket_bot.resolution_monitor import ResolutionMonitor
from polymarket_bot.scanner import MarketScanner
from polymarket_bot.unified_executor import UnifiedExecutor

log = logging.getLogger(__name__)

# Global flag for graceful shutdown
_shutdown_requested = False


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    global _shutdown_requested
    log.warning(f"Shutdown signal received: {signum}")
    _shutdown_requested = True


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
    uptime: float
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
    
    # Register signal handlers for graceful shutdown
    sig.signal(sig.SIGINT, signal_handler)
    sig.signal(sig.SIGTERM, signal_handler)
    
    # Initialize position management
    storage_path = Path.home() / ".polymarket_bot" / "positions.json"
    position_manager = PositionManager(storage_path=str(storage_path))
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
        max_concurrent_trades=5,
        enable_arbitrage=True,
        enable_guaranteed_win=True,
        enable_multi_outcome_arb=True,  # Buy all YES tokens in a group for < $1
        # Speculative strategies disabled — focus on guaranteed-profit only
        enable_stat_arb=False,
        enable_value_betting=False,
        enable_sniping=False,
        enable_market_making=False,
    )
    
    orchestrator = StrategyOrchestrator(settings, orch_config)
    executor = UnifiedExecutor(client, settings, position_manager=position_manager)
    
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
    iteration = 0
    
    while not _shutdown_requested:
        try:
            iteration += 1
            loop_start = time.time()
            
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
                close_results = position_closer.check_and_close_positions(price_data)
                if close_results:
                    successful_closes = [r for r in close_results if r.success]
                    if successful_closes:
                        log.info(f"✅ Closed {len(successful_closes)} positions")
                last_position_close_check = loop_start
            
            # Run strategy scan
            signals = orchestrator.run_once()

            # In PAPER mode, advance the paper fill simulator for resting maker
            # orders using the latest top-of-book snapshot.
            if settings.trading_mode == "paper":
                tob = orchestrator.get_top_of_book_snapshot()
                best_bid_map = tob.get("best_bid", {})
                best_ask_map = dict(tob.get("best_ask", {}))

                # Merge CLOB cache into ask map so paper FOK fills can see
                # executable ask prices for negRisk tokens.
                clob_cache = getattr(orchestrator, "_clob_cache", {})
                for tid, price in clob_cache.items():
                    if tid not in best_ask_map or best_ask_map.get(tid) is None:
                        best_ask_map[tid] = price

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
            for signal in signals:
                if _shutdown_requested:
                    break
                
                # Get the strategy that generated this signal
                strategy = None
                for strat in orchestrator.registry.get_enabled():
                    if strat.name == signal.opportunity.strategy_type.value:
                        strategy = strat
                        break
                
                if not strategy:
                    log.warning(f"No strategy found for signal type: {signal.opportunity.strategy_type}")
                    continue
                
                # Execute
                result = executor.execute_signal(signal, strategy)
                
                if result.success:
                    orchestrator.total_signals_executed += 1
                    # Mark position as active (for orchestrator tracking)
                    condition_id = signal.opportunity.metadata.get("condition_id")
                    if condition_id:
                        orchestrator.mark_position_active(condition_id)
            
            # Print stats periodically
            now = time.time()
            if now - last_stats_time >= 60:  # Every minute
                uptime = now - start_time
                print_stats(orchestrator, executor, resolution_monitor, position_closer, uptime)
                last_stats_time = now
            
            # Log iteration info
            if signals:
                log.info(f"Iteration {iteration}: processed {len(signals)} signals")
            
            # Sleep until next scan
            elapsed = time.time() - loop_start
            sleep_time = max(0, orch_config.scan_interval - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)
                
        except KeyboardInterrupt:
            log.warning("Keyboard interrupt received")
            break
        except Exception as e:
            log.exception(f"Error in main loop: {e}")
            time.sleep(5)  # Back off on errors
    
    # Shutdown
    log.info("Shutting down gracefully...")
    uptime = time.time() - start_time
    print_stats(orchestrator, executor, resolution_monitor, position_closer, uptime)
    log.info("Bot stopped. Stay profitable! 🚀")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        sys.exit(1)
