"""Enhanced unified executor for multi-strategy trading.

This module handles execution of trading signals from multiple strategies
with proper validation, risk management, and error handling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs

from polymarket_bot.config import Settings, is_live
from polymarket_bot.strategy import Strategy, StrategySignal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionResult:
    """Result of executing a strategy signal."""
    success: bool
    reason: str
    signal: StrategySignal
    order_ids: list[str] | None = None
    error: str | None = None


class UnifiedExecutor:
    """Unified executor for all trading strategies."""

    def __init__(self, client: ClobClient | None, settings: Settings):
        self.client = client
        self.settings = settings
        self.execution_count = 0
        self.success_count = 0
        self.failure_count = 0
        
        # Paper trading profitability tracking
        self.paper_total_profit = Decimal("0")
        self.paper_total_cost = Decimal("0")
        self.paper_trades_by_strategy: dict[str, dict] = {}

    def execute_signal(
        self,
        signal: StrategySignal,
        strategy: Strategy,
    ) -> ExecutionResult:
        """Execute a trading signal.
        
        Args:
            signal: The trading signal to execute.
            strategy: The strategy that generated the signal.
            
        Returns:
            ExecutionResult with success status and details.
        """
        self.execution_count += 1

        # Pre-execution validation
        if self.settings.kill_switch:
            return ExecutionResult(
                success=False,
                reason="kill_switch_enabled",
                signal=signal,
            )

        # Validate with strategy
        valid, reason = strategy.validate(signal)
        if not valid:
            log.warning(f"Signal validation failed: {reason}")
            return ExecutionResult(
                success=False,
                reason=f"validation_failed_{reason}",
                signal=signal,
            )

        # Check if we have a client for live trading
        if not self.client:
            return self._paper_trade(signal)

        # Execute based on trading mode
        if is_live(self.settings):
            return self._live_trade(signal)
        else:
            return self._paper_trade(signal)

    def _paper_trade(self, signal: StrategySignal) -> ExecutionResult:
        """Simulate trade execution in paper mode."""
        strategy_type = signal.opportunity.strategy_type.value
        profit = signal.opportunity.expected_profit
        confidence = signal.opportunity.confidence
        cost = signal.max_total_cost
        
        # Track profitability
        self.paper_total_profit += profit
        self.paper_total_cost += cost
        
        # Track by strategy
        if strategy_type not in self.paper_trades_by_strategy:
            self.paper_trades_by_strategy[strategy_type] = {
                "count": 0,
                "total_profit": Decimal("0"),
                "total_cost": Decimal("0"),
            }
        
        self.paper_trades_by_strategy[strategy_type]["count"] += 1
        self.paper_trades_by_strategy[strategy_type]["total_profit"] += profit
        self.paper_trades_by_strategy[strategy_type]["total_cost"] += cost
        
        log.warning(
            f"📄 PAPER TRADE [{strategy_type}]: "
            f"profit=${profit:.4f} cost=${cost:.2f} confidence={confidence:.2%} "
            f"trades={len(signal.trades)}"
        )
        
        for i, trade in enumerate(signal.trades):
            log.info(
                f"  Trade {i+1}: {trade.side} {trade.size:.2f} @ ${trade.price:.4f} "
                f"token={trade.token_id[:8]}... type={trade.order_type}"
            )
        
        # Show running total
        roi = (self.paper_total_profit / self.paper_total_cost * 100) if self.paper_total_cost > 0 else Decimal("0")
        log.info(
            f"  💰 Running Total: profit=${self.paper_total_profit:.4f} "
            f"cost=${self.paper_total_cost:.2f} ROI={roi:.2f}%"
        )
        
        return ExecutionResult(
            success=True,
            reason="paper_mode_simulated",
            signal=signal,
        )

    def _live_trade(self, signal: StrategySignal) -> ExecutionResult:
        """Execute trades in live mode."""
        if not self.client:
            return ExecutionResult(
                success=False,
                reason="no_client",
                signal=signal,
                error="Client not initialized",
            )

        order_ids = []
        
        try:
            for trade in signal.trades:
                # Create order
                order_args = OrderArgs(
                    price=float(trade.price),
                    size=float(trade.size),
                    side=trade.side,
                    token_id=trade.token_id,
                )
                
                # Sign order
                signed_order = self.client.create_order(order_args)
                
                # Post order
                response = self.client.post_order(signed_order, orderType=trade.order_type)  # type: ignore[arg-type]
                
                # Extract order ID
                order_id = self._extract_order_id(response)
                if order_id:
                    order_ids.append(order_id)
                
                log.info(
                    f"✅ LIVE ORDER: {trade.side} {trade.size:.2f} @ ${trade.price:.4f} "
                    f"order_id={order_id}"
                )
            
            self.success_count += 1
            
            return ExecutionResult(
                success=True,
                reason="live_executed",
                signal=signal,
                order_ids=order_ids,
            )
            
        except Exception as e:
            self.failure_count += 1
            log.exception(f"Live execution failed: {e}")
            
            return ExecutionResult(
                success=False,
                reason="live_execution_error",
                signal=signal,
                error=str(e),
            )

    def _extract_order_id(self, response: object) -> str | None:
        """Extract order ID from various response formats.
        
        The py-clob-client library may return different formats:
        - dict with "orderID" or "orderId" key
        - object with orderID or orderId attribute
        """
        try:
            if isinstance(response, dict):
                return response.get("orderID") or response.get("orderId")
            return getattr(response, "orderID", None) or getattr(response, "orderId", None)
        except Exception as e:
            log.warning(f"Failed to extract order ID from response: {e}")
            return None

    def get_stats(self) -> dict:
        """Get executor statistics including profitability."""
        stats = {
            "total_executions": self.execution_count,
            "successful": self.success_count,
            "failed": self.failure_count,
            "paper_total_profit": float(self.paper_total_profit),
            "paper_total_cost": float(self.paper_total_cost),
            "paper_roi": float((self.paper_total_profit / self.paper_total_cost * 100) if self.paper_total_cost > 0 else Decimal("0")),
            "paper_trades_by_strategy": {
                strategy: {
                    "count": data["count"],
                    "total_profit": float(data["total_profit"]),
                    "total_cost": float(data["total_cost"]),
                    "roi": float((data["total_profit"] / data["total_cost"] * 100) if data["total_cost"] > 0 else Decimal("0")),
                }
                for strategy, data in self.paper_trades_by_strategy.items()
            },
        }
        return stats
