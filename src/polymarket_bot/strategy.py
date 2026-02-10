"""Strategy framework for Polymarket trading bot.

This module defines the abstract base class for all trading strategies
and provides a registry for strategy management.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any


class StrategyType(str, Enum):
    """Types of trading strategies."""
    ARBITRAGE = "arbitrage"
    GUARANTEED_WIN = "guaranteed_win"
    STATISTICAL_ARBITRAGE = "statistical_arbitrage"
    HIGH_FREQUENCY_SNIPING = "high_frequency_sniping"
    MARKET_MAKING = "market_making"
    SPREAD_FARMING = "spread_farming"
    COPY_TRADING = "copy_trading"
    AI_PROBABILITY = "ai_probability"
    MULTI_OUTCOME_ARB = "multi_outcome_arb"


@dataclass(frozen=True)
class Opportunity:
    """Base class for trading opportunities."""
    strategy_type: StrategyType
    expected_profit: Decimal
    confidence: Decimal  # 0.0 to 1.0
    urgency: int  # 0=low, 10=critical
    metadata: dict[str, Any]


@dataclass(frozen=True)
class Trade:
    """Represents a trade to be executed."""
    token_id: str
    side: str  # "BUY" or "SELL"
    size: Decimal
    price: Decimal
    order_type: str = "FOK"  # FOK, IOC, GTC, etc.


@dataclass(frozen=True)
class StrategySignal:
    """Signal from a strategy to execute trades."""
    opportunity: Opportunity
    trades: list[Trade]
    max_total_cost: Decimal
    min_expected_return: Decimal


class Strategy(ABC):
    """Abstract base class for all trading strategies."""

    def __init__(self, name: str, enabled: bool = True):
        self.name = name
        self.enabled = enabled

    @abstractmethod
    def scan(self, market_data: dict[str, Any]) -> list[StrategySignal]:
        """Scan market data for opportunities.
        
        Args:
            market_data: Current market state including prices, volumes, etc.
            
        Returns:
            List of trading signals to be evaluated by executor.
        """
        pass

    @abstractmethod
    def validate(self, signal: StrategySignal) -> tuple[bool, str]:
        """Validate a signal before execution.
        
        Args:
            signal: The signal to validate.
            
        Returns:
            Tuple of (is_valid, reason).
        """
        pass


class StrategyRegistry:
    """Registry for managing multiple trading strategies."""

    def __init__(self):
        self._strategies: dict[str, Strategy] = {}

    def register(self, strategy: Strategy) -> None:
        """Register a strategy."""
        self._strategies[strategy.name] = strategy

    def unregister(self, name: str) -> None:
        """Unregister a strategy by name."""
        self._strategies.pop(name, None)

    def get(self, name: str) -> Strategy | None:
        """Get a strategy by name."""
        return self._strategies.get(name)

    def get_enabled(self) -> list[Strategy]:
        """Get all enabled strategies."""
        return [s for s in self._strategies.values() if s.enabled]

    def scan_all(self, market_data: dict[str, Any]) -> list[StrategySignal]:
        """Run all enabled strategies and collect signals."""
        signals = []
        for strategy in self.get_enabled():
            try:
                strategy_signals = strategy.scan(market_data)
                signals.extend(strategy_signals)
            except Exception as e:
                # Log but don't fail if one strategy errors
                import logging
                log = logging.getLogger(__name__)
                log.exception(f"Strategy {strategy.name} failed during scan: {e}")
        return signals
