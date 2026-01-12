"""Guaranteed win strategy for resolved markets.

This strategy detects when a market has been resolved (e.g., a football game
completed) but winning shares are still trading below $1. This presents a
risk-free profit opportunity.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from polymarket_bot.strategy import (
    Opportunity,
    Strategy,
    StrategySignal,
    StrategyType,
    Trade,
)

log = logging.getLogger(__name__)


class GuaranteedWinStrategy(Strategy):
    """Strategy for buying winning shares below $1 in resolved markets.
    
    When a market is resolved, the winning outcome pays $1 per share.
    If shares are trading below $1, we can buy them for instant profit.
    """

    def __init__(
        self,
        name: str = "guaranteed_win",
        min_discount_cents: Decimal = Decimal("5.0"),  # Min discount to consider
        max_price: Decimal = Decimal("0.95"),  # Don't buy above 95 cents
        max_order_usdc: Decimal = Decimal("50"),  # Willing to deploy more capital here
        enabled: bool = True,
    ):
        super().__init__(name=name, enabled=enabled)
        self.min_discount_cents = min_discount_cents
        self.max_price = max_price
        self.max_order_usdc = max_order_usdc

    def scan(self, market_data: dict[str, Any]) -> list[StrategySignal]:
        """Scan for resolved markets with mispriced winning shares.
        
        Args:
            market_data: Dict with structure:
                {
                    "resolved_markets": [
                        {
                            "condition_id": str,
                            "resolved": bool,
                            "winning_outcome": str,  # "YES" or "NO"
                            "tokens": [
                                {"token_id": str, "outcome": "YES", "price": float, "best_ask": float},
                                {"token_id": str, "outcome": "NO", "price": float, "best_ask": float},
                            ]
                        }
                    ]
                }
        """
        signals = []
        resolved_markets = market_data.get("resolved_markets", [])

        for market in resolved_markets:
            if not market.get("resolved"):
                continue

            winning_outcome = market.get("winning_outcome")
            if not winning_outcome:
                continue

            tokens = market.get("tokens", [])
            winning_token = None

            # Find the winning token
            for token in tokens:
                if token.get("outcome", "").upper() == winning_outcome.upper():
                    winning_token = token
                    break

            if not winning_token:
                continue

            # Get current ask price for winning shares
            ask_price = winning_token.get("best_ask") or winning_token.get("price")
            if ask_price is None:
                continue

            ask_price = Decimal(str(ask_price))

            # Calculate discount from $1
            discount = Decimal("1") - ask_price
            discount_cents = discount * Decimal("100")

            # Only trade if discount is significant and price reasonable
            if discount_cents >= self.min_discount_cents and ask_price <= self.max_price:
                # Calculate position size
                size = self._calculate_size(ask_price)
                
                if size > 0:
                    opportunity = Opportunity(
                        strategy_type=StrategyType.GUARANTEED_WIN,
                        expected_profit=discount * size,
                        confidence=Decimal("0.99"),  # Very high confidence - market resolved
                        urgency=10,  # CRITICAL - these opportunities disappear fast
                        metadata={
                            "condition_id": market.get("condition_id"),
                            "winning_token_id": winning_token.get("token_id"),
                            "winning_outcome": winning_outcome,
                            "ask_price": float(ask_price),
                            "discount_cents": float(discount_cents),
                            "question": market.get("question", ""),
                        },
                    )

                    trades = [
                        Trade(
                            token_id=winning_token.get("token_id"),
                            side="BUY",
                            size=size,
                            price=ask_price,
                            order_type="IOC",  # Immediate or cancel - speed is critical
                        ),
                    ]

                    signal = StrategySignal(
                        opportunity=opportunity,
                        trades=trades,
                        max_total_cost=ask_price * size,
                        min_expected_return=size,  # Guaranteed $1 per share
                    )

                    signals.append(signal)
                    log.warning(
                        f"🎯 GUARANTEED WIN: {discount_cents:.2f}¢ discount, "
                        f"size={size}, price={ask_price:.4f}, "
                        f"outcome={winning_outcome}, "
                        f"condition={market.get('condition_id')[:8]}..."
                    )

        return signals

    def validate(self, signal: StrategySignal) -> tuple[bool, str]:
        """Validate guaranteed win signal before execution."""
        if signal.opportunity.strategy_type != StrategyType.GUARANTEED_WIN:
            return False, "not_guaranteed_win_strategy"

        if len(signal.trades) != 1:
            return False, "invalid_trade_count"

        trade = signal.trades[0]
        
        # Must be a buy
        if trade.side != "BUY":
            return False, "must_be_buy_order"

        # Price must be below $1
        if trade.price >= Decimal("1"):
            return False, "price_too_high"

        # Price must be reasonable (not above our max)
        if trade.price > self.max_price:
            return False, "price_above_max"

        return True, "ok"

    def _calculate_size(self, ask_price: Decimal) -> Decimal:
        """Calculate position size based on max order size.
        
        For guaranteed wins, we're willing to deploy more capital
        since the risk is minimal.
        """
        if ask_price <= 0:
            return Decimal("0")
        
        max_size = self.max_order_usdc / ask_price
        # Round down to 2 decimal places
        return max_size.quantize(Decimal("0.01"))
