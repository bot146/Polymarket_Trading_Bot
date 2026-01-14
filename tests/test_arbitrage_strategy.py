"""Tests for arbitrage strategy."""

from decimal import Decimal

from polymarket_bot.strategies.arbitrage_strategy import ArbitrageStrategy
from polymarket_bot.strategy import StrategyType


def test_arbitrage_strategy_no_opportunities():
    """Test that no signals are generated when no opportunities exist."""
    strategy = ArbitrageStrategy(min_edge_cents=Decimal("1.5"))
    
    market_data = {
        "markets": [
            {
                "condition_id": "cond1",
                "tokens": [
                    {"token_id": "yes1", "outcome": "YES", "price": 0.51, "best_ask": 0.51},
                    {"token_id": "no1", "outcome": "NO", "price": 0.50, "best_ask": 0.50},
                ],
            }
        ]
    }
    
    signals = strategy.scan(market_data)
    assert len(signals) == 0


def test_arbitrage_strategy_finds_opportunity():
    """Test that arbitrage opportunities are found."""
    strategy = ArbitrageStrategy(
        min_edge_cents=Decimal("1.5"),
        max_order_usdc=Decimal("20")
    )
    
    market_data = {
        "markets": [
            {
                "condition_id": "cond1",
                "tokens": [
                    {"token_id": "yes1", "outcome": "YES", "price": 0.48, "best_ask": 0.48},
                    {"token_id": "no1", "outcome": "NO", "price": 0.49, "best_ask": 0.49},
                ],
            }
        ]
    }
    
    signals = strategy.scan(market_data)
    
    assert len(signals) == 1
    signal = signals[0]
    
    assert signal.opportunity.strategy_type == StrategyType.ARBITRAGE
    assert signal.opportunity.expected_profit > 0
    assert len(signal.trades) == 2
    
    # Both should be buys
    assert all(t.side == "BUY" for t in signal.trades)
    
    # Sizes should match
    assert signal.trades[0].size == signal.trades[1].size


def test_arbitrage_strategy_edge_below_threshold():
    """Test that opportunities below edge threshold are ignored."""
    strategy = ArbitrageStrategy(min_edge_cents=Decimal("2.0"))
    
    market_data = {
        "markets": [
            {
                "condition_id": "cond1",
                "tokens": [
                    # Edge is 1.5 cents (below 2.0 threshold)
                    {"token_id": "yes1", "outcome": "YES", "price": 0.495, "best_ask": 0.495},
                    {"token_id": "no1", "outcome": "NO", "price": 0.490, "best_ask": 0.490},
                ],
            }
        ]
    }
    
    signals = strategy.scan(market_data)
    assert len(signals) == 0


def test_arbitrage_strategy_validation():
    """Test signal validation."""
    strategy = ArbitrageStrategy()
    
    market_data = {
        "markets": [
            {
                "condition_id": "cond1",
                "tokens": [
                    {"token_id": "yes1", "outcome": "YES", "price": 0.48, "best_ask": 0.48},
                    {"token_id": "no1", "outcome": "NO", "price": 0.49, "best_ask": 0.49},
                ],
            }
        ]
    }
    
    signals = strategy.scan(market_data)
    assert len(signals) == 1
    
    valid, reason = strategy.validate(signals[0])
    assert valid
    assert reason == "ok"


def test_arbitrage_strategy_validation_fails_on_disappeared_edge():
    """Test that validation fails if edge disappears."""
    from polymarket_bot.strategy import Opportunity, StrategySignal, StrategyType, Trade
    
    strategy = ArbitrageStrategy()
    
    # Create a signal manually with no edge
    opportunity = Opportunity(
        strategy_type=StrategyType.ARBITRAGE,
        expected_profit=Decimal("0"),
        confidence=Decimal("0.9"),
        urgency=5,
        metadata={"condition_id": "cond1"},
    )
    
    trades = [
        Trade("yes1", "BUY", Decimal("10"), Decimal("0.51"), "FOK"),
        Trade("no1", "BUY", Decimal("10"), Decimal("0.50"), "FOK"),
    ]
    
    signal = StrategySignal(
        opportunity=opportunity,
        trades=trades,
        max_total_cost=Decimal("10.1"),
        min_expected_return=Decimal("10"),
    )
    
    # Should fail validation because total cost >= 1
    valid, reason = strategy.validate(signal)
    assert not valid
    assert reason == "edge_disappeared"


def test_arbitrage_strategy_calculates_size_correctly():
    """Test that position sizing is correct."""
    strategy = ArbitrageStrategy(max_order_usdc=Decimal("100"))
    
    market_data = {
        "markets": [
            {
                "condition_id": "cond1",
                "tokens": [
                    {"token_id": "yes1", "outcome": "YES", "price": 0.30, "best_ask": 0.30},
                    {"token_id": "no1", "outcome": "NO", "price": 0.40, "best_ask": 0.40},
                ],
            }
        ]
    }
    
    signals = strategy.scan(market_data)
    assert len(signals) == 1
    
    signal = signals[0]
    # Total cost is 0.30 + 0.40 = 0.70 per share pair
    # With $100 max, we should buy: 100 / 0.70 = 142.85, rounded down to 142.85
    expected_size = Decimal("100") / Decimal("0.70")
    expected_size = expected_size.quantize(Decimal("0.01"))
    
    assert signal.trades[0].size == expected_size
    assert signal.trades[1].size == expected_size


def test_arbitrage_strategy_strict_applies_edge_buffer():
    """Strict mode should require min_edge + buffer."""
    # Raw edge here is 2.0 cents: 1 - (0.49 + 0.49) = 0.02
    # With min_edge=1.5c and buffer=1.0c => threshold = 2.5c => should NOT trade.
    strategy = ArbitrageStrategy(
        min_edge_cents=Decimal("1.5"),
        edge_buffer_cents=Decimal("1.0"),
        strict=True,
        require_top_of_book=True,
    )

    market_data = {
        "markets": [
            {
                "condition_id": "cond1",
                "tokens": [
                    {"token_id": "yes1", "outcome": "YES", "price": 0.49, "best_ask": 0.49},
                    {"token_id": "no1", "outcome": "NO", "price": 0.49, "best_ask": 0.49},
                ],
            }
        ]
    }

    signals = strategy.scan(market_data)
    assert len(signals) == 0


def test_arbitrage_strategy_strict_requires_top_of_book():
    """If require_top_of_book is enabled, missing best_ask should suppress signals."""
    strategy = ArbitrageStrategy(
        min_edge_cents=Decimal("0.5"),
        edge_buffer_cents=Decimal("0.0"),
        strict=True,
        require_top_of_book=True,
    )

    # Prices imply a strong arb, but we omit best_ask to simulate missing websocket data.
    market_data = {
        "markets": [
            {
                "condition_id": "cond1",
                "tokens": [
                    {"token_id": "yes1", "outcome": "YES", "price": 0.40},
                    {"token_id": "no1", "outcome": "NO", "price": 0.40},
                ],
            }
        ]
    }

    signals = strategy.scan(market_data)
    assert len(signals) == 0
