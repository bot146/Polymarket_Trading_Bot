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

from polymarket_bot.clob_client import build_clob_client
from polymarket_bot.config import load_settings
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
        enable_stat_arb=False,  # Disabled by default - more complex
    )
    
    orchestrator = StrategyOrchestrator(settings, orch_config)
    executor = UnifiedExecutor(client, settings, position_manager=position_manager)
    
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
            if loop_start - last_position_close_check >= 30:  # Every 30 seconds
                # Get current price data (would need to fetch from market)
                price_data = {}  # TODO: Fetch current prices
                close_results = position_closer.check_and_close_positions(price_data)
                if close_results:
                    successful_closes = [r for r in close_results if r.success]
                    if successful_closes:
                        log.info(f"✅ Closed {len(successful_closes)} positions")
                last_position_close_check = loop_start
            
            # Run strategy scan
            signals = orchestrator.run_once()
            
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
