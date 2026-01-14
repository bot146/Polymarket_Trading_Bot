"""Inventory hedger.

Goal: keep the bot from turning maker fills into directional bets.

This is intentionally conservative:
- If a condition has both YES and NO exposure, we consider it "paired".
- If one side is net larger, we hedge the excess by buying the opposite token.

In Polymarket binary markets, holding 1 YES + 1 NO shares is (ignoring fees)
close to a $1 payoff at resolution. Being unpaired creates outcome risk.

We implement an *opportunistic but safe* rule:
- Attempt to hedge only when exposure imbalance exceeds a threshold.
- Hedge using current best ask for BUY (paper/live ordering uses that price).
- Clamp hedge notional by settings.

The hedger emits a list of Trade intents; the executor is responsible for
placing them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from polymarket_bot.position_manager import Position
from polymarket_bot.strategy import Trade


@dataclass(frozen=True)
class HedgeDecision:
    condition_id: str
    reason: str
    trades: list[Trade]


class InventoryHedger:
    def __init__(
        self,
        *,
        # Minimum imbalance (shares) before we hedge.
        min_imbalance_shares: Decimal = Decimal("1"),
        # Max hedge notional per decision.
        max_hedge_usdc: Decimal = Decimal("10"),
    ) -> None:
        self.min_imbalance_shares = min_imbalance_shares
        self.max_hedge_usdc = max_hedge_usdc

    def decide(
        self,
        *,
        positions: Iterable[Position],
        yes_token_id: str | None,
        no_token_id: str | None,
        best_ask: dict[str, Decimal],
    ) -> HedgeDecision | None:
        """Return a hedge decision for a condition, or None."""
        if not yes_token_id or not no_token_id:
            return None

        # Net share exposure by token.
        yes_qty = sum((p.quantity for p in positions if p.is_open and p.token_id == yes_token_id), Decimal("0"))
        no_qty = sum((p.quantity for p in positions if p.is_open and p.token_id == no_token_id), Decimal("0"))

        imbalance = yes_qty - no_qty  # + means too much YES
        if imbalance.copy_abs() < self.min_imbalance_shares:
            return None

        # Hedge the excess by buying the opposite side.
        if imbalance > 0:
            # Need more NO
            hedge_token = no_token_id
        else:
            # Need more YES
            hedge_token = yes_token_id

        ask = best_ask.get(hedge_token)
        if ask is None or ask <= 0:
            return None

        # Quantity capped by max usdc.
        max_shares = (self.max_hedge_usdc / ask).quantize(Decimal("0.01"))
        hedge_size = min(imbalance.copy_abs(), max_shares)
        if hedge_size <= 0:
            return None

        trade = Trade(
            token_id=hedge_token,
            side="BUY",
            size=hedge_size,
            price=ask,
            order_type="IOC",
        )

        condition_id = next(iter(positions)).condition_id  # safe: caller groups by condition
        return HedgeDecision(condition_id=condition_id, reason="inventory_imbalance", trades=[trade])
