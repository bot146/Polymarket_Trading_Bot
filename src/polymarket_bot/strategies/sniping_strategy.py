"""High-frequency sniping strategy.

This strategy is intentionally simple:
- It looks for *wide spreads* (ask - bid) relative to the mid.
- It then tries to buy at a price meaningfully *below the mid*.

In live trading, sniping is tricky: you need queue position and strict slippage
controls. Here we focus on producing frequent *paper* signals that are grounded
in executable top-of-book data (best_bid/best_ask) so we can iterate fast.

Market data contract (per token):
- token_id: str
- outcome: str
- best_bid: float | None
- best_ask: float | None
- price: float (Gamma fallback)

The strategy will only act when best_bid and best_ask are available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from polymarket_bot.strategy import Opportunity, Strategy, StrategySignal, StrategyType, Trade

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SnipingConfig:
    """Tuning knobs for sniping.

    The defaults are conservative and meant to generate signals without
    pretending we can reliably fill deep in the book.
    """

    # minimum spread to consider, in basis points (bps). 100 bps = 1%
    min_spread_bps: Decimal = Decimal("75")

    # require the buy price to be X bps below mid (i.e. we "snipe" cheap)
    min_discount_to_mid_bps: Decimal = Decimal("25")

    # absolute max we will pay for any single share
    max_entry_price: Decimal = Decimal("0.99")

    # risk control
    max_order_usdc: Decimal = Decimal("5")

    # how many tokens per scan (prevents a noisy feed from spamming)
    max_signals_per_scan: int = 5


class SnipingStrategy(Strategy):
    """Generates BUY signals for tokens with wide spreads and a cheap ask."""

    def __init__(self, config: SnipingConfig | None = None, enabled: bool = True) -> None:
        super().__init__(name=StrategyType.HIGH_FREQUENCY_SNIPING.value, enabled=enabled)
        self.config = config or SnipingConfig()

    def scan(self, market_data: dict[str, Any]) -> list[StrategySignal]:
        markets = market_data.get("markets", [])
        signals: list[StrategySignal] = []

        for market in markets:
            condition_id = str(market.get("condition_id") or "")
            question = str(market.get("question") or "")

            for token in market.get("tokens", []) or []:
                if len(signals) >= self.config.max_signals_per_scan:
                    return signals

                token_id = str(token.get("token_id") or "")
                outcome = str(token.get("outcome") or "")

                # Require real top-of-book.
                bid = token.get("best_bid")
                ask = token.get("best_ask")
                if bid is None or ask is None:
                    continue

                try:
                    bid_d = Decimal(str(bid))
                    ask_d = Decimal(str(ask))
                except Exception:
                    continue

                if bid_d <= 0 or ask_d <= 0:
                    continue
                if ask_d <= bid_d:
                    continue

                mid = (bid_d + ask_d) / 2
                if mid <= 0:
                    continue

                spread_bps = (ask_d - bid_d) / mid * Decimal("10000")
                if spread_bps < self.config.min_spread_bps:
                    continue

                # Try to buy at a discount to mid. In reality we'd place a limit
                # order near bid, but in this codebase Trade.price is used as the
                # intended limit.
                target_price = bid_d  # anchored to bid (the most likely to fill)

                discount_bps = (mid - target_price) / mid * Decimal("10000")
                if discount_bps < self.config.min_discount_to_mid_bps:
                    continue

                if target_price > self.config.max_entry_price:
                    continue

                # Position sizing: cap by max_order_usdc.
                if target_price <= 0:
                    continue
                size = (self.config.max_order_usdc / target_price).quantize(Decimal("0.01"))
                if size <= 0:
                    continue

                # Expected profit is heuristic: if we buy at bid, mark to mid.
                expected_profit = (mid - target_price) * size

                opportunity = Opportunity(
                    strategy_type=StrategyType.HIGH_FREQUENCY_SNIPING,
                    expected_profit=expected_profit,
                    confidence=Decimal("0.55"),
                    urgency=6,
                    metadata={
                        "condition_id": condition_id,
                        "question": question,
                        "token_id": token_id,
                        "outcome": outcome,
                        "best_bid": float(bid_d),
                        "best_ask": float(ask_d),
                        "mid": float(mid),
                        "spread_bps": float(spread_bps),
                        "discount_to_mid_bps": float(discount_bps),
                    },
                )

                signal = StrategySignal(
                    opportunity=opportunity,
                    trades=[
                        Trade(
                            token_id=token_id,
                            side="BUY",
                            size=size,
                            price=target_price,
                            order_type="GTC",
                        )
                    ],
                    max_total_cost=target_price * size,
                    min_expected_return=Decimal("0"),
                )

                log.debug(
                    "Sniping signal: condition=%s token=%s spread_bps=%.1f discount_bps=%.1f",
                    condition_id[:8],
                    token_id[:8],
                    float(spread_bps),
                    float(discount_bps),
                )

                signals.append(signal)

        return signals

    def validate(self, signal: StrategySignal) -> tuple[bool, str]:
        # Guard rails: ensure the signal cost doesn't exceed cap.
        if signal.max_total_cost <= 0:
            return False, "non_positive_cost"

        if signal.max_total_cost > self.config.max_order_usdc:
            return False, "exceeds_max_order"

        # Ensure there is exactly one BUY trade.
        if len(signal.trades) != 1:
            return False, "unexpected_trade_count"

        trade = signal.trades[0]
        if trade.side != "BUY":
            return False, "only_buy_supported"

        if trade.price <= 0 or trade.price > self.config.max_entry_price:
            return False, "invalid_price"

        if trade.size <= 0:
            return False, "invalid_size"

        return True, "ok"
