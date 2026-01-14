"""Market-making / spread-capture strategy for binary markets.

What you're describing is real in many venues:
- Place *resting* buy orders on both YES and NO (provide liquidity).
- Earn the spread when one side fills and you later exit at a better price.
- Sometimes capture mispricings when YES+NO != 1.00.
- Potentially earn liquidity rewards (venue-specific; not modeled here).

Important nuance:
- "Outcome doesn't matter" is only true if you stay *delta-neutral*.
  That neutrality is achieved by inventory control + symmetric quoting + hedging.
  If you get filled on one side and not the other, you have directional exposure.

This implementation focuses on:
- producing frequent *maker* quote signals (GTC) for paper-mode iteration
- strict guard rails (inventory caps, quote sanity)
- expressing both-sided quoting in the existing StrategySignal/Trade model

Market data contract (per token):
- token_id: str
- outcome: "YES" or "NO"
- best_bid: float | None
- best_ask: float | None

We only quote when both best_bid and best_ask are present.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from polymarket_bot.strategy import Opportunity, Strategy, StrategySignal, StrategyType, Trade

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketMakingConfig:
    # Minimum spread bps required to quote (avoid tight markets where we just churn).
    min_spread_bps: Decimal = Decimal("20")

    # How far inside the spread we quote, in bps.
    # Example: 5 bps means we improve best bid by 5 bps and improve best ask by 5 bps.
    improve_bps: Decimal = Decimal("5")

    # Max we will pay for a YES/NO share.
    max_entry_price: Decimal = Decimal("0.99")

    # Max notional we allocate per side per market per refresh.
    max_order_usdc_per_side: Decimal = Decimal("5")

    # Limit the number of markets we quote per scan (keeps it tame while iterating).
    max_markets_per_scan: int = 10


class MarketMakingStrategy(Strategy):
    """Quote both sides of YES/NO when spreads are wide enough."""

    def __init__(self, config: MarketMakingConfig | None = None, enabled: bool = True) -> None:
        super().__init__(name=StrategyType.MARKET_MAKING.value, enabled=enabled)
        self.config = config or MarketMakingConfig()

    def scan(self, market_data: dict[str, Any]) -> list[StrategySignal]:
        markets = market_data.get("markets", [])
        signals: list[StrategySignal] = []

        for market in markets[: self.config.max_markets_per_scan]:
            tokens = market.get("tokens", []) or []
            if len(tokens) != 2:
                continue

            yes = next((t for t in tokens if str(t.get("outcome", "")).upper() == "YES"), None)
            no = next((t for t in tokens if str(t.get("outcome", "")).upper() == "NO"), None)
            if not yes or not no:
                continue

            maybe = self._quote_market(market)
            if maybe is not None:
                signals.append(maybe)

        return signals

    def validate(self, signal: StrategySignal) -> tuple[bool, str]:
        if signal.opportunity.strategy_type != StrategyType.MARKET_MAKING:
            return False, "not_market_making"

        # We expect to place 4 orders: bid+ask on YES, bid+ask on NO.
        if len(signal.trades) != 4:
            return False, "unexpected_trade_count"

        for t in signal.trades:
            if t.side not in {"BUY", "SELL"}:
                return False, "invalid_side"
            if t.size <= 0:
                return False, "invalid_size"
            if t.price <= 0 or t.price > self.config.max_entry_price:
                return False, "invalid_price"

        if signal.max_total_cost <= 0:
            return False, "non_positive_cost"

        # Maker quoting worst-case is both BUY orders filling (YES bid + NO bid).
        # We allow a tiny rounding cushion since sizes are quantized.
        max_budget = self.config.max_order_usdc_per_side * Decimal("2")
        if signal.max_total_cost > (max_budget + Decimal("0.02")):
            return False, "exceeds_budget"

        return True, "ok"

    def _quote_market(self, market: dict[str, Any]) -> StrategySignal | None:
        condition_id = str(market.get("condition_id") or "")
        question = str(market.get("question") or "")

        tokens = market.get("tokens", []) or []
        yes = next((t for t in tokens if str(t.get("outcome", "")).upper() == "YES"), None)
        no = next((t for t in tokens if str(t.get("outcome", "")).upper() == "NO"), None)
        if not yes or not no:
            return None

        # Need top-of-book.
        y_bid = yes.get("best_bid")
        y_ask = yes.get("best_ask")
        n_bid = no.get("best_bid")
        n_ask = no.get("best_ask")
        if None in (y_bid, y_ask, n_bid, n_ask):
            return None

        y_bid = Decimal(str(y_bid))
        y_ask = Decimal(str(y_ask))
        n_bid = Decimal(str(n_bid))
        n_ask = Decimal(str(n_ask))

        # Require sane books
        if y_bid <= 0 or y_ask <= 0 or n_bid <= 0 or n_ask <= 0:
            return None
        if y_ask <= y_bid or n_ask <= n_bid:
            return None

        # Spread checks (bps)
        y_mid = (y_bid + y_ask) / 2
        n_mid = (n_bid + n_ask) / 2
        y_spread_bps = (y_ask - y_bid) / y_mid * Decimal("10000")
        n_spread_bps = (n_ask - n_bid) / n_mid * Decimal("10000")

        if y_spread_bps < self.config.min_spread_bps and n_spread_bps < self.config.min_spread_bps:
            return None

        # Improvement amount in price terms.
        def _improve(bps: Decimal, mid: Decimal) -> Decimal:
            return (bps / Decimal("10000")) * mid

        y_imp = _improve(self.config.improve_bps, y_mid)
        n_imp = _improve(self.config.improve_bps, n_mid)

        # Quote narrow: buy slightly above best bid, sell slightly below best ask.
        y_quote_bid = (y_bid + y_imp).min(y_ask)  # never cross above ask
        y_quote_ask = (y_ask - y_imp).max(y_bid)  # never cross below bid
        n_quote_bid = (n_bid + n_imp).min(n_ask)
        n_quote_ask = (n_ask - n_imp).max(n_bid)

        # Keep it strictly non-crossing; if improvement collapses it, skip.
        if y_quote_bid >= y_quote_ask or n_quote_bid >= n_quote_ask:
            return None

        # Enforce max price caps for safety.
        if y_quote_bid > self.config.max_entry_price or n_quote_bid > self.config.max_entry_price:
            return None

        # Size: cap cost per side.
        y_size = (self.config.max_order_usdc_per_side / y_quote_bid).quantize(Decimal("0.01"))
        n_size = (self.config.max_order_usdc_per_side / n_quote_bid).quantize(Decimal("0.01"))
        if y_size <= 0 or n_size <= 0:
            return None

        # Expected profit heuristic: if we buy at our bid and later sell at our ask.
        # Not accounting for fees, fills, queue position.
        expected_profit = ((y_quote_ask - y_quote_bid) * y_size) + ((n_quote_ask - n_quote_bid) * n_size)

        opportunity = Opportunity(
            strategy_type=StrategyType.MARKET_MAKING,
            expected_profit=expected_profit,
            confidence=Decimal("0.50"),
            urgency=4,
            metadata={
                "condition_id": condition_id,
                "question": question,
                "yes_token_id": str(yes.get("token_id") or ""),
                "no_token_id": str(no.get("token_id") or ""),
                "yes_quote_bid": float(y_quote_bid),
                "yes_quote_ask": float(y_quote_ask),
                "no_quote_bid": float(n_quote_bid),
                "no_quote_ask": float(n_quote_ask),
                "yes_spread_bps": float(y_spread_bps),
                "no_spread_bps": float(n_spread_bps),
            },
        )

        trades = [
            # YES quotes
            Trade(token_id=str(yes.get("token_id") or ""), side="BUY", size=y_size, price=y_quote_bid, order_type="GTC"),
            Trade(token_id=str(yes.get("token_id") or ""), side="SELL", size=y_size, price=y_quote_ask, order_type="GTC"),
            # NO quotes
            Trade(token_id=str(no.get("token_id") or ""), side="BUY", size=n_size, price=n_quote_bid, order_type="GTC"),
            Trade(token_id=str(no.get("token_id") or ""), side="SELL", size=n_size, price=n_quote_ask, order_type="GTC"),
        ]

        # In maker mode, "max_total_cost" is the worst-case if both BUYs fill.
        max_total_cost = (y_quote_bid * y_size) + (n_quote_bid * n_size)

        return StrategySignal(
            opportunity=opportunity,
            trades=trades,
            max_total_cost=max_total_cost,
            min_expected_return=Decimal("0"),
        )
