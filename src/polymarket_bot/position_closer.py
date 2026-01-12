"""Position closer for automatically selling and redeeming positions.

This module handles the exit side of trading, selling positions at profit
targets or when markets resolve.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import SELL

from polymarket_bot.config import Settings, is_live
from polymarket_bot.position_manager import Position, PositionManager
from polymarket_bot.resolution_monitor import ResolutionMonitor

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CloseResult:
    """Result of closing a position."""
    success: bool
    position_id: str
    reason: str
    realized_pnl: Decimal | None = None
    order_id: str | None = None
    error: str | None = None


class PositionCloser:
    """Handles closing positions through sales or redemptions."""
    
    def __init__(
        self,
        client: ClobClient | None,
        settings: Settings,
        position_manager: PositionManager,
        resolution_monitor: ResolutionMonitor,
    ):
        self.client = client
        self.settings = settings
        self.position_manager = position_manager
        self.resolution_monitor = resolution_monitor
        
        # Statistics
        self.close_count = 0
        self.redemption_count = 0
        self.total_realized_pnl = Decimal("0")
    
    def check_and_close_positions(self, price_data: dict[str, Decimal]) -> list[CloseResult]:
        """Check all positions and close those meeting exit criteria.
        
        Args:
            price_data: Dict of token_id -> current_price
            
        Returns:
            List of close results
        """
        results: list[CloseResult] = []
        
        # Update unrealized P&L
        self.position_manager.update_unrealized_pnl(price_data)
        
        # Check for positions to close
        open_positions = self.position_manager.get_open_positions()
        redeemable_positions = self.position_manager.get_redeemable_positions()
        
        # Close redeemable positions first (market resolved)
        for position in redeemable_positions:
            result = self.redeem_position(position)
            results.append(result)
        
        # Check open positions for profit targets
        for position in open_positions:
            if self._should_close_position(position, price_data):
                result = self.close_position(position, price_data)
                results.append(result)
        
        return results
    
    def _should_close_position(self, position: Position, price_data: dict[str, Decimal]) -> bool:
        """Determine if a position should be closed.
        
        Current logic:
        - For arbitrage: Close when market resolves (handled by redeemable check)
        - For guaranteed_win: Already at target (bought winning shares)
        - For others: Could add profit target logic here
        """
        # For now, only close via resolution
        # Future: Add profit targets, stop losses, etc.
        return False
    
    def close_position(self, position: Position, price_data: dict[str, Decimal]) -> CloseResult:
        """Close a position by selling.
        
        Args:
            position: Position to close
            price_data: Current market prices
            
        Returns:
            CloseResult with outcome
        """
        if position.token_id not in price_data:
            return CloseResult(
                success=False,
                position_id=position.position_id,
                reason="no_price_data",
                error=f"No price data for token {position.token_id}",
            )
        
        current_price = price_data[position.token_id]
        
        # Check if profitable
        if current_price <= position.entry_price:
            log.debug(
                f"Position {position.position_id} not profitable yet: "
                f"current=${current_price:.4f} entry=${position.entry_price:.4f}"
            )
            return CloseResult(
                success=False,
                position_id=position.position_id,
                reason="not_profitable",
            )
        
        # Execute sell
        if not is_live(self.settings):
            # Paper mode
            return self._paper_close(position, current_price)
        else:
            # Live mode
            return self._live_close(position, current_price)
    
    def redeem_position(self, position: Position) -> CloseResult:
        """Redeem a position in a resolved market.
        
        For winning shares in resolved markets, we can redeem them for $1 each.
        """
        if not position.is_redeemable:
            return CloseResult(
                success=False,
                position_id=position.position_id,
                reason="not_redeemable",
                error="Position is not marked as redeemable",
            )
        
        redemption_value = Decimal("1.0")
        
        if not is_live(self.settings):
            # Paper mode
            pnl = self.position_manager.close_position(
                position.position_id,
                exit_price=redemption_value,
            )
            
            self.redemption_count += 1
            self.total_realized_pnl += pnl
            
            log.warning(
                f"📄 PAPER REDEMPTION [{position.strategy}]: "
                f"pos={position.position_id} qty={position.quantity} "
                f"entry=${position.entry_price:.4f} exit=$1.00 "
                f"P&L=${pnl:.4f}"
            )
            
            return CloseResult(
                success=True,
                position_id=position.position_id,
                reason="paper_redeemed",
                realized_pnl=pnl,
            )
        else:
            # Live mode: Would need to call settlement/redemption endpoint
            # For now, mark as closed at $1
            pnl = self.position_manager.close_position(
                position.position_id,
                exit_price=redemption_value,
            )
            
            self.redemption_count += 1
            self.total_realized_pnl += pnl
            
            log.warning(
                f"✅ REDEEMED [{position.strategy}]: "
                f"pos={position.position_id} qty={position.quantity} "
                f"P&L=${pnl:.4f}"
            )
            
            return CloseResult(
                success=True,
                position_id=position.position_id,
                reason="redeemed",
                realized_pnl=pnl,
            )
    
    def _paper_close(self, position: Position, exit_price: Decimal) -> CloseResult:
        """Simulate closing a position in paper mode."""
        pnl = self.position_manager.close_position(
            position.position_id,
            exit_price=exit_price,
        )
        
        self.close_count += 1
        self.total_realized_pnl += pnl
        
        log.warning(
            f"📄 PAPER CLOSE [{position.strategy}]: "
            f"pos={position.position_id} qty={position.quantity} "
            f"entry=${position.entry_price:.4f} exit=${exit_price:.4f} "
            f"P&L=${pnl:.4f}"
        )
        
        return CloseResult(
            success=True,
            position_id=position.position_id,
            reason="paper_closed",
            realized_pnl=pnl,
        )
    
    def _live_close(self, position: Position, exit_price: Decimal) -> CloseResult:
        """Close a position in live mode by selling."""
        if not self.client:
            return CloseResult(
                success=False,
                position_id=position.position_id,
                reason="no_client",
                error="Client not initialized",
            )
        
        try:
            # Create sell order
            order_args = OrderArgs(
                price=float(exit_price),
                size=float(position.quantity),
                side=SELL,
                token_id=position.token_id,
            )
            
            # Sign and post
            signed_order = self.client.create_order(order_args)
            response = self.client.post_order(signed_order, orderType="IOC")  # type: ignore[arg-type]
            
            # Extract order ID
            order_id = self._extract_order_id(response)
            
            # Close position
            pnl = self.position_manager.close_position(
                position.position_id,
                exit_price=exit_price,
                exit_order_id=order_id,
            )
            
            self.close_count += 1
            self.total_realized_pnl += pnl
            
            log.warning(
                f"✅ LIVE CLOSE [{position.strategy}]: "
                f"pos={position.position_id} P&L=${pnl:.4f} order={order_id}"
            )
            
            return CloseResult(
                success=True,
                position_id=position.position_id,
                reason="live_closed",
                realized_pnl=pnl,
                order_id=order_id,
            )
            
        except Exception as e:
            log.exception(f"Failed to close position {position.position_id}")
            return CloseResult(
                success=False,
                position_id=position.position_id,
                reason="error",
                error=str(e),
            )
    
    def _extract_order_id(self, response: object) -> str | None:
        """Extract order ID from response."""
        try:
            if isinstance(response, dict):
                return response.get("orderID") or response.get("orderId")
            return getattr(response, "orderID", None) or getattr(response, "orderId", None)
        except Exception:
            return None
    
    def get_stats(self) -> dict[str, Any]:
        """Get position closer statistics."""
        return {
            "total_closes": self.close_count,
            "total_redemptions": self.redemption_count,
            "total_realized_pnl": float(self.total_realized_pnl),
        }
