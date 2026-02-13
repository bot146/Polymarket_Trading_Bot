from polymarket_bot.strategy import StrategyType
from polymarket_bot.strategies.conditional_arb_strategy import ConditionalArbStrategy
from polymarket_bot.strategies.liquidity_rewards_strategy import LiquidityRewardsStrategy
from polymarket_bot.strategies.near_resolution_strategy import NearResolutionStrategy


def test_new_strategy_types_present():
    assert StrategyType.CONDITIONAL_ARB.value == "conditional_arb"
    assert StrategyType.LIQUIDITY_REWARDS.value == "liquidity_rewards"
    assert StrategyType.NEAR_RESOLUTION.value == "near_resolution"


def test_new_strategies_scan_empty_market_data():
    assert ConditionalArbStrategy().scan({"markets": []}) == []
    assert LiquidityRewardsStrategy().scan({"markets": []}) == []
    assert NearResolutionStrategy().scan({"markets": []}) == []
