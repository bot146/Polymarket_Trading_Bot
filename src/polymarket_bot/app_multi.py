"""Enhanced multi-strategy Polymarket trading bot.

This is the main application that orchestrates multiple trading strategies
and executes them safely with proper risk management.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from decimal import Decimal

from polymarket_bot.clob_client import build_clob_client
from polymarket_bot.config import load_settings
from polymarket_bot.logging import setup_logging
from polymarket_bot.orchestrator import OrchestratorConfig, StrategyOrchestrator
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
║     Strategies: Arbitrage, Guaranteed Win, Stat Arb         ║
║     Built with precision. Designed for profit.              ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
"""
    print(banner)


def print_stats(orchestrator: StrategyOrchestrator, executor: UnifiedExecutor, uptime: float):
    """Print bot statistics."""
    orch_stats = orchestrator.get_stats()
    exec_stats = executor.get_stats()
    
    log.info("=" * 70)
    log.info(f"⏱️  UPTIME: {uptime/60:.1f} minutes")
    log.info(f"📊 SIGNALS: seen={orch_stats['total_signals_seen']} executed={orch_stats['total_signals_executed']}")
    log.info(f"📈 EXECUTIONS: total={exec_stats['total_executions']} success={exec_stats['successful']} failed={exec_stats['failed']}")
    log.info(f"💼 ACTIVE POSITIONS: {orch_stats['active_positions']}")
    log.info(f"🎯 STRATEGIES: {orch_stats['enabled_strategies']} enabled")
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
    
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
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
    
    # Initialize orchestrator and executor
    orch_config = OrchestratorConfig(
        scan_interval=2.0,
        max_concurrent_trades=5,
        enable_arbitrage=True,
        enable_guaranteed_win=True,
        enable_stat_arb=False,  # Disabled by default - more complex
        min_volume=Decimal("5000"),
    )
    
    orchestrator = StrategyOrchestrator(settings, orch_config)
    executor = UnifiedExecutor(client, settings)
    
    log.info("🚀 Bot initialized. Starting main loop...")
    
    # Main loop
    start_time = time.time()
    last_stats_time = start_time
    iteration = 0
    
    while not _shutdown_requested:
        try:
            iteration += 1
            loop_start = time.time()
            
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
                    # Mark position as active
                    condition_id = signal.opportunity.metadata.get("condition_id")
                    if condition_id:
                        orchestrator.mark_position_active(condition_id)
            
            # Print stats periodically
            now = time.time()
            if now - last_stats_time >= 60:  # Every minute
                uptime = now - start_time
                print_stats(orchestrator, executor, uptime)
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
    print_stats(orchestrator, executor, uptime)
    log.info("Bot stopped. Stay profitable! 🚀")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        sys.exit(1)
