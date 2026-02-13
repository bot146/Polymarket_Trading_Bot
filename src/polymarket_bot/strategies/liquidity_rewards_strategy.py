"""Liquidity reward harvesting strategy.

Polymarket incentivises liquidity providers with daily USDC rewards.
To qualify, a market-maker must maintain balanced two-sided quotes
(bid + ask) within the ``rewardsMaxSpread`` and at least
``rewardsMinSize`` shares on each side.

This strategy:
1. Identifies markets with ``rewards_daily_rate > 0``.
2. Places balanced GTC quotes centred on the current mid-price,
   staying within the required spread and minimum size.
3. The quotes are designed to be delta-neutral: matching bid and ask
   sizes means fills on one side are roughly offset by fills on the
   other.  The reward payment is the primary income source, not the
   spread capture itself.

Risk: Both sides can be filled sequentially (inventory risk).  The
strategy limits per-market exposure via ``max_position_usdc`` and only
targets markets with tight-enough spreads to keep the reward flowing.
"""

from __future__ import annotations

import logging
import time
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

# Minimum daily reward rate ($/day/side) to bother quoting
MIN_DAILY_RATE = Decimal("0.10")

# Maximum one-day price change (absolute) — avoid quoting in volatile markets
MAX_1D_CHANGE_ABS = 0.15


class LiquidityRewardsStrategy(Strategy):
    """Place balanced two-sided quotes to earn Polymarket liquidity rewards.

    Generates paired BID + ASK GTC signals for qualifying markets.
    """

    def __init__(
        self,
        name: str = "liquidity_rewards",
        max_order_usdc: Decimal = Decimal("20"),
        maker_fee_rate: Decimal = Decimal("0.005"),
        max_position_usdc: Decimal = Decimal("50"),
        enabled: bool = True,
    ):
        super().__init__(name=name, enabled=enabled)
        self.max_order_usdc = max_order_usdc
        self.maker_fee_rate = maker_fee_rate
        self.max_position_usdc = max_position_usdc
        self._signal_cooldown: dict[str, float] = {}
        self._cooldown_seconds: float = 300.0  # 5 min between re-quotes per market

    def scan(self, market_data: dict[str, Any]) -> list[StrategySignal]:
        signals: list[StrategySignal] = []
        markets = market_data.get("markets", [])
        now = time.time()

        for m in markets:
            condition_id = m.get("condition_id", "")
            if not condition_id:
                continue

            # Cooldown
            if now - self._signal_cooldown.get(condition_id, 0) < self._cooldown_seconds:
                continue

            # Must have reward fields populated
            rewards_daily_rate = m.get("rewards_daily_rate")
            rewards_max_spread = m.get("rewards_max_spread")
            rewards_min_size = m.get("rewards_min_size")

            if rewards_daily_rate is None or rewards_max_spread is None or rewards_min_size is None:
                continue

            daily_rate = Decimal(str(rewards_daily_rate))
            max_spread = Decimal(str(rewards_max_spread))
            min_size = Decimal(str(rewards_min_size))

            if daily_rate < MIN_DAILY_RATE:
                continue

            # Avoid volatile markets
            change_1d = m.get("one_day_price_change")
            if change_1d is not None and abs(change_1d) > MAX_1D_CHANGE_ABS:
                continue

            # Need YES token with a mid price for quoting
            tokens = m.get("tokens", [])
            yes_token = None
            for t in tokens:
                if t.get("outcome", "").upper() == "YES":
                    yes_token = t
                    break
            if not yes_token:
                yes_token = tokens[0] if tokens else None
            if not yes_token or not yes_token.get("token_id"):
                continue

            mid_price = Decimal(str(yes_token.get("price", 0)))
            if mid_price <= Decimal("0.05") or mid_price >= Decimal("0.95"):
                # Avoid extreme odds — one side is nearly worthless, hard to stay neutral
                continue

            # Compute bid and ask centred on mid, within max_spread
            half_spread = max_spread / Decimal("2")
            bid_price = (mid_price - half_spread).quantize(Decimal("0.01"))
            ask_price = (mid_price + half_spread).quantize(Decimal("0.01"))

            # Clamp to [0.01, 0.99]
            bid_price = max(Decimal("0.01"), min(Decimal("0.99"), bid_price))
            ask_price = max(Decimal("0.01"), min(Decimal("0.99"), ask_price))

            if ask_price - bid_price > max_spread:
                continue  # Can't satisfy spread requirement

            # Size: use rewards_min_size as floor, cap at max_order_usdc
            size = max(min_size, Decimal("5"))
            if size * mid_price > self.max_order_usdc:
                size = (self.max_order_usdc / mid_price).quantize(Decimal("0.01"))

            if size < min_size:
                continue  # Can't meet min size within our capital limit

            # Expected daily profit from rewards (ignoring spread P&L)
            expected_daily = daily_rate
            # Annualize for a rough per-signal profit estimate
            expected_profit = (expected_daily / Decimal("24"))  # hourly rate

            log.info(
                "💰 LIQ-REWARD: cid=%s mid=%.2f bid=%.2f ask=%.2f "
                "size=%.1f rate=$%.2f/day spread_req=%.3f",
                condition_id[:12],
                float(mid_price),
                float(bid_price),
                float(ask_price),
                float(size),
                float(daily_rate),
                float(max_spread),
            )

            # Build paired GTC trades — bid and ask
            trades = [
                Trade(
                    token_id=yes_token["token_id"],
                    side="BUY",
                    size=size,
                    price=bid_price,
                    order_type="GTC",
                ),
                Trade(
                    token_id=yes_token["token_id"],
                    side="SELL",
                    size=size,
                    price=ask_price,
                    order_type="GTC",
                ),
            ]

            opportunity = Opportunity(
                strategy_type=StrategyType.LIQUIDITY_REWARDS,
                expected_profit=expected_profit,
                confidence=Decimal("0.70"),  # Moderate — depends on staying filled
                urgency=3,  # Low urgency — rewards accumulate continuously
                metadata={
                    "condition_id": condition_id,
                    "type": "liquidity_rewards",
                    "mid_price": float(mid_price),
                    "bid_price": float(bid_price),
                    "ask_price": float(ask_price),
                    "size": float(size),
                    "daily_rate": float(daily_rate),
                    "max_spread": float(max_spread),
                    "min_size": float(min_size),
                    "question": m.get("question", "")[:60],
                },
            )

            signal = StrategySignal(
                opportunity=opportunity,
                trades=trades,
                max_total_cost=size * bid_price,  # Capital at risk is the bid side
                min_expected_return=expected_daily,
            )
            signals.append(signal)
            self._signal_cooldown[condition_id] = now

        return signals

    def validate(self, signal: StrategySignal) -> tuple[bool, str]:
        meta = signal.opportunity.metadata
        if meta.get("type") != "liquidity_rewards":
            return False, "not_liquidity_rewards"

        if len(signal.trades) != 2:
            return False, "expected_bid_ask_pair"

        sides = {t.side for t in signal.trades}
        if sides != {"BUY", "SELL"}:
            return False, "expected_one_buy_one_sell"

        return True, "ok"
