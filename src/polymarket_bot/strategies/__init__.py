"""Strategies package for Polymarket trading bot."""

from polymarket_bot.strategies.arbitrage_strategy import ArbitrageStrategy
from polymarket_bot.strategies.conditional_arb_strategy import ConditionalArbStrategy
from polymarket_bot.strategies.copy_trading_strategy import CopyTradingStrategy
from polymarket_bot.strategies.guaranteed_win_strategy import GuaranteedWinStrategy
from polymarket_bot.strategies.liquidity_rewards_strategy import LiquidityRewardsStrategy
from polymarket_bot.strategies.market_making_strategy import MarketMakingStrategy
from polymarket_bot.strategies.multi_outcome_arb_strategy import MultiOutcomeArbStrategy
from polymarket_bot.strategies.near_resolution_strategy import NearResolutionStrategy
from polymarket_bot.strategies.oracle_sniping_strategy import OracleSnipingStrategy
from polymarket_bot.strategies.short_duration_strategy import ShortDurationStrategy
from polymarket_bot.strategies.sniping_strategy import SnipingStrategy
from polymarket_bot.strategies.statistical_arbitrage_strategy import StatisticalArbitrageStrategy
from polymarket_bot.strategies.value_betting_strategy import ValueBettingStrategy

__all__ = [
	"ArbitrageStrategy",
	"ConditionalArbStrategy",
	"CopyTradingStrategy",
	"GuaranteedWinStrategy",
	"LiquidityRewardsStrategy",
	"MarketMakingStrategy",
	"MultiOutcomeArbStrategy",
	"NearResolutionStrategy",
	"OracleSnipingStrategy",
	"ShortDurationStrategy",
	"SnipingStrategy",
	"StatisticalArbitrageStrategy",
	"ValueBettingStrategy",
]
