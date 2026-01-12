"""Statistical arbitrage strategy for correlated markets.

This strategy identifies markets that should move together (e.g., "Trump wins"
and "GOP Senate control") and trades when their prices diverge beyond expected levels.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from polymarket_bot.strategy import (
    Opportunity,
    Strategy,
    StrategySignal,
    StrategyType,
    Trade,
)

log = logging.getLogger(__name__)


# Predefined correlated market pairs
# TODO: Move to external configuration file or database for easier updates
# In production, these could be:
# 1. Loaded from a JSON/YAML config file
# 2. Learned from historical correlation analysis
# 3. Updated dynamically based on market conditions
CORRELATION_PAIRS = [
    {
        "name": "Trump + GOP Senate",
        "markets": ["trump_wins", "gop_senate"],
        "correlation": 0.85,
        "divergence_threshold": 0.07,  # 7% divergence
    },
    {
        "name": "BTC + ETH",
        "markets": ["btc_up", "eth_up"],
        "correlation": 0.90,
        "divergence_threshold": 0.05,
    },
]


class StatisticalArbitrageStrategy(Strategy):
    """Strategy for trading divergences in correlated markets.
    
    When two markets that typically move together diverge significantly,
    we can short the expensive one and long the cheap one, profiting
    when they converge.
    """

    def __init__(
        self,
        name: str = "statistical_arbitrage",
        min_divergence: Decimal = Decimal("0.04"),  # 4% minimum divergence
        max_divergence: Decimal = Decimal("0.15"),  # 15% maximum (may indicate decorrelation)
        max_order_usdc: Decimal = Decimal("30"),
        enabled: bool = True,
    ):
        super().__init__(name=name, enabled=enabled)
        self.min_divergence = min_divergence
        self.max_divergence = max_divergence
        self.max_order_usdc = max_order_usdc
        self.correlation_pairs = CORRELATION_PAIRS

    def scan(self, market_data: dict[str, Any]) -> list[StrategySignal]:
        """Scan for divergences in correlated market pairs.
        
        Args:
            market_data: Dict with structure:
                {
                    "markets": [
                        {
                            "condition_id": str,
                            "tags": [str],  # Keywords for matching
                            "tokens": [
                                {"token_id": str, "outcome": "YES", "price": float, "best_ask": float, "best_bid": float},
                            ]
                        }
                    ]
                }
        """
        signals = []
        markets = market_data.get("markets", [])

        # Build market index by keywords/tags
        market_index = self._index_markets(markets)

        # Check each correlation pair
        for pair_config in self.correlation_pairs:
            try:
                signal = self._check_pair_divergence(market_index, pair_config)
                if signal:
                    signals.append(signal)
            except Exception as e:
                log.debug(f"Failed to check pair {pair_config['name']}: {e}")
                continue

        return signals

    def validate(self, signal: StrategySignal) -> tuple[bool, str]:
        """Validate statistical arbitrage signal before execution."""
        if signal.opportunity.strategy_type != StrategyType.STATISTICAL_ARBITRAGE:
            return False, "not_stat_arb_strategy"

        if len(signal.trades) != 2:
            return False, "invalid_trade_count"

        # Should have one long and one short
        sides = [t.side for t in signal.trades]
        if "BUY" not in sides or "SELL" not in sides:
            return False, "must_have_long_and_short"

        # Verify divergence is still valid
        divergence = signal.opportunity.metadata.get("divergence")
        if divergence is None:
            return False, "missing_divergence_data"

        divergence = Decimal(str(divergence))
        if divergence < self.min_divergence:
            return False, "divergence_too_small"

        if divergence > self.max_divergence:
            return False, "divergence_too_large"

        return True, "ok"

    def _index_markets(self, markets: list[dict[str, Any]]) -> dict[str, list[dict]]:
        """Index markets by keywords for quick lookup."""
        index: dict[str, list[dict]] = {}
        
        for market in markets:
            question = market.get("question", "").lower()
            tags = market.get("tags", [])
            
            # Extract keywords from question and tags
            keywords = set()
            keywords.update(tags)
            
            # Simple keyword extraction
            for word in question.split():
                if len(word) > 3:  # Skip short words
                    keywords.add(word.lower())
            
            for keyword in keywords:
                if keyword not in index:
                    index[keyword] = []
                index[keyword].append(market)
        
        return index

    def _check_pair_divergence(
        self,
        market_index: dict[str, list[dict]],
        pair_config: dict[str, Any]
    ) -> StrategySignal | None:
        """Check if a correlated pair has diverged enough to trade."""
        # Find markets matching the pair keywords
        market_a_keywords = pair_config["markets"][0].split("_")
        market_b_keywords = pair_config["markets"][1].split("_")

        markets_a = self._find_markets_by_keywords(market_index, market_a_keywords)
        markets_b = self._find_markets_by_keywords(market_index, market_b_keywords)

        if not markets_a or not markets_b:
            return None

        # Take the first/best match for simplicity
        market_a = markets_a[0]
        market_b = markets_b[0]

        # Get YES token prices (assuming we're trading YES outcomes)
        price_a = self._get_yes_price(market_a)
        price_b = self._get_yes_price(market_b)

        if price_a is None or price_b is None:
            return None

        # Calculate divergence
        divergence = abs(price_a - price_b)

        if divergence < self.min_divergence or divergence > self.max_divergence:
            return None

        # Determine which to long and which to short
        if price_a > price_b:
            expensive_market = market_a
            expensive_price = price_a
            cheap_market = market_b
            cheap_price = price_b
        else:
            expensive_market = market_b
            expensive_price = price_b
            cheap_market = market_a
            cheap_price = price_a

        # Calculate position size (equal dollar amount on each side)
        size_expensive = self._calculate_size(expensive_price)
        size_cheap = self._calculate_size(cheap_price)

        if size_expensive <= 0 or size_cheap <= 0:
            return None

        # Get token IDs
        expensive_token_id = self._get_yes_token_id(expensive_market)
        cheap_token_id = self._get_yes_token_id(cheap_market)

        if not expensive_token_id or not cheap_token_id:
            return None

        opportunity = Opportunity(
            strategy_type=StrategyType.STATISTICAL_ARBITRAGE,
            expected_profit=divergence * min(size_expensive, size_cheap),
            confidence=Decimal("0.70"),  # Medium confidence - depends on correlation holding
            urgency=6,  # Medium urgency
            metadata={
                "pair_name": pair_config["name"],
                "divergence": float(divergence),
                "correlation": pair_config["correlation"],
                "expensive_condition": expensive_market.get("condition_id"),
                "cheap_condition": cheap_market.get("condition_id"),
                "expensive_price": float(expensive_price),
                "cheap_price": float(cheap_price),
            },
        )

        trades = [
            Trade(
                token_id=expensive_token_id,
                side="SELL",  # Short the expensive one
                size=size_expensive,
                price=expensive_price,
                order_type="GTC",  # Good till cancel - willing to wait
            ),
            Trade(
                token_id=cheap_token_id,
                side="BUY",  # Long the cheap one
                size=size_cheap,
                price=cheap_price,
                order_type="GTC",
            ),
        ]

        signal = StrategySignal(
            opportunity=opportunity,
            trades=trades,
            max_total_cost=cheap_price * size_cheap + expensive_price * size_expensive,
            min_expected_return=Decimal("0"),  # Uncertain - depends on convergence
        )

        log.info(
            f"Stat arb opportunity: {pair_config['name']} divergence={divergence:.2%}, "
            f"short={expensive_price:.4f} long={cheap_price:.4f}"
        )

        return signal

    def _find_markets_by_keywords(
        self,
        market_index: dict[str, list[dict]],
        keywords: list[str]
    ) -> list[dict]:
        """Find markets matching given keywords."""
        matching_markets = []
        seen_conditions = set()

        for keyword in keywords:
            markets = market_index.get(keyword.lower(), [])
            for market in markets:
                condition_id = market.get("condition_id")
                if condition_id and condition_id not in seen_conditions:
                    matching_markets.append(market)
                    seen_conditions.add(condition_id)

        return matching_markets

    def _get_yes_price(self, market: dict[str, Any]) -> Decimal | None:
        """Get YES token price from market data."""
        tokens = market.get("tokens", [])
        for token in tokens:
            if token.get("outcome", "").upper() == "YES":
                price = token.get("price") or token.get("best_ask")
                if price is not None:
                    return Decimal(str(price))
        return None

    def _get_yes_token_id(self, market: dict[str, Any]) -> str | None:
        """Get YES token ID from market data."""
        tokens = market.get("tokens", [])
        for token in tokens:
            if token.get("outcome", "").upper() == "YES":
                return token.get("token_id")
        return None

    def _calculate_size(self, price: Decimal) -> Decimal:
        """Calculate position size based on max order size."""
        if price <= 0:
            return Decimal("0")
        
        max_size = self.max_order_usdc / price
        return max_size.quantize(Decimal("0.01"))
