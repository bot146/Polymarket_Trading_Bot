from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from polymarket_bot.config import Settings
from polymarket_bot.position_closer import PositionCloser
from polymarket_bot.position_manager import PositionManager
from polymarket_bot.strategy import Opportunity, StrategySignal, StrategyType, Trade
from polymarket_bot.unified_executor import UnifiedExecutor


class _DummyStrategy:
    def validate(self, signal: StrategySignal):
        return True, "ok"


class _DummyLiveClient:
    def __init__(self, *, post_response: dict, order_response: dict):
        self._post_response = post_response
        self._order_response = order_response

    def create_order(self, order_args):
        return order_args

    def post_order(self, signed_order, orderType="GTC"):
        return self._post_response

    def get_order(self, order_id):
        return self._order_response


class _DummyScanner:
    pass


def _make_live_signal(order_type: str = "FOK") -> StrategySignal:
    return StrategySignal(
        opportunity=Opportunity(
            strategy_type=StrategyType.CONDITIONAL_ARB,
            expected_profit=Decimal("0.20"),
            confidence=Decimal("0.8"),
            urgency=5,
            metadata={"condition_id": "c1", "outcome": "YES"},
        ),
        trades=[
            Trade(
                token_id="tok_yes",
                side="BUY",
                price=Decimal("0.50"),
                size=Decimal("2"),
                order_type=order_type,
            )
        ],
        max_total_cost=Decimal("1.00"),
        min_expected_return=Decimal("0.01"),
    )


def test_live_trade_books_actual_fill_price_and_size(tmp_path) -> None:
    pm = PositionManager(storage_path=str(tmp_path / "positions.json"))
    client = _DummyLiveClient(
        post_response={"orderID": "ord_1"},
        order_response={"status": "filled", "size_matched": "1.25", "avg_price": "0.47"},
    )
    settings = Settings(trading_mode="live", kill_switch=False, verify_book_depth=False)
    ex = UnifiedExecutor(client=cast(Any, client), settings=settings, position_manager=pm)

    res = ex.execute_signal(_make_live_signal("FOK"), cast(Any, _DummyStrategy()))

    assert res.success is False
    assert res.reason == "live_partial_fill"
    open_positions = pm.get_open_positions()
    assert len(open_positions) == 1
    assert open_positions[0].quantity == Decimal("1.25")
    assert open_positions[0].entry_price == Decimal("0.47")


def test_live_trade_rejects_zero_fill(tmp_path) -> None:
    pm = PositionManager(storage_path=str(tmp_path / "positions.json"))
    client = _DummyLiveClient(
        post_response={"orderID": "ord_2"},
        order_response={"status": "canceled", "size_matched": "0"},
    )
    settings = Settings(trading_mode="live", kill_switch=False, verify_book_depth=False)
    ex = UnifiedExecutor(client=cast(Any, client), settings=settings, position_manager=pm)

    res = ex.execute_signal(_make_live_signal("IOC"), cast(Any, _DummyStrategy()))

    assert res.success is False
    assert res.reason == "live_not_filled"
    assert len(pm.get_open_positions()) == 0


def test_circuit_breaker_uses_realized_pnl_path() -> None:
    settings = Settings(trading_mode="paper", kill_switch=False)
    ex = UnifiedExecutor(client=None, settings=settings, position_manager=None)

    ex.record_realized_trade_pnl(Decimal("-2.5"))
    stats = ex.get_stats()["circuit_breaker"]

    assert stats["daily_pnl"] == -2.5


def test_live_redemption_is_not_booked_without_settlement(tmp_path) -> None:
    pm = PositionManager(storage_path=str(tmp_path / "positions.json"))
    pos = pm.open_position(
        condition_id="c1",
        token_id="tok_yes",
        outcome="YES",
        strategy="conditional_arb",
        entry_price=Decimal("0.42"),
        quantity=Decimal("2"),
    )
    pm.mark_redeemable(pos.position_id)

    closer = PositionCloser(
        client=cast(Any, _DummyLiveClient(post_response={"orderID": "ord_3"}, order_response={"status": "filled"})),
        settings=Settings(trading_mode="live", kill_switch=False),
        position_manager=pm,
        resolution_monitor=_DummyScanner(),  # type: ignore[arg-type]
    )

    result = closer.redeem_position(pm.get_position(pos.position_id))  # type: ignore[arg-type]

    assert result.success is False
    assert result.reason == "live_redemption_pending_external_settlement"
    current = pm.get_position(pos.position_id)
    assert current is not None
    assert current.is_redeemable
