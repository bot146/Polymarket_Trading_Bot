from decimal import Decimal

from polymarket_bot.strategies.market_making_strategy import MarketMakingConfig, MarketMakingStrategy
from polymarket_bot.strategy import StrategyType


def test_market_making_emits_quotes_on_wide_spread() -> None:
    strat = MarketMakingStrategy(
        config=MarketMakingConfig(
            min_spread_bps=Decimal("10"),
            improve_bps=Decimal("5"),
            max_order_usdc_per_side=Decimal("5"),
        )
    )

    market_data = {
        "markets": [
            {
                "condition_id": "c1",
                "question": "Q",
                "tokens": [
                    {"token_id": "yes", "outcome": "YES", "best_bid": 0.40, "best_ask": 0.60, "price": 0.50, "volume": 1000.0},
                    {"token_id": "no", "outcome": "NO", "best_bid": 0.35, "best_ask": 0.65, "price": 0.50, "volume": 1000.0},
                ],
            }
        ]
    }

    signals = strat.scan(market_data)
    assert len(signals) == 1

    sig = signals[0]
    assert sig.opportunity.strategy_type == StrategyType.MARKET_MAKING
    assert len(sig.trades) == 4

    ok, reason = strat.validate(sig)
    assert ok, reason


def test_market_making_requires_top_of_book() -> None:
    strat = MarketMakingStrategy()

    market_data = {
        "markets": [
            {
                "condition_id": "c1",
                "question": "Q",
                "tokens": [
                    {"token_id": "yes", "outcome": "YES", "best_bid": None, "best_ask": 0.60, "price": 0.50},
                    {"token_id": "no", "outcome": "NO", "best_bid": 0.35, "best_ask": None, "price": 0.50},
                ],
            }
        ]
    }

    signals = strat.scan(market_data)
    assert signals == []
