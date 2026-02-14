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
            # Same applies to conditional arb (partial bracket sets).
            #
            # However — as a capital-recycling safety valve — if the entire
            # group has been held longer than max_position_age_hours, force-
            # close all brackets in the group at their current mid price.
            if position.strategy in ("multi_outcome_arb", "conditional_arb"):
                continue

            if self._should_close_position(position, price_data):
                result = self.close_position(position, price_data)
                results.append(result)

        # Arb group age-based exit: close entire groups that exceeded max age.
        if self.max_position_age_seconds > 0:
            arb_results = self._check_arb_group_age_exit(open_positions, price_data)
            results.extend(arb_results)
        
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
            # Live mode: do NOT book synthetic redemptions in the local ledger.
            # Settlement/redemption must be confirmed externally; otherwise we'd
            # create phantom realized P&L and drift from wallet truth.
            log.warning(
                "⚠️  LIVE REDEMPTION PENDING [%s]: pos=%s qty=%s — external settlement required",
                position.strategy,
                position.position_id,
                position.quantity,
            )
            return CloseResult(
                success=False,
                position_id=position.position_id,
                reason="live_redemption_pending_external_settlement",
                error="No live redemption endpoint wired; position left redeemable",
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
            response = self.client.post_order(signed_order, orderType="FOK")  # type: ignore[arg-type]
            
            # Extract order ID
            order_id = self._extract_order_id(response)
            if not order_id:
                return CloseResult(
                    success=False,
                    position_id=position.position_id,
                    reason="live_close_missing_order_id",
                    error="Order posted but no order ID returned",
                )

            filled_size, filled_price, status = self._verify_live_close_fill(
                order_id=order_id,
                post_response=response,
                requested_size=position.quantity,
                requested_price=exit_price,
            )

            if filled_size < position.quantity:
                return CloseResult(
                    success=False,
                    position_id=position.position_id,
                    reason="live_close_not_fully_filled",
                    order_id=order_id,
                    error=(
                        f"status={status} filled={filled_size} requested={position.quantity}; "
                        "position left open"
                    ),
                )
            
            # Close position
            pnl = self.position_manager.close_position(
                position.position_id,
                exit_price=filled_price,
                exit_order_id=order_id,
            )
            
            self.close_count += 1
            self.total_realized_pnl += pnl
            
            log.warning(
                f"✅ LIVE CLOSE [{position.strategy}]: "
                f"pos={position.position_id} P&L=${pnl:.4f} order={order_id} status={status}"
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

    def _verify_live_close_fill(
        self,
        *,
        order_id: str,
        post_response: object,
        requested_size: Decimal,
        requested_price: Decimal,
    ) -> tuple[Decimal, Decimal, str]:
        """Verify full close fill using post response and brief get_order polling."""
        import time as _time

        filled_size, filled_price, status = self._extract_fill_details(
            payload=post_response,
            requested_size=requested_size,
            requested_price=requested_price,
        )
        terminal = {
            "filled",
            "matched",
            "partially_filled",
            "partial",
            "canceled",
            "cancelled",
            "rejected",
            "expired",
            "failed",
        }
        if filled_size > Decimal("0") or status in terminal:
            return filled_size, filled_price, status

        for _ in range(3):
            _time.sleep(0.2)
            try:
                payload = self.client.get_order(order_id) if self.client else None
            except Exception:
                continue
            filled_size, filled_price, status = self._extract_fill_details(
                payload=payload,
                requested_size=requested_size,
                requested_price=requested_price,
            )
            if filled_size > Decimal("0") or status in terminal:
                return filled_size, filled_price, status

        return filled_size, filled_price, status

    def _extract_fill_details(
        self,
        *,
        payload: object,
        requested_size: Decimal,
        requested_price: Decimal,
    ) -> tuple[Decimal, Decimal, str]:
        """Extract (filled_size, fill_price, status) from variable payload shapes."""
        data: Any = payload
        if hasattr(payload, "model_dump"):
            try:
                data = payload.model_dump()
            except Exception:
                data = payload
        elif hasattr(payload, "__dict__"):
            try:
                data = dict(getattr(payload, "__dict__"))
            except Exception:
                data = payload

        if isinstance(data, dict):
            if isinstance(data.get("order"), dict):
                data = data["order"]
            elif isinstance(data.get("data"), dict):
                data = data["data"]

        status = "unknown"
        if isinstance(data, dict):
            s = data.get("status") or data.get("state") or data.get("order_status")
            if s is not None:
                status = str(s).strip().lower()

        def _pick_decimal(obj: object, keys: set[str]) -> Decimal | None:
            keyset = {k.lower() for k in keys}

            def _to_decimal(v: object) -> Decimal | None:
                if v is None:
                    return None
                try:
                    return Decimal(str(v))
                except Exception:
                    return None

            def _walk(node: object) -> Decimal | None:
                if isinstance(node, dict):
                    for k, v in node.items():
                        if str(k).lower() in keyset:
                            d = _to_decimal(v)
                            if d is not None:
                                return d
                    for v in node.values():
                        d = _walk(v)
                        if d is not None:
                            return d
                elif isinstance(node, list):
                    for item in node:
                        d = _walk(item)
                        if d is not None:
                            return d
                return None

            return _walk(obj)

        size = _pick_decimal(
            data,
            {
                "size_matched",
                "matched_size",
                "filled_size",
                "filled",
                "executed_size",
                "filledamount",
                "sizefilled",
                "totalsizefilled",
            },
        )
        price = _pick_decimal(
            data,
            {
                "avg_price",
                "average_price",
                "filled_price",
                "execution_price",
                "match_price",
                "price",
            },
        )

        if size is None:
            size = requested_size if status in {"filled", "matched", "executed"} else Decimal("0")
        if price is None or price <= 0:
            price = requested_price

        return size, price, status
    
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

    # ------------------------------------------------------------------
    # Arb group age-based exit
    # ------------------------------------------------------------------

    def _check_arb_group_age_exit(
        self,
        open_positions: list[Position],
        price_data: dict[str, Decimal],
    ) -> list[CloseResult]:
        """Force-close entire arb groups that exceeded max_position_age.

        Without this, arb positions (which skip normal exit rules) could lock up
        capital forever if the resolution monitor misses a market.

        The exit uses the current mid-price for each bracket so P&L reflects
        actual market conditions.
        """
        import time as _time

        results: list[CloseResult] = []
        now = _time.time()

        arb_positions = [
            p for p in open_positions
            if p.strategy in ("multi_outcome_arb", "conditional_arb")
        ]
        if not arb_positions:
            return results

        # Group by condition_id.
        groups: dict[str, list[Position]] = {}
        for p in arb_positions:
            groups.setdefault(p.condition_id, []).append(p)

        for cid, group_positions in groups.items():
            oldest_age = max(now - p.entry_time for p in group_positions)
            if oldest_age <= self.max_position_age_seconds:
                continue

            log.warning(
                "⏰ ARB GROUP AGE EXIT: %s (%d legs, oldest=%.1fh > max=%.1fh) — force-closing",
                cid[:12],
                len(group_positions),
                oldest_age / 3600,
                self.max_position_age_seconds / 3600,
            )

            for p in group_positions:
                exit_price = price_data.get(p.token_id, p.entry_price)
                result = self.close_position(p, price_data)
                results.append(result)

        return results
