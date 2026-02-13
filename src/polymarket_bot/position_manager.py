"""Position management system for tracking holdings and trades.

This module provides a comprehensive position tracking system that maintains
the complete lifecycle of trades from entry to exit, enabling accurate P&L
calculation and portfolio management.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class PositionStatus(str, Enum):
    """Status of a position."""
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    REDEEMABLE = "redeemable"  # Market resolved, can redeem for $1


@dataclass
class Position:
    """Represents a trading position."""
    position_id: str  # Unique identifier
    condition_id: str  # Market condition
    token_id: str
    outcome: str  # YES/NO or other outcome
    strategy: str  # Strategy that opened position
    
    # Entry details
    entry_price: Decimal
    quantity: Decimal
    entry_time: float
    entry_order_id: str | None = None
    
    # Exit details
    exit_price: Decimal | None = None
    exit_time: float | None = None
    exit_order_id: str | None = None
    
    # Status
    status: PositionStatus = PositionStatus.OPEN
    
    # P&L
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    
    # Metadata
    metadata: dict[str, Any] = field(default_factory=dict)
    
    @property
    def cost_basis(self) -> Decimal:
        """Total cost of the position."""
        return self.entry_price * self.quantity
    
    @property
    def is_open(self) -> bool:
        """Check if position is open."""
        return self.status == PositionStatus.OPEN
    
    @property
    def is_closed(self) -> bool:
        """Check if position is closed."""
        return self.status == PositionStatus.CLOSED
    
    @property
    def is_redeemable(self) -> bool:
        """Check if position can be redeemed."""
        return self.status == PositionStatus.REDEEMABLE
    
    def update_unrealized_pnl(self, current_price: Decimal) -> None:
        """Update unrealized P&L based on current price."""
        if self.is_open:
            current_value = current_price * self.quantity
            self.unrealized_pnl = current_value - self.cost_basis
    
    def close(self, exit_price: Decimal, exit_order_id: str | None = None) -> Decimal:
        """Close the position and calculate realized P&L.
        
        Returns:
            Realized profit/loss
        """
        self.exit_price = exit_price
        self.exit_time = time.time()
        self.exit_order_id = exit_order_id
        self.status = PositionStatus.CLOSED
        
        exit_value = exit_price * self.quantity
        self.realized_pnl = exit_value - self.cost_basis
        self.unrealized_pnl = Decimal("0")
        
        return self.realized_pnl
    
    def mark_redeemable(self) -> None:
        """Mark position as redeemable (market resolved, winning side)."""
        self.status = PositionStatus.REDEEMABLE
        # For winning shares, exit price is $1
        self.exit_price = Decimal("1.0")
        # Calculate unrealized P&L as if we sold at $1
        self.unrealized_pnl = (Decimal("1.0") * self.quantity) - self.cost_basis
    
    def to_dict(self) -> dict[str, Any]:
        """Convert position to dictionary for serialization."""
        return {
            "position_id": self.position_id,
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "outcome": self.outcome,
            "strategy": self.strategy,
            "entry_price": str(self.entry_price),
            "quantity": str(self.quantity),
            "entry_time": self.entry_time,
            "entry_order_id": self.entry_order_id,
            "exit_price": str(self.exit_price) if self.exit_price else None,
            "exit_time": self.exit_time,
            "exit_order_id": self.exit_order_id,
            "status": self.status.value,
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Position:
        """Create position from dictionary."""
        return cls(
            position_id=data["position_id"],
            condition_id=data["condition_id"],
            token_id=data["token_id"],
            outcome=data["outcome"],
            strategy=data["strategy"],
            entry_price=Decimal(data["entry_price"]),
            quantity=Decimal(data["quantity"]),
            entry_time=data["entry_time"],
            entry_order_id=data.get("entry_order_id"),
            exit_price=Decimal(data["exit_price"]) if data.get("exit_price") else None,
            exit_time=data.get("exit_time"),
            exit_order_id=data.get("exit_order_id"),
            status=PositionStatus(data["status"]),
            realized_pnl=Decimal(data["realized_pnl"]),
            unrealized_pnl=Decimal(data["unrealized_pnl"]),
            metadata=data.get("metadata", {}),
        )


class PositionManager:
    """Manages all trading positions."""
    
    def __init__(self, storage_path: str | None = None):
        self.storage_path = Path(storage_path) if storage_path else None
        self.positions: dict[str, Position] = {}
        self._next_position_id = 1
        
        # Load positions from storage if available
        if self.storage_path and self.storage_path.exists():
            self._load_positions()
    
    def open_position(
        self,
        condition_id: str,
        token_id: str,
        outcome: str,
        strategy: str,
        entry_price: Decimal,
        quantity: Decimal,
        entry_order_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Position:
        """Open a new position.
        
        Returns:
            The newly created position
        """
        position_id = f"pos_{self._next_position_id}"
        self._next_position_id += 1
        
        position = Position(
            position_id=position_id,
            condition_id=condition_id,
            token_id=token_id,
            outcome=outcome,
            strategy=strategy,
            entry_price=entry_price,
            quantity=quantity,
            entry_time=time.time(),
            entry_order_id=entry_order_id,
            metadata=metadata or {},
        )
        
        self.positions[position_id] = position
        self._save_positions()
        
        log.info(
            f"Opened position {position_id}: {outcome} {quantity} @ ${entry_price} "
            f"(condition={condition_id[:8]}...)"
        )
        
        return position
    
    def close_position(
        self,
        position_id: str,
        exit_price: Decimal,
        exit_order_id: str | None = None,
    ) -> Decimal:
        """Close a position and calculate P&L.
        
        Returns:
            Realized profit/loss
        """
        if position_id not in self.positions:
            raise ValueError(f"Position {position_id} not found")
        
        position = self.positions[position_id]
        pnl = position.close(exit_price, exit_order_id)
        self._save_positions()
        
        log.info(
            f"Closed position {position_id}: "
            f"entry=${position.entry_price:.4f} exit=${exit_price:.4f} "
            f"P&L=${pnl:.4f}"
        )
        
        return pnl
    
    def mark_redeemable(self, position_id: str) -> None:
        """Mark a position as redeemable (market resolved)."""
        if position_id not in self.positions:
            raise ValueError(f"Position {position_id} not found")
        
        position = self.positions[position_id]
        position.mark_redeemable()
        self._save_positions()
        
        log.info(
            f"Position {position_id} marked redeemable: "
            f"unrealized P&L=${position.unrealized_pnl:.4f}"
        )
    
    def get_position(self, position_id: str) -> Position | None:
        """Get a position by ID."""
        return self.positions.get(position_id)
    
    def get_open_positions(self) -> list[Position]:
        """Get all open positions."""
        return [p for p in self.positions.values() if p.is_open]
    
    def get_redeemable_positions(self) -> list[Position]:
        """Get all redeemable positions."""
        return [p for p in self.positions.values() if p.is_redeemable]
    
    def get_positions_by_condition(self, condition_id: str) -> list[Position]:
        """Get all positions for a specific market."""
        return [p for p in self.positions.values() if p.condition_id == condition_id]
    
    def get_positions_by_strategy(self, strategy: str) -> list[Position]:
        """Get all positions from a specific strategy."""
        return [p for p in self.positions.values() if p.strategy == strategy]
    
    def update_unrealized_pnl(self, price_data: dict[str, Decimal]) -> None:
        """Update unrealized P&L for all open positions.
        
        For multi-outcome arb positions, we calculate P&L at the group level
        rather than using individual WSS mid-prices, because:
        - The arb holds ALL brackets simultaneously
        - Exactly one bracket will resolve to $1.00, the rest to $0.00
        - Expected value per share = $1.00 across the whole group
        - Using WSS mid-prices ($0.50 each) would wildly overstate returns
        
        Args:
            price_data: Dict of token_id -> current_price
        """
        open_positions = self.get_open_positions()

        # --- Multi-outcome arb: group-level P&L ---
        # Group arb positions by condition_id (= neg_risk_market_id).
        arb_groups: dict[str, list[Position]] = {}
        non_arb: list[Position] = []

        for position in open_positions:
            if position.strategy == "multi_outcome_arb":
                arb_groups.setdefault(position.condition_id, []).append(position)
            else:
                non_arb.append(position)

        # For each arb group, the expected value is $1.00 × qty per execution
        # (one bracket wins).  If the same group was executed N times (e.g.
        # across restarts before the dedup fix), there are N × B positions
        # (B = unique brackets).  We detect N by comparing total positions
        # to the number of unique token_ids.
        for _group_id, positions in arb_groups.items():
            if not positions:
                continue
            qty = positions[0].quantity  # All brackets have the same size
            unique_tokens = len({p.token_id for p in positions})
            num_executions = max(1, len(positions) // unique_tokens) if unique_tokens else 1
            group_cost = sum(p.entry_price * qty for p in positions)
            group_value = Decimal("1.00") * qty * num_executions  # One winner per execution
            group_pnl = group_value - group_cost
            per_position_pnl = group_pnl / len(positions)
            for p in positions:
                p.unrealized_pnl = per_position_pnl

        # --- All other strategies: standard per-token valuation ---
        for position in non_arb:
            if position.token_id in price_data:
                position.update_unrealized_pnl(price_data[position.token_id])
    
    def get_portfolio_stats(self) -> dict[str, Any]:
        """Get portfolio statistics."""
        open_positions = self.get_open_positions()
        closed_positions = [p for p in self.positions.values() if p.is_closed]
        redeemable_positions = self.get_redeemable_positions()

        # Breakdowns for risk/metrics.
        cost_by_condition: dict[str, float] = {}
        pnl_by_condition: dict[str, float] = {}
        cost_by_strategy: dict[str, float] = {}
        realized_by_strategy: dict[str, float] = {}
        unrealized_by_strategy: dict[str, float] = {}

        for p in open_positions + redeemable_positions:
            cost_by_condition[p.condition_id] = cost_by_condition.get(p.condition_id, 0.0) + float(p.cost_basis)
            pnl_by_condition[p.condition_id] = pnl_by_condition.get(p.condition_id, 0.0) + float(p.unrealized_pnl)

            cost_by_strategy[p.strategy] = cost_by_strategy.get(p.strategy, 0.0) + float(p.cost_basis)
            unrealized_by_strategy[p.strategy] = unrealized_by_strategy.get(p.strategy, 0.0) + float(p.unrealized_pnl)

        for p in closed_positions:
            realized_by_strategy[p.strategy] = realized_by_strategy.get(p.strategy, 0.0) + float(p.realized_pnl)
        
        total_realized_pnl = sum(p.realized_pnl for p in closed_positions)
        total_unrealized_pnl = sum(p.unrealized_pnl for p in open_positions + redeemable_positions)
        total_cost_basis = sum(p.cost_basis for p in open_positions + redeemable_positions)
        
        return {
            "total_positions": len(self.positions),
            "open_positions": len(open_positions),
            "closed_positions": len(closed_positions),
            "redeemable_positions": len(redeemable_positions),
            "total_realized_pnl": float(total_realized_pnl),
            "total_unrealized_pnl": float(total_unrealized_pnl),
            "total_pnl": float(total_realized_pnl + total_unrealized_pnl),
            "total_cost_basis": float(total_cost_basis),
            "realized_roi": float((total_realized_pnl / total_cost_basis * 100) if total_cost_basis > 0 else Decimal("0")),
            "cost_by_condition": cost_by_condition,
            "unrealized_pnl_by_condition": pnl_by_condition,
            "by_strategy": {
                "cost": cost_by_strategy,
                "realized": realized_by_strategy,
                "unrealized": unrealized_by_strategy,
            },
        }
    
    def _save_positions(self) -> None:
        """Save positions to storage."""
        if not self.storage_path:
            return
        
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "positions": [p.to_dict() for p in self.positions.values()],
                "next_position_id": self._next_position_id,
            }
            with open(self.storage_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.error(f"Failed to save positions: {e}")
    
    def _load_positions(self) -> None:
        """Load positions from storage."""
        try:
            with open(self.storage_path, "r") as f:
                data = json.load(f)
            
            self.positions = {
                p["position_id"]: Position.from_dict(p)
                for p in data.get("positions", [])
            }
            self._next_position_id = data.get("next_position_id", 1)
            
            log.info(f"Loaded {len(self.positions)} positions from {self.storage_path}")
        except Exception as e:
            log.error(f"Failed to load positions: {e}")

    def reset_all_positions(self) -> None:
        """Clear all positions and persist an empty portfolio.

        Intended for paper-mode clean restarts.
        """
        self.positions = {}
        self._next_position_id = 1
        self._save_positions()
        log.info("Reset all positions (paper clean start)")
