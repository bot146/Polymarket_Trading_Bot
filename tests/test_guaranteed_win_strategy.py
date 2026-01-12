"""Tests for guaranteed win strategy."""

from decimal import Decimal

from polymarket_bot.strategies.guaranteed_win_strategy import GuaranteedWinStrategy
from polymarket_bot.strategy import StrategyType


def test_guaranteed_win_no_opportunities():
    """Test that no signals when no resolved markets."""
    strategy = GuaranteedWinStrategy()
    
    market_data = {
        "resolved_markets": []
    }
    
    signals = strategy.scan(market_data)
    assert len(signals) == 0


def test_guaranteed_win_finds_opportunity():
    """Test finding guaranteed win opportunity."""
    strategy = GuaranteedWinStrategy(
        min_discount_cents=Decimal("5.0"),
        max_order_usdc=Decimal("50")
    )
    
    market_data = {
        "resolved_markets": [
            {
                "condition_id": "resolved1",
                "resolved": True,
                "winning_outcome": "YES",
                "question": "Will it rain?",
                "tokens": [
                    {"token_id": "yes1", "outcome": "YES", "price": 0.85, "best_ask": 0.85},
                    {"token_id": "no1", "outcome": "NO", "price": 0.10, "best_ask": 0.10},
                ],
            }
        ]
    }
    
    signals = strategy.scan(market_data)
    
    assert len(signals) == 1
    signal = signals[0]
    
    assert signal.opportunity.strategy_type == StrategyType.GUARANTEED_WIN
    assert signal.opportunity.urgency == 10  # Critical urgency
    assert signal.opportunity.confidence == Decimal("0.99")  # Very high confidence
    
    # Should have exactly one trade (buying winning token)
    assert len(signal.trades) == 1
    assert signal.trades[0].side == "BUY"
    assert signal.trades[0].token_id == "yes1"
    assert signal.trades[0].order_type == "IOC"  # Immediate or cancel


def test_guaranteed_win_ignores_small_discount():
    """Test that small discounts are ignored."""
    strategy = GuaranteedWinStrategy(min_discount_cents=Decimal("10.0"))
    
    market_data = {
        "resolved_markets": [
            {
                "condition_id": "resolved1",
                "resolved": True,
                "winning_outcome": "YES",
                "tokens": [
                    # Only 5 cent discount, below 10 cent threshold
                    {"token_id": "yes1", "outcome": "YES", "price": 0.95, "best_ask": 0.95},
                    {"token_id": "no1", "outcome": "NO", "price": 0.05, "best_ask": 0.05},
                ],
            }
        ]
    }
    
    signals = strategy.scan(market_data)
    assert len(signals) == 0


def test_guaranteed_win_ignores_high_price():
    """Test that prices above max_price are ignored."""
    strategy = GuaranteedWinStrategy(
        min_discount_cents=Decimal("1.0"),
        max_price=Decimal("0.90")
    )
    
    market_data = {
        "resolved_markets": [
            {
                "condition_id": "resolved1",
                "resolved": True,
                "winning_outcome": "YES",
                "tokens": [
                    # Price is 0.93, above 0.90 max
                    {"token_id": "yes1", "outcome": "YES", "price": 0.93, "best_ask": 0.93},
                    {"token_id": "no1", "outcome": "NO", "price": 0.07, "best_ask": 0.07},
                ],
            }
        ]
    }
    
    signals = strategy.scan(market_data)
    assert len(signals) == 0


def test_guaranteed_win_validation():
    """Test signal validation."""
    strategy = GuaranteedWinStrategy()
    
    market_data = {
        "resolved_markets": [
            {
                "condition_id": "resolved1",
                "resolved": True,
                "winning_outcome": "NO",
                "tokens": [
                    {"token_id": "yes1", "outcome": "YES", "price": 0.10, "best_ask": 0.10},
                    {"token_id": "no1", "outcome": "NO", "price": 0.80, "best_ask": 0.80},
                ],
            }
        ]
    }
    
    signals = strategy.scan(market_data)
    assert len(signals) == 1
    
    valid, reason = strategy.validate(signals[0])
    assert valid
    assert reason == "ok"


def test_guaranteed_win_validation_fails_on_high_price():
    """Test that validation fails if price >= $1."""
    from polymarket_bot.strategy import Opportunity, StrategySignal, StrategyType, Trade
    
    strategy = GuaranteedWinStrategy()
    
    # Create a signal manually with price >= $1
    opportunity = Opportunity(
        strategy_type=StrategyType.GUARANTEED_WIN,
        expected_profit=Decimal("0"),
        confidence=Decimal("0.99"),
        urgency=10,
        metadata={"condition_id": "resolved1"},
    )
    
    trades = [
        Trade("yes1", "BUY", Decimal("10"), Decimal("1.0"), "IOC"),
    ]
    
    signal = StrategySignal(
        opportunity=opportunity,
        trades=trades,
        max_total_cost=Decimal("10"),
        min_expected_return=Decimal("10"),
    )
    
    valid, reason = strategy.validate(signal)
    assert not valid
    assert reason == "price_too_high"


def test_guaranteed_win_position_sizing():
    """Test that position sizing is correct."""
    strategy = GuaranteedWinStrategy(max_order_usdc=Decimal("100"))
    
    market_data = {
        "resolved_markets": [
            {
                "condition_id": "resolved1",
                "resolved": True,
                "winning_outcome": "YES",
                "tokens": [
                    {"token_id": "yes1", "outcome": "YES", "price": 0.80, "best_ask": 0.80},
                    {"token_id": "no1", "outcome": "NO", "price": 0.20, "best_ask": 0.20},
                ],
            }
        ]
    }
    
    signals = strategy.scan(market_data)
    assert len(signals) == 1
    
    signal = signals[0]
    # With $100 max and price at $0.80, we should buy 100 / 0.80 = 125 shares
    expected_size = Decimal("100") / Decimal("0.80")
    expected_size = expected_size.quantize(Decimal("0.01"))
    
    assert signal.trades[0].size == expected_size
