"""Strategies package for Polymarket trading bot."""

from polymarket_bot.strategies.arbitrage_strategy import ArbitrageStrategy
from polymarket_bot.strategies.copy_trading_strategy import CopyTradingStrategy
from polymarket_bot.strategies.guaranteed_win_strategy import GuaranteedWinStrategy
from polymarket_bot.strategies.market_making_strategy import MarketMakingStrategy
from polymarket_bot.strategies.oracle_sniping_strategy import OracleSnipingStrategy
from polymarket_bot.strategies.sniping_strategy import SnipingStrategy
from polymarket_bot.strategies.statistical_arbitrage_strategy import StatisticalArbitrageStrategy

__all__ = [
	"ArbitrageStrategy",
	"CopyTradingStrategy",
	"GuaranteedWinStrategy",
	"MarketMakingStrategy",
	"OracleSnipingStrategy",
	"SnipingStrategy",
	"StatisticalArbitrageStrategy",
]
