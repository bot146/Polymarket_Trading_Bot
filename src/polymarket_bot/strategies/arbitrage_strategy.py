"""Enhanced arbitrage strategy using the strategy framework.

This strategy looks for YES+NO hedge arbitrage opportunities where
the combined ask price is less than $1, locking in guaranteed profit.
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


class ArbitrageStrategy(Strategy):
    """YES+NO hedge arbitrage strategy.
    
    When YES_ask + NO_ask < $1, we can buy both and lock in profit.
    """

    def __init__(
        self,
        name: str = "arbitrage",
        min_edge_cents: Decimal = Decimal("0.5"),
        edge_buffer_cents: Decimal = Decimal("0"),
        max_order_usdc: Decimal = Decimal("20"),
        strict: bool = False,
        require_top_of_book: bool = False,
        taker_fee_rate: Decimal = Decimal("0.02"),
        enabled: bool = True,
    ):
        super().__init__(name=name, enabled=enabled)
        self.min_edge_cents = min_edge_cents
        self.edge_buffer_cents = edge_buffer_cents
        self.max_order_usdc = max_order_usdc
        self.strict = strict
        self.require_top_of_book = require_top_of_book
        self.taker_fee_rate = taker_fee_rate

    def scan(self, market_data: dict[str, Any]) -> list[StrategySignal]:
        """Scan for arbitrage opportunities in binary markets.
        
        Args:
            market_data: Dict with structure:
                {
                    "markets": [
                        {
                            "condition_id": str,
                            "tokens": [
                                {"token_id": str, "outcome": "YES", "price": float, "best_ask": float},
                                {"token_id": str, "outcome": "NO", "price": float, "best_ask": float},
                            ]
                        }
                    ]
                }
        """
        signals = []
        markets = market_data.get("markets", [])

        for market in markets:
            tokens = market.get("tokens", [])
            if len(tokens) != 2:
                continue

            # Find YES and NO tokens
            yes_token = None
            no_token = None
            for token in tokens:
                if token.get("outcome", "").upper() == "YES":
                    yes_token = token
                elif token.get("outcome", "").upper() == "NO":
                    no_token = token

            if not yes_token or not no_token:
                continue

            # Get best ask prices.
            # In strict mode we prefer top-of-book prices and can optionally
            # require them to be present (avoid Gamma fallback).
            yes_best_ask = yes_token.get("best_ask")
            no_best_ask = no_token.get("best_ask")

            yes_ask = yes_best_ask or yes_token.get("price")
            no_ask = no_best_ask or no_token.get("price")

            if self.strict and self.require_top_of_book:
                if yes_best_ask is None or no_best_ask is None:
                    # Without executable prices, strict arb shouldn't fire.
                    continue
            
            if yes_ask is None or no_ask is None:
                continue

            yes_ask = Decimal(str(yes_ask))
            no_ask = Decimal(str(no_ask))

            # Calculate edge (after fees)
            total_cost = yes_ask + no_ask
            # Polymarket fee formula: fee_rate * min(price, 1-price) per leg
            yes_fee = self.taker_fee_rate * min(yes_ask, Decimal("1") - yes_ask)
            no_fee = self.taker_fee_rate * min(no_ask, Decimal("1") - no_ask)
            total_fees = yes_fee + no_fee
            edge = Decimal("1") - total_cost - total_fees
            edge_cents = edge * Decimal("100")

            min_edge_cents = self.min_edge_cents + (self.edge_buffer_cents if self.strict else Decimal("0"))
            if edge_cents >= min_edge_cents:
                # Calculate position size
                size = self._calculate_size(yes_ask, no_ask)
                
                if size > 0:
                    opportunity = Opportunity(
                        strategy_type=StrategyType.ARBITRAGE,
                        expected_profit=edge * size,
                        confidence=Decimal("0.95"),  # High confidence for arbitrage
                        urgency=5,  # Medium-high urgency
                        metadata={
                            "condition_id": market.get("condition_id"),
                            "yes_token_id": yes_token.get("token_id"),
                            "no_token_id": no_token.get("token_id"),
                            "yes_ask": float(yes_ask),
                            "no_ask": float(no_ask),
                            "edge_cents": float(edge_cents),
                            "edge_cents_before_fees": float((Decimal("1") - total_cost) * 100),
                            "total_fees_pct": float(self.taker_fee_rate * 2 * 100),
                            "min_edge_cents": float(min_edge_cents),
                            "strict": bool(self.strict),
                        },
                    )

                    trades = [
                        Trade(
                            token_id=yes_token.get("token_id"),
                            side="BUY",
                            size=size,
                            price=yes_ask,
                            order_type="FOK",
                        ),
                        Trade(
                            token_id=no_token.get("token_id"),
                            side="BUY",
                            size=size,
                            price=no_ask,
                            order_type="FOK",
                        ),
                    ]

                    signal = StrategySignal(
                        opportunity=opportunity,
                        trades=trades,
                        max_total_cost=total_cost * size,
                        min_expected_return=size,  # Guaranteed $1 per share pair
                    )

                    signals.append(signal)
                    log.info(
                        f"Arbitrage opportunity: edge={edge_cents:.2f}¢ size={size} "
                        f"condition={market.get('condition_id')[:8]}..."
                    )

        return signals

    def validate(self, signal: StrategySignal) -> tuple[bool, str]:
        """Validate arbitrage signal before execution."""
        if signal.opportunity.strategy_type != StrategyType.ARBITRAGE:
            return False, "not_arbitrage_strategy"

        if len(signal.trades) != 2:
            return False, "invalid_trade_count"

        # Check that we're buying both YES and NO
        if not all(t.side == "BUY" for t in signal.trades):
            return False, "not_buying_both_sides"

        # Verify sizes match
        if signal.trades[0].size != signal.trades[1].size:
            return False, "size_mismatch"

        # Verify edge is still positive (including fees)
        total_cost = signal.trades[0].price + signal.trades[1].price
        p0 = signal.trades[0].price
        p1 = signal.trades[1].price
        total_fees = (self.taker_fee_rate * min(p0, Decimal("1") - p0)
                      + self.taker_fee_rate * min(p1, Decimal("1") - p1))
        if total_cost + total_fees >= Decimal("1"):
            return False, "edge_disappeared"

        return True, "ok"

    def _calculate_size(self, yes_ask: Decimal, no_ask: Decimal) -> Decimal:
        """Calculate position size based on max order size (including fees)."""
        # We need to buy both, so total cost is (yes_ask + no_ask) * (1 + fee) * size
        # Limit total cost to max_order_usdc
        total_ask = yes_ask + no_ask
        if total_ask <= 0:
            return Decimal("0")
        
        cost_per_pair = total_ask * (Decimal("1") + self.taker_fee_rate)
        max_size = self.max_order_usdc / cost_per_pair
        # Round down to 2 decimal places
        return max_size.quantize(Decimal("0.01"))
