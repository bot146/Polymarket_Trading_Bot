from decimal import Decimal

from polymarket_bot.strategies.sniping_strategy import SnipingConfig, SnipingStrategy


def test_sniping_strategy_emits_signal_on_wide_spread() -> None:
    strat = SnipingStrategy(
        config=SnipingConfig(
            min_spread_bps=Decimal("50"),
            min_discount_to_mid_bps=Decimal("10"),
            max_order_usdc=Decimal("5"),
        )
    )

    market_data = {
        "markets": [
            {
                "condition_id": "c1",
                "question": "Will thing happen?",
                "tokens": [
                    {
                        "token_id": "t1",
                        "outcome": "YES",
                        "best_bid": 0.40,
                        "best_ask": 0.60,
                        "price": 0.50,
                        "volume": 1000.0,
                    }
                ],
            }
        ]
    }

    signals = strat.scan(market_data)
    assert len(signals) == 1

    sig = signals[0]
    assert sig.trades[0].token_id == "t1"
    assert sig.trades[0].side == "BUY"
    assert sig.trades[0].price == Decimal("0.40")

    ok, reason = strat.validate(sig)
    assert ok, reason


def test_sniping_strategy_requires_best_bid_and_best_ask() -> None:
    strat = SnipingStrategy()

    market_data = {
        "markets": [
            {
                "condition_id": "c1",
                "question": "Will thing happen?",
                "tokens": [
                    {"token_id": "t1", "outcome": "YES", "best_bid": None, "best_ask": 0.6, "price": 0.5},
                    {"token_id": "t2", "outcome": "NO", "best_bid": 0.4, "best_ask": None, "price": 0.5},
                ],
            }
        ]
    }

    signals = strat.scan(market_data)
    assert signals == []
