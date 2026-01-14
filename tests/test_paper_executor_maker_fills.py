from decimal import Decimal

from polymarket_bot.config import Settings
from polymarket_bot.position_manager import PositionManager
from polymarket_bot.strategy import Opportunity, StrategySignal, StrategyType, Trade
from polymarket_bot.unified_executor import UnifiedExecutor


def test_paper_executor_gtc_does_not_open_position_until_fill(tmp_path) -> None:
    pm = PositionManager(storage_path=str(tmp_path / "pos.json"))
    ex = UnifiedExecutor(client=None, settings=Settings(kill_switch=False), position_manager=pm)

    signal = StrategySignal(
        opportunity=Opportunity(
            strategy_type=StrategyType.MARKET_MAKING,
            expected_profit=Decimal("0.10"),
            confidence=Decimal("0.5"),
            urgency=1,
            metadata={"condition_id": "c1", "yes_token_id": "t_yes", "no_token_id": "t_no"},
        ),
        trades=[
            Trade(token_id="t_yes", side="BUY", size=Decimal("5"), price=Decimal("0.40"), order_type="GTC"),
        ],
        max_total_cost=Decimal("2.00"),
        min_expected_return=Decimal("0.01"),
    )

    res = ex._paper_trade(signal)
    assert res.success
    assert len(pm.positions) == 0

    # Market doesn't cross -> still no fill.
    fills = ex.on_market_update(token_id="t_yes", best_bid=Decimal("0.39"), best_ask=Decimal("0.41"))
    assert fills == []
    assert len(pm.positions) == 0

    # Cross -> fill and position opens.
    fills = ex.on_market_update(token_id="t_yes", best_bid=Decimal("0.39"), best_ask=Decimal("0.40"))
    assert len(fills) == 1
    assert len(pm.positions) == 1


def test_paper_executor_ioc_fills_only_when_marketable(tmp_path) -> None:
    pm = PositionManager(storage_path=str(tmp_path / "pos.json"))
    ex = UnifiedExecutor(client=None, settings=Settings(kill_switch=False), position_manager=pm)

    # Establish last top-of-book.
    ex.on_market_update(token_id="t_yes", best_bid=Decimal("0.39"), best_ask=Decimal("0.41"))

    signal = StrategySignal(
        opportunity=Opportunity(
            strategy_type=StrategyType.HIGH_FREQUENCY_SNIPING,
            expected_profit=Decimal("0.01"),
            confidence=Decimal("0.5"),
            urgency=1,
            metadata={"condition_id": "c1", "outcome": "YES"},
        ),
        trades=[
            # Not marketable: best_ask 0.41 > limit 0.40
            Trade(token_id="t_yes", side="BUY", size=Decimal("2"), price=Decimal("0.40"), order_type="IOC"),
        ],
        max_total_cost=Decimal("0.80"),
        min_expected_return=Decimal("0.01"),
    )

    res = ex._paper_trade(signal)
    assert res.success
    assert len(pm.positions) == 0

    # Now make it marketable.
    ex.on_market_update(token_id="t_yes", best_bid=Decimal("0.39"), best_ask=Decimal("0.40"))
    res2 = ex._paper_trade(signal)
    assert res2.success
    assert len(pm.positions) == 1


def test_paper_executor_fok_is_atomic_across_legs(tmp_path) -> None:
    pm = PositionManager(storage_path=str(tmp_path / "pos.json"))
    ex = UnifiedExecutor(client=None, settings=Settings(kill_switch=False), position_manager=pm)

    # YES is marketable, NO is not.
    ex.on_market_update(token_id="t_yes", best_bid=Decimal("0.39"), best_ask=Decimal("0.40"))
    ex.on_market_update(token_id="t_no", best_bid=Decimal("0.59"), best_ask=Decimal("0.61"))

    signal = StrategySignal(
        opportunity=Opportunity(
            strategy_type=StrategyType.ARBITRAGE,
            expected_profit=Decimal("0.10"),
            confidence=Decimal("0.9"),
            urgency=5,
            metadata={"condition_id": "c1", "yes_token_id": "t_yes", "no_token_id": "t_no"},
        ),
        trades=[
            Trade(token_id="t_yes", side="BUY", size=Decimal("1"), price=Decimal("0.40"), order_type="FOK"),
            # Not marketable: best_ask 0.61 > limit 0.60
            Trade(token_id="t_no", side="BUY", size=Decimal("1"), price=Decimal("0.60"), order_type="FOK"),
        ],
        max_total_cost=Decimal("1.00"),
        min_expected_return=Decimal("1"),
    )

    res = ex._paper_trade(signal)
    assert not res.success
    assert res.reason == "paper_fok_not_marketable"
    assert len(pm.positions) == 0

    # Now make NO marketable too.
    ex.on_market_update(token_id="t_no", best_bid=Decimal("0.59"), best_ask=Decimal("0.60"))
    res2 = ex._paper_trade(signal)
    assert res2.success
    assert len(pm.positions) == 2
