from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class HedgeOpportunity:
    yes_token_id: str
    no_token_id: str
    yes_ask: Decimal
    no_ask: Decimal
    total_cost: Decimal
    edge: Decimal  # 1 - total_cost


def cents(x: Decimal) -> Decimal:
    return x * Decimal(100)


def compute_hedge_opportunity(
    *,
    yes_token_id: str,
    no_token_id: str,
    yes_ask: Decimal,
    no_ask: Decimal,
) -> HedgeOpportunity:
    """Compute the simplest hedge: buy 1 YES share and 1 NO share.

    If bought at asks, you lock $1 payout at resolution, so gross edge is:

        edge = 1 - (yes_ask + no_ask)

    Note: this function is deliberately *fee-agnostic*.
    The executor/risk layer will add fee/slippage cushions.
    """

    total = yes_ask + no_ask
    edge = Decimal(1) - total
    return HedgeOpportunity(
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        yes_ask=yes_ask,
        no_ask=no_ask,
        total_cost=total,
        edge=edge,
    )
