from __future__ import annotations

from decimal import Decimal

from polymarket_bot.config import Settings
from polymarket_bot.strategy import Opportunity, StrategySignal, StrategyType, Trade
from polymarket_bot.unified_executor import UnifiedExecutor


class _DummyStrategy:
    def validate(self, signal: StrategySignal):
        return True, "ok"


class _DummyPosition:
    def __init__(self, *, cost_basis: Decimal, is_open: bool = True):
        self.cost_basis = cost_basis
        self.is_open = is_open


class _DummyPositionManager:
    def __init__(self, *, cost_by_condition: dict[str, Decimal]):
        self._cost_by_condition = cost_by_condition

    def get_positions_by_condition(self, condition_id: str):
        cost = self._cost_by_condition.get(condition_id, Decimal("0"))
        return [_DummyPosition(cost_basis=cost, is_open=True)]


def _make_signal(*, condition_id: str, order_type: str = "GTC") -> StrategySignal:
    opp = Opportunity(
        strategy_type=StrategyType.MARKET_MAKING,
        expected_profit=Decimal("0.01"),
        confidence=Decimal("0.8"),
        urgency=5,
        metadata={"condition_id": condition_id},
    )
    return StrategySignal(
        opportunity=opp,
        trades=[
            Trade(
                token_id="token_1",
                side="BUY",
                price=Decimal("0.50"),
                size=Decimal("1"),
                order_type=order_type,
            )
        ],
        max_total_cost=Decimal("0.50"),
        min_expected_return=Decimal("0"),
    )


def test_inventory_cap_blocks_when_over_limit():
    settings = Settings(
        trading_mode="paper",
        kill_switch=False,
        max_inventory_usdc_per_condition=Decimal("5"),
        max_open_gtc_orders_per_condition=10,
    )
    pm = _DummyPositionManager(cost_by_condition={"c1": Decimal("5.01")})
    ex = UnifiedExecutor(client=None, settings=settings, position_manager=pm)  # type: ignore[arg-type]

    res = ex.execute_signal(
        _make_signal(condition_id="c1", order_type="IOC"),
        _DummyStrategy(),  # type: ignore[arg-type]
    )
    assert res.success is False
    assert "risk_check_failed_max_inventory_usdc_per_condition" in res.reason


def test_open_gtc_cap_blocks_in_paper_mode():
    settings = Settings(
        trading_mode="paper",
        kill_switch=False,
        max_inventory_usdc_per_condition=Decimal("999"),
        max_open_gtc_orders_per_condition=1,
    )
    ex = UnifiedExecutor(client=None, settings=settings, position_manager=None)

    # Pre-existing open GTC for same condition
    ex.paper_blotter.submit(
        token_id="token_1",
        side="BUY",
        price=Decimal("0.40"),
        size=Decimal("1"),
        order_type="GTC",
        condition_id="c1",
    )

    res = ex.execute_signal(
        _make_signal(condition_id="c1", order_type="GTC"),
        _DummyStrategy(),  # type: ignore[arg-type]
    )
    assert res.success is False
    assert "risk_check_failed_max_open_gtc_orders_per_condition" in res.reason
