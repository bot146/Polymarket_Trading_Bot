"""Market resolution monitor for tracking event outcomes.

This module monitors markets for resolution events and identifies
positions that can be closed or redeemed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from polymarket_bot.position_manager import Position, PositionManager
from polymarket_bot.scanner import MarketScanner

log = logging.getLogger(__name__)


@dataclass
class ResolutionEvent:
    """Represents a market resolution event."""
    condition_id: str
    question: str
    winning_outcome: str
    resolved_time: float
    affected_positions: list[str]  # Position IDs


class ResolutionMonitor:
    """Monitors markets for resolution and manages position lifecycle."""
    
    def __init__(
        self,
        position_manager: PositionManager,
        scanner: MarketScanner,
        check_interval: float = 60.0,
    ):
        self.position_manager = position_manager
        self.scanner = scanner
        self.check_interval = check_interval
        
        # Track resolved markets
        self._resolved_markets: dict[str, ResolutionEvent] = {}
        self._last_check = 0.0
    
    def check_resolutions(self) -> list[ResolutionEvent]:
        """Check for newly resolved markets affecting open positions.
        
        Returns:
            List of new resolution events
        """
        now = time.time()
        if now - self._last_check < self.check_interval:
            return []
        
        self._last_check = now
        new_events: list[ResolutionEvent] = []
        
        # Get all open positions
        open_positions = self.position_manager.get_open_positions()
        if not open_positions:
            return []
        
        # Get unique condition IDs
        condition_ids = list(set(p.condition_id for p in open_positions))
        
        log.debug(f"Checking resolution status for {len(condition_ids)} markets")
        
        for condition_id in condition_ids:
            # Skip if already processed
            if condition_id in self._resolved_markets:
                continue

            # Skip negRisk group IDs (e.g. "0xb9aa...") — they are not
            # individual Gamma market IDs and will return 422.  Multi-outcome
            # arb positions use the neg_risk_market_id as condition_id for
            # grouping; resolution must be checked per-bracket separately.
            if condition_id.startswith("0x"):
                continue
            
            # Check market status
            market = self.scanner.get_market(condition_id)
            if not market:
                continue
            
            if market.resolved and market.winning_outcome:
                # Market has resolved!
                affected_positions = [
                    p.position_id
                    for p in self.position_manager.get_positions_by_condition(condition_id)
                    if p.is_open
                ]
                
                event = ResolutionEvent(
                    condition_id=condition_id,
                    question=market.question,
                    winning_outcome=market.winning_outcome,
                    resolved_time=now,
                    affected_positions=affected_positions,
                )
                
                self._resolved_markets[condition_id] = event
                new_events.append(event)
                
                log.warning(
                    f"🎯 Market resolved: {market.question[:50]}... "
                    f"Winner: {market.winning_outcome}, "
                    f"Affects {len(affected_positions)} positions"
                )
                
                # Mark winning positions as redeemable
                self._process_resolution(event, market)
        
        return new_events
    
    def _process_resolution(self, event: ResolutionEvent, market: Any) -> None:
        """Process a resolution event and update positions."""
        positions = self.position_manager.get_positions_by_condition(event.condition_id)
        
        for position in positions:
            if not position.is_open:
                continue
            
            # Check if this position is on the winning side
            if position.outcome.upper() == event.winning_outcome.upper():
                # Winning position - mark as redeemable
                self.position_manager.mark_redeemable(position.position_id)
                log.info(
                    f"✅ Position {position.position_id} is a WINNER! "
                    f"Can redeem {position.quantity} shares @ $1.00"
                )
            else:
                # Losing position - close at $0
                self.position_manager.close_position(
                    position.position_id,
                    exit_price=Decimal("0"),
                )
                log.info(
                    f"❌ Position {position.position_id} lost. "
                    f"P&L: ${position.realized_pnl:.4f}"
                )
    
    def get_resolution_event(self, condition_id: str) -> ResolutionEvent | None:
        """Get resolution event for a market."""
        return self._resolved_markets.get(condition_id)
    
    def is_market_resolved(self, condition_id: str) -> bool:
        """Check if a market has been resolved."""
        return condition_id in self._resolved_markets
    
    def get_redeemable_value(self) -> Decimal:
        """Get total value of redeemable positions."""
        redeemable = self.position_manager.get_redeemable_positions()
        return sum(p.quantity for p in redeemable)  # Each share is worth $1
    
    def get_stats(self) -> dict[str, Any]:
        """Get resolution monitoring statistics."""
        return {
            "resolved_markets": len(self._resolved_markets),
            "redeemable_positions": len(self.position_manager.get_redeemable_positions()),
            "redeemable_value": float(self.get_redeemable_value()),
        }
