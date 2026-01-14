from __future__ import annotations

from decimal import Decimal

from polymarket_bot.position_manager import PositionManager


def test_portfolio_stats_includes_breakdowns(tmp_path):
    pm = PositionManager(storage_path=str(tmp_path / "positions.json"))

    pm.open_position(
        condition_id="c1",
        token_id="t1",
        outcome="YES",
        strategy="market_making",
        entry_price=Decimal("0.4"),
        quantity=Decimal("10"),
        entry_order_id="o1",
        metadata={},
    )

    pm.open_position(
        condition_id="c2",
        token_id="t2",
        outcome="NO",
        strategy="sniping",
        entry_price=Decimal("0.6"),
        quantity=Decimal("5"),
        entry_order_id="o2",
        metadata={},
    )

    stats = pm.get_portfolio_stats()

    assert "cost_by_condition" in stats
    assert stats["cost_by_condition"]["c1"] > 0
    assert stats["cost_by_condition"]["c2"] > 0

    assert "by_strategy" in stats
    assert "cost" in stats["by_strategy"]
    assert stats["by_strategy"]["cost"]["market_making"] > 0
    assert stats["by_strategy"]["cost"]["sniping"] > 0
