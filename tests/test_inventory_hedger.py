from decimal import Decimal

from polymarket_bot.inventory_hedger import InventoryHedger
from polymarket_bot.position_manager import Position, PositionStatus


def _pos(condition_id: str, token_id: str, qty: str) -> Position:
    return Position(
        position_id="p",
        condition_id=condition_id,
        token_id=token_id,
        outcome="YES",
        strategy="test",
        entry_price=Decimal("0.40"),
        quantity=Decimal(qty),
        entry_time=0.0,
        status=PositionStatus.OPEN,
    )


def test_hedger_buys_opposite_when_imbalanced() -> None:
    hedger = InventoryHedger(min_imbalance_shares=Decimal("1"), max_hedge_usdc=Decimal("10"))

    yes = "yes"
    no = "no"

    decision = hedger.decide(
        positions=[_pos("c1", yes, "5"), _pos("c1", no, "1")],
        yes_token_id=yes,
        no_token_id=no,
        best_ask={no: Decimal("0.50"), yes: Decimal("0.50")},
    )

    assert decision is not None
    assert decision.trades[0].token_id == no
    assert decision.trades[0].side == "BUY"
    assert decision.trades[0].price == Decimal("0.50")
