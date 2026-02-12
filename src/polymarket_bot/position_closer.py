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
        
        # Exit rule parameters from settings
        self.profit_target_pct = settings.profit_target_pct / Decimal("100")
        self.stop_loss_pct = settings.stop_loss_pct / Decimal("100")
        self.max_position_age_seconds = settings.max_position_age_hours * 3600.0
        
        # Statistics
        self.close_count = 0
        self.redemption_count = 0
        self.profit_target_closes = 0
        self.stop_loss_closes = 0
        self.time_based_closes = 0
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
            # Multi-outcome arb positions must be held as a complete group.
            # Individual bracket exits break the arb — they should only exit
            # when the market resolves (one bracket pays $1, rest pay $0).
            if position.strategy == "multi_outcome_arb":
                continue

            if self._should_close_position(position, price_data):
                result = self.close_position(position, price_data)
                results.append(result)
        
        return results
    
    def _should_close_position(self, position: Position, price_data: dict[str, Decimal]) -> bool:
        """Determine if a position should be closed.

        Exit rules (checked in order):
        1. Stop loss: close if unrealized P&L drops below -stop_loss_pct
        2. Profit target: close if unrealized P&L exceeds +profit_target_pct
        3. Time-based: close if position age exceeds max_position_age
        """
        import time as _time

        # Need price data for this token
        current_price = price_data.get(position.token_id)
        if current_price is None:
            # Can't evaluate without price — check time-based exit only
            if self.max_position_age_seconds > 0:
                age = _time.time() - position.entry_time
                if age > self.max_position_age_seconds:
                    log.info(
                        "⏰ TIME EXIT: %s aged %.1fh (max %.1fh) — closing",
                        position.position_id,
                        age / 3600,
                        self.max_position_age_seconds / 3600,
                    )
                    self.time_based_closes += 1
                    return True
            return False

        # Update unrealized P&L for this position
        position.update_unrealized_pnl(current_price)

        # Calculate return on cost basis
        if position.cost_basis <= 0:
            return False

        return_pct = position.unrealized_pnl / position.cost_basis

        # 1. Stop loss
        if self.stop_loss_pct > 0 and return_pct <= -self.stop_loss_pct:
            log.warning(
                "🛑 STOP LOSS: %s return=%.2f%% (limit=-%.2f%%) — closing",
                position.position_id,
                float(return_pct * 100),
                float(self.stop_loss_pct * 100),
            )
            self.stop_loss_closes += 1
            return True

        # 2. Profit target
        if self.profit_target_pct > 0 and return_pct >= self.profit_target_pct:
            log.info(
                "🎯 PROFIT TARGET: %s return=%.2f%% (target=%.2f%%) — closing",
                position.position_id,
                float(return_pct * 100),
                float(self.profit_target_pct * 100),
            )
            self.profit_target_closes += 1
            return True

        # 3. Time-based exit
        if self.max_position_age_seconds > 0:
            age = _time.time() - position.entry_time
            if age > self.max_position_age_seconds:
                log.info(
                    "⏰ TIME EXIT: %s aged %.1fh (max %.1fh) return=%.2f%% — closing",
                    position.position_id,
                    age / 3600,
                    self.max_position_age_seconds / 3600,
                    float(return_pct * 100),
                )
                self.time_based_closes += 1
                return True

        return False
    
    def close_position(self, position: Position, price_data: dict[str, Decimal]) -> CloseResult:
        """Close a position by selling.
        
        Args:
            position: Position to close
            price_data: Current market prices
            
        Returns:
            CloseResult with outcome
        """
        current_price = price_data.get(position.token_id)
        if current_price is None:
            # For time-based exits without price data, use entry price as fallback
            current_price = position.entry_price
        
        # Execute sell (regardless of profitability — exit rules already decided)
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
            "profit_target_closes": self.profit_target_closes,
            "stop_loss_closes": self.stop_loss_closes,
            "time_based_closes": self.time_based_closes,
            "total_realized_pnl": float(self.total_realized_pnl),
        }
