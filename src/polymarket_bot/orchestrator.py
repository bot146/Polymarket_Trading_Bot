"""Multi-strategy orchestrator for Polymarket trading bot.

This module coordinates multiple strategies and manages their execution.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from polymarket_bot.config import Settings
from polymarket_bot.scanner import MarketScanner
from polymarket_bot.strategy import StrategyRegistry, StrategySignal
from polymarket_bot.strategies.arbitrage_strategy import ArbitrageStrategy
from polymarket_bot.strategies.guaranteed_win_strategy import GuaranteedWinStrategy
from polymarket_bot.strategies.statistical_arbitrage_strategy import StatisticalArbitrageStrategy

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrchestratorConfig:
    """Configuration for the strategy orchestrator."""
    scan_interval: float = 2.0  # Seconds between scans
    max_concurrent_trades: int = 5  # Max number of simultaneous positions
    enable_arbitrage: bool = True
    enable_guaranteed_win: bool = True
    enable_stat_arb: bool = False  # More complex, disabled by default
    
    # Market scanning
    scan_high_volume: bool = True
    scan_resolved: bool = True
    # min_volume will be taken from Settings.min_market_volume
    
    def __post_init__(self):
        """Validate configuration values."""
        if self.scan_interval <= 0:
            raise ValueError("scan_interval must be positive")
        if self.max_concurrent_trades < 0:
            raise ValueError("max_concurrent_trades must be non-negative")


class StrategyOrchestrator:
    """Orchestrates multiple trading strategies."""

    def __init__(self, settings: Settings, config: OrchestratorConfig | None = None):
        self.settings = settings
        self.config = config or OrchestratorConfig()
        
        # Initialize components with settings
        self.scanner = MarketScanner(fetch_limit=settings.market_fetch_limit)
        self.registry = StrategyRegistry()
        
        # Initialize strategies
        self._init_strategies()
        
        # State tracking
        self.active_positions: list[str] = []  # Track active condition_ids
        self.total_signals_seen = 0
        self.total_signals_executed = 0

    def _init_strategies(self) -> None:
        """Initialize and register trading strategies."""
        if self.config.enable_arbitrage:
            arb_strategy = ArbitrageStrategy(
                min_edge_cents=self.settings.min_edge_cents,
                max_order_usdc=self.settings.max_order_usdc,
                enabled=True,
            )
            self.registry.register(arb_strategy)
            log.info("Registered: ArbitrageStrategy")

        if self.config.enable_guaranteed_win:
            gw_strategy = GuaranteedWinStrategy(
                min_discount_cents=Decimal("5.0"),
                max_order_usdc=self.settings.max_order_usdc * Decimal("2"),  # More capital for guaranteed wins
                enabled=True,
            )
            self.registry.register(gw_strategy)
            log.info("Registered: GuaranteedWinStrategy")

        if self.config.enable_stat_arb:
            stat_arb_strategy = StatisticalArbitrageStrategy(
                max_order_usdc=self.settings.max_order_usdc,
                enabled=True,
            )
            self.registry.register(stat_arb_strategy)
            log.info("Registered: StatisticalArbitrageStrategy")

    def scan_and_collect_signals(self) -> list[StrategySignal]:
        """Scan markets and collect signals from all strategies."""
        market_data = self._gather_market_data()
        
        # Run all strategies
        signals = self.registry.scan_all(market_data)
        self.total_signals_seen += len(signals)
        
        if signals:
            log.info(f"Found {len(signals)} total signals across all strategies")
        
        return signals

    def prioritize_signals(self, signals: list[StrategySignal]) -> list[StrategySignal]:
        """Prioritize signals by urgency and expected profit."""
        # Sort by urgency (descending), then expected profit (descending)
        sorted_signals = sorted(
            signals,
            key=lambda s: (s.opportunity.urgency, s.opportunity.expected_profit),
            reverse=True
        )
        return sorted_signals

    def filter_signals(self, signals: list[StrategySignal]) -> list[StrategySignal]:
        """Filter signals based on current state and constraints."""
        filtered = []
        
        for signal in signals:
            # Skip if we already have a position in this market
            condition_id = signal.opportunity.metadata.get("condition_id")
            if condition_id and condition_id in self.active_positions:
                log.debug(f"Skipping {condition_id[:8]}... - already have position")
                continue
            
            # Check concurrent trade limit
            if len(self.active_positions) >= self.config.max_concurrent_trades:
                log.info(f"Max concurrent trades reached ({self.config.max_concurrent_trades})")
                break
            
            filtered.append(signal)
        
        return filtered

    def _gather_market_data(self) -> dict[str, Any]:
        """Gather market data from scanner."""
        market_data: dict[str, Any] = {"markets": [], "resolved_markets": []}
        
        try:
            # Scan high-volume active markets
            if self.config.scan_high_volume:
                high_vol_markets = self.scanner.get_high_volume_markets(
                    min_volume=self.settings.min_market_volume,  # Use settings
                    limit=None  # Scan all markets above volume threshold
                )
                
                # Convert to dict format for strategies
                for market in high_vol_markets:
                    market_dict = {
                        "condition_id": market.condition_id,
                        "question": market.question,
                        "volume": float(market.volume),
                        "active": market.active,
                        "tokens": [
                            {
                                "token_id": token.token_id,
                                "outcome": token.outcome,
                                "price": float(token.price),
                                "best_ask": float(token.price),  # Scanner doesn't have bid/ask yet
                                "volume": float(token.volume),
                            }
                            for token in market.tokens
                        ],
                    }
                    market_data["markets"].append(market_dict)
            
            # Scan resolved markets for guaranteed wins
            if self.config.scan_resolved:
                resolved_markets = self.scanner.get_resolved_markets(limit=None)  # Scan all resolved markets
                
                for market in resolved_markets:
                    market_dict = {
                        "condition_id": market.condition_id,
                        "question": market.question,
                        "resolved": market.resolved,
                        "winning_outcome": market.winning_outcome,
                        "tokens": [
                            {
                                "token_id": token.token_id,
                                "outcome": token.outcome,
                                "price": float(token.price),
                                "best_ask": float(token.price),
                            }
                            for token in market.tokens
                        ],
                    }
                    market_data["resolved_markets"].append(market_dict)
                    
            log.debug(
                f"Gathered {len(market_data['markets'])} active markets, "
                f"{len(market_data['resolved_markets'])} resolved markets"
            )
            
        except Exception as e:
            log.error(f"Failed to gather market data: {e}")
        
        return market_data

    def run_once(self) -> list[StrategySignal]:
        """Run one iteration of strategy scanning.
        
        Returns:
            List of prioritized, filtered signals ready for execution.
        """
        # Collect signals from all strategies
        signals = self.scan_and_collect_signals()
        
        # Prioritize by urgency and profit
        signals = self.prioritize_signals(signals)
        
        # Filter based on constraints
        signals = self.filter_signals(signals)
        
        return signals

    def mark_position_active(self, condition_id: str) -> None:
        """Mark a market as having an active position."""
        if condition_id not in self.active_positions:
            self.active_positions.append(condition_id)

    def mark_position_closed(self, condition_id: str) -> None:
        """Mark a market position as closed."""
        if condition_id in self.active_positions:
            self.active_positions.remove(condition_id)

    def get_stats(self) -> dict[str, Any]:
        """Get orchestrator statistics."""
        return {
            "total_signals_seen": self.total_signals_seen,
            "total_signals_executed": self.total_signals_executed,
            "active_positions": len(self.active_positions),
            "enabled_strategies": len(self.registry.get_enabled()),
        }
