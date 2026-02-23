"""Near-resolution sniping strategy.

Markets that are about to resolve (end_date within 24–48 hours) with a
near-certain outcome (YES token ≥ 95¢) offer a structural edge:

    Edge = $1.00 − ask_price − taker_fee

The outcome is essentially determined — the market is just waiting for
the official resolution.  Buying the near-certain YES token captures
the last few cents of edge with very low risk.

Risk: The "certain" outcome may flip (upset event).  Mitigated by:
- Only targeting tokens ≥ 95¢ (strong consensus)
- Short holding period (resolves within hours, not days)
- Small position sizing
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
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

# Price thresholds
MIN_YES_PRICE = Decimal("0.95")       # Token must trade at ≥ 95¢
MAX_YES_PRICE = Decimal("0.995")      # Don't buy if already at 99.5¢ — no edge

# Time window
MIN_HOURS_TO_END = 1.0                # Must be ≥ 1h from resolution
MAX_HOURS_TO_END = 48.0               # Must be ≤ 48h from resolution


class NearResolutionStrategy(Strategy):
    """Buy near-certain outcomes in markets about to resolve.

    Edge = $1.00 − best_ask − taker_fee_on_ask.
    """

    def __init__(
        self,
        name: str = "near_resolution",
        min_edge_cents: Decimal = Decimal("0.3"),
        max_order_usdc: Decimal = Decimal("20"),
        taker_fee_rate: Decimal = Decimal("0.02"),
        min_yes_price: Decimal = MIN_YES_PRICE,
        max_hours_to_end: float = MAX_HOURS_TO_END,
        enabled: bool = True,
    ):
        super().__init__(name=name, enabled=enabled)
        self.min_edge_cents = min_edge_cents
        self.max_order_usdc = max_order_usdc
        self.taker_fee_rate = taker_fee_rate
        self.min_yes_price = min_yes_price
        self.max_hours_to_end = max_hours_to_end
        self._signal_cooldown: dict[str, float] = {}
        self._cooldown_seconds: float = 300.0  # 5 min per market

    def scan(self, market_data: dict[str, Any]) -> list[StrategySignal]:
        signals: list[StrategySignal] = []
        markets = market_data.get("markets", [])
        now_utc = datetime.now(timezone.utc)
        now_ts = time.time()

        # Prune expired cooldown entries to prevent memory leaks
        cutoff = now_ts - self._cooldown_seconds * 2
        self._signal_cooldown = {k: v for k, v in self._signal_cooldown.items() if v > cutoff}

        for m in markets:
            condition_id = m.get("condition_id", "")
            if not condition_id:
                continue

            # Cooldown
            if now_ts - self._signal_cooldown.get(condition_id, 0) < self._cooldown_seconds:
                continue

            # Must be active
            if not m.get("active", False):
                continue

            # Must have an end_date
            end_date_str = m.get("end_date")
            if not end_date_str:
                continue

            hours_to_end = self._hours_until(end_date_str, now_utc)
            if hours_to_end is None:
                continue
            if hours_to_end < MIN_HOURS_TO_END or hours_to_end > self.max_hours_to_end:
                continue

            # Find the YES token with a high price
            tokens = m.get("tokens", [])
            best_token = self._find_near_certain_token(tokens)
            if best_token is None:
                continue

            best_ask = Decimal(str(best_token["best_ask"]))
            fee = best_ask * self.taker_fee_rate
            edge = Decimal("1") - best_ask - fee
            edge_cents = edge * Decimal("100")

            if edge_cents < self.min_edge_cents:
                continue

            # Size — how many shares can we buy (including fees)?
            cost_per_share = best_ask * (Decimal("1") + self.taker_fee_rate)
            size = (self.max_order_usdc / cost_per_share).quantize(Decimal("0.01"))
            if size <= 0:
                continue

            expected_profit = edge * size

            log.info(
                "⏰ NEAR-RES: cid=%s ask=%.3f edge=%.2f¢ "
                "hours_left=%.1f size=%.2f profit=$%.4f  q=%s",
                condition_id[:12],
                float(best_ask),
                float(edge_cents),
                hours_to_end,
                float(size),
                float(expected_profit),
                m.get("question", "")[:50],
            )

            trades = [
                Trade(
                    token_id=best_token["token_id"],
                    side="BUY",
                    size=size,
                    price=best_ask,
                    order_type="FOK",
                ),
            ]

            opportunity = Opportunity(
                strategy_type=StrategyType.NEAR_RESOLUTION,
                expected_profit=expected_profit,
                confidence=Decimal("0.92"),
                urgency=6,  # Moderate-high — time-sensitive
                metadata={
                    "condition_id": condition_id,
                    "type": "near_resolution",
                    "best_ask": float(best_ask),
                    "edge_cents": float(edge_cents),
                    "hours_to_end": hours_to_end,
                    "outcome": best_token.get("outcome", "YES"),
                    "question": m.get("question", "")[:80],
                },
            )

            signal = StrategySignal(
                opportunity=opportunity,
                trades=trades,
                max_total_cost=best_ask * size,
                min_expected_return=size,  # Pays $1/share on resolution
            )
            signals.append(signal)
            self._signal_cooldown[condition_id] = now_ts

        return signals

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hours_until(end_date_str: str, now_utc: datetime) -> float | None:
        """Parse ISO date string and return hours until that time."""
        try:
            # Handle various ISO formats from Gamma API
            end_date_str = end_date_str.rstrip("Z")
            if "T" in end_date_str:
                dt = datetime.fromisoformat(end_date_str).replace(tzinfo=timezone.utc)
            else:
                dt = datetime.fromisoformat(end_date_str + "T00:00:00").replace(tzinfo=timezone.utc)
            delta = dt - now_utc
            return delta.total_seconds() / 3600.0
        except (ValueError, TypeError):
            return None

    def _find_near_certain_token(self, tokens: list[dict]) -> dict | None:
        """Find a YES token trading at ≥ min_yes_price with a valid best_ask."""
        best: dict | None = None
        best_price = Decimal("0")

        for t in tokens:
            if t.get("outcome", "").upper() not in ("YES", ""):
                # Only consider YES tokens (or unlabelled ones)
                continue

            best_ask = t.get("best_ask")
            if best_ask is None or best_ask <= 0:
                continue

            price = Decimal(str(best_ask))
            if price < self.min_yes_price or price > MAX_YES_PRICE:
                continue

            if price > best_price:
                best_price = price
                best = t

        return best

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self, signal: StrategySignal) -> tuple[bool, str]:
        meta = signal.opportunity.metadata
        if meta.get("type") != "near_resolution":
            return False, "not_near_resolution"

        if len(signal.trades) != 1:
            return False, "expected_single_trade"

        t = signal.trades[0]
        if t.side != "BUY":
            return False, "expected_buy"

        if t.price < self.min_yes_price:
            return False, "price_dropped_below_threshold"

        return True, "ok"
