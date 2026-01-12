"""Tests for the strategy framework."""

from decimal import Decimal

import pytest

from polymarket_bot.strategy import (
    Opportunity,
    Strategy,
    StrategyRegistry,
    StrategySignal,
    StrategyType,
    Trade,
)


class MockStrategy(Strategy):
    """Mock strategy for testing."""

    def __init__(self, name: str = "mock", enabled: bool = True, fail_scan: bool = False):
        super().__init__(name=name, enabled=enabled)
        self.fail_scan = fail_scan
        self.scan_called = False
        self.validate_called = False

    def scan(self, market_data):
        self.scan_called = True
        if self.fail_scan:
            raise ValueError("Scan failed")
        
        # Return a mock signal
        opportunity = Opportunity(
            strategy_type=StrategyType.ARBITRAGE,
            expected_profit=Decimal("1.0"),
            confidence=Decimal("0.9"),
            urgency=5,
            metadata={"test": "data"},
        )
        
        trades = [
            Trade(
                token_id="token1",
                side="BUY",
                size=Decimal("10"),
                price=Decimal("0.5"),
            ),
        ]
        
        signal = StrategySignal(
            opportunity=opportunity,
            trades=trades,
            max_total_cost=Decimal("5"),
            min_expected_return=Decimal("10"),
        )
        
        return [signal]

    def validate(self, signal):
        self.validate_called = True
        return True, "ok"


def test_strategy_registry_register():
    """Test strategy registration."""
    registry = StrategyRegistry()
    strategy = MockStrategy(name="test1")
    
    registry.register(strategy)
    
    assert registry.get("test1") == strategy
    assert len(registry.get_enabled()) == 1


def test_strategy_registry_unregister():
    """Test strategy unregistration."""
    registry = StrategyRegistry()
    strategy = MockStrategy(name="test1")
    
    registry.register(strategy)
    registry.unregister("test1")
    
    assert registry.get("test1") is None
    assert len(registry.get_enabled()) == 0


def test_strategy_registry_disabled():
    """Test that disabled strategies are not returned."""
    registry = StrategyRegistry()
    strategy1 = MockStrategy(name="enabled", enabled=True)
    strategy2 = MockStrategy(name="disabled", enabled=False)
    
    registry.register(strategy1)
    registry.register(strategy2)
    
    enabled = registry.get_enabled()
    assert len(enabled) == 1
    assert enabled[0].name == "enabled"


def test_strategy_registry_scan_all():
    """Test scanning all strategies."""
    registry = StrategyRegistry()
    strategy1 = MockStrategy(name="strat1")
    strategy2 = MockStrategy(name="strat2")
    
    registry.register(strategy1)
    registry.register(strategy2)
    
    signals = registry.scan_all({})
    
    assert len(signals) == 2
    assert strategy1.scan_called
    assert strategy2.scan_called


def test_strategy_registry_scan_all_with_failure():
    """Test that scan_all continues even if one strategy fails."""
    registry = StrategyRegistry()
    strategy1 = MockStrategy(name="good", fail_scan=False)
    strategy2 = MockStrategy(name="bad", fail_scan=True)
    strategy3 = MockStrategy(name="also_good", fail_scan=False)
    
    registry.register(strategy1)
    registry.register(strategy2)
    registry.register(strategy3)
    
    # Should get signals from good strategies despite one failing
    signals = registry.scan_all({})
    
    assert len(signals) == 2  # From strategy1 and strategy3
    assert strategy1.scan_called
    assert strategy2.scan_called
    assert strategy3.scan_called


def test_opportunity_creation():
    """Test creating an Opportunity."""
    opp = Opportunity(
        strategy_type=StrategyType.ARBITRAGE,
        expected_profit=Decimal("1.5"),
        confidence=Decimal("0.95"),
        urgency=8,
        metadata={"key": "value"},
    )
    
    assert opp.strategy_type == StrategyType.ARBITRAGE
    assert opp.expected_profit == Decimal("1.5")
    assert opp.confidence == Decimal("0.95")
    assert opp.urgency == 8
    assert opp.metadata["key"] == "value"


def test_trade_creation():
    """Test creating a Trade."""
    trade = Trade(
        token_id="123",
        side="BUY",
        size=Decimal("10"),
        price=Decimal("0.5"),
        order_type="FOK",
    )
    
    assert trade.token_id == "123"
    assert trade.side == "BUY"
    assert trade.size == Decimal("10")
    assert trade.price == Decimal("0.5")
    assert trade.order_type == "FOK"


def test_strategy_signal_creation():
    """Test creating a StrategySignal."""
    opportunity = Opportunity(
        strategy_type=StrategyType.GUARANTEED_WIN,
        expected_profit=Decimal("2.0"),
        confidence=Decimal("0.99"),
        urgency=10,
        metadata={},
    )
    
    trades = [
        Trade("token1", "BUY", Decimal("5"), Decimal("0.8")),
        Trade("token2", "BUY", Decimal("5"), Decimal("0.7")),
    ]
    
    signal = StrategySignal(
        opportunity=opportunity,
        trades=trades,
        max_total_cost=Decimal("7.5"),
        min_expected_return=Decimal("10"),
    )
    
    assert signal.opportunity == opportunity
    assert len(signal.trades) == 2
    assert signal.max_total_cost == Decimal("7.5")
    assert signal.min_expected_return == Decimal("10")
