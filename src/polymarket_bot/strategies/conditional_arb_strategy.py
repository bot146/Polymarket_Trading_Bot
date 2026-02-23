"""Conditional probability arbitrage strategy.

In negRisk grouped markets with ordered/cumulative brackets (e.g. "Total
viewers: <250k, 250–500k, 500–750k, 750k–1M, >1M"), the cumulative
probability of all brackets above a threshold must equal the complement
of all brackets below it.

For example, if the bracket "≥500k" can be synthesised by buying all
brackets from 500k upward, the sum of their ask prices must not exceed
the complement (1 − sum of bids for brackets below 500k).  When they
do, there is an arbitrage opportunity.

More generally, for any split point *k* in an ordered, exhaustive group:
    SUM(ask[k:]) + fees  <  1 − SUM(bid[:k])

This strategy detects such violations and generates BUY signals for
the cheaper side.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
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

# Minimum brackets for this strategy to apply
MIN_BRACKETS = 4

# Only consider groups whose SUM(mid) is close to 1.0 (exhaustive)
SUM_MID_MIN = Decimal("0.90")
SUM_MID_MAX = Decimal("1.10")


class ConditionalArbStrategy(Strategy):
    """Detect cumulative probability violations in ordered bracket groups.

    Generates BUY signals for the cheaper side of a split-point
    arbitrage within a negRisk group.
    """

    def __init__(
        self,
        name: str = "conditional_arb",
        min_edge_cents: Decimal = Decimal("0.5"),
        max_order_usdc: Decimal = Decimal("50"),
        taker_fee_rate: Decimal = Decimal("0.02"),
        enabled: bool = True,
    ):
        super().__init__(name=name, enabled=enabled)
        self.min_edge_cents = min_edge_cents
        self.max_order_usdc = max_order_usdc
        self.taker_fee_rate = taker_fee_rate
        self._signal_cooldown: dict[str, float] = {}
        self._cooldown_seconds: float = 120.0

    # ------------------------------------------------------------------
    # Core scanning
    # ------------------------------------------------------------------

    def scan(self, market_data: dict[str, Any]) -> list[StrategySignal]:
        signals: list[StrategySignal] = []
        markets = market_data.get("markets", [])

        # 1. Group by negRiskMarketID
        groups: dict[str, list[dict]] = defaultdict(list)
        for m in markets:
            nrid = m.get("neg_risk_market_id")
            if nrid:
                groups[nrid].append(m)

        now = time.time()

        # Prune expired cooldown entries to prevent memory leaks
        cutoff = now - self._cooldown_seconds * 2
        self._signal_cooldown = {k: v for k, v in self._signal_cooldown.items() if v > cutoff}

        for group_id, brackets in groups.items():
            if len(brackets) < MIN_BRACKETS:
                continue

            # Cooldown
            if now - self._signal_cooldown.get(group_id, 0) < self._cooldown_seconds:
                continue

            # Collect YES token bid/ask per bracket
            bracket_data = self._collect_bracket_data(brackets)
            if bracket_data is None:
                continue

            # Check SUM(mid) sanity
            sum_mid = sum(bd["mid"] for bd in bracket_data)
            if sum_mid < SUM_MID_MIN or sum_mid > SUM_MID_MAX:
                continue

            # 2. Try every split point k ∈ [1, N-1]
            #    "buy upper" side: BUY all brackets k..N
            #    The complementary cost of the lower side uses best_bid
            best_signal = self._find_best_split(group_id, bracket_data)
            if best_signal is not None:
                signals.append(best_signal)
                self._signal_cooldown[group_id] = now

        return signals

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _collect_bracket_data(self, brackets: list[dict]) -> list[dict] | None:
        """Parse YES token data from each bracket, sorted by mid price ascending.

        Lower-priced brackets correspond to lower-probability outcomes,
        giving us a natural ordering for cumulative split-point analysis.
        """
        data: list[dict] = []
        for b in brackets:
            tokens = b.get("tokens", [])
            yes_token = None
            for t in tokens:
                if t.get("outcome", "").upper() == "YES":
                    yes_token = t
                    break
            if not yes_token:
                yes_token = tokens[0] if tokens else None
            if not yes_token or not yes_token.get("token_id"):
                return None

            best_ask = yes_token.get("best_ask")
            best_bid = yes_token.get("best_bid")
            mid = yes_token.get("price", 0)
            if best_ask is None or best_ask <= 0:
                return None
            if best_bid is None or best_bid <= 0:
                return None

            data.append({
                "token_id": yes_token["token_id"],
                "condition_id": b.get("condition_id", ""),
                "label": b.get("group_item_title") or b.get("question", "")[:40],
                "mid": Decimal(str(mid)),
                "best_ask": Decimal(str(best_ask)),
                "best_bid": Decimal(str(best_bid)),
            })

        # Sort by mid ascending — gives natural bracket ordering
        data.sort(key=lambda d: d["mid"])
        return data

    def _find_best_split(
        self,
        group_id: str,
        bracket_data: list[dict],
    ) -> StrategySignal | None:
        """Try every split point and return the best arb signal, if any.

        For split point *k* (1 ≤ k ≤ N−1):
            upper_cost = SUM(best_ask[k:])          # cost to buy all upper brackets
            lower_bid  = SUM(best_bid[:k])           # what we'd receive selling lower
            implied_upper_value = 1 − lower_bid      # fair value of the upper set
            edge = implied_upper_value − upper_cost − fees(upper_cost)

        If edge > 0 we can buy the upper set for less than implied value.
        """
        n = len(bracket_data)
        best_edge = Decimal("0")
        best_k: int | None = None
        best_upper_cost = Decimal("0")

        # Pre-compute prefix sums for efficiency
        prefix_bid = [Decimal("0")] * (n + 1)
        suffix_ask = [Decimal("0")] * (n + 1)

        for i in range(n):
            prefix_bid[i + 1] = prefix_bid[i] + bracket_data[i]["best_bid"]
        for i in range(n - 1, -1, -1):
            suffix_ask[i] = suffix_ask[i + 1] + bracket_data[i]["best_ask"]

        for k in range(1, n):
            lower_bid = prefix_bid[k]            # SUM(bid[:k])
            upper_cost = suffix_ask[k]           # SUM(ask[k:])
            upper_fees = upper_cost * self.taker_fee_rate
            implied_upper = Decimal("1") - lower_bid
            edge = implied_upper - upper_cost - upper_fees

            if edge > best_edge:
                best_edge = edge
                best_k = k
                best_upper_cost = upper_cost

        if best_k is None:
            return None

        edge_cents = best_edge * Decimal("100")
        if edge_cents < self.min_edge_cents:
            return None

        # Build trades — BUY all brackets from k onward
        upper_brackets = bracket_data[best_k:]
        upper_cost = best_upper_cost

        size = (self.max_order_usdc / upper_cost).quantize(Decimal("0.01"))
        if size <= 0:
            return None

        expected_profit = best_edge * size

        log.info(
            "🔗 COND-ARB: split@%d/%d, upper_cost=%.4f, edge=%.2f¢, "
            "size=%.2f, profit=$%.4f  group=%s",
            best_k, n,
            float(upper_cost),
            float(edge_cents),
            float(size),
            float(expected_profit),
            group_id[:12],
        )

        trades = [
            Trade(
                token_id=bd["token_id"],
                side="BUY",
                size=size,
                price=bd["best_ask"],
                order_type="FOK",
            )
            for bd in upper_brackets
        ]

        opportunity = Opportunity(
            strategy_type=StrategyType.CONDITIONAL_ARB,
            expected_profit=expected_profit,
            confidence=Decimal("0.90"),
            urgency=7,
            metadata={
                "condition_id": group_id,
                "type": "conditional_arb",
                "split_point": best_k,
                "num_brackets": n,
                "upper_brackets": len(upper_brackets),
                "upper_cost": float(upper_cost),
                "edge_cents": float(edge_cents),
                "bracket_labels": [bd["label"] for bd in upper_brackets],
                # Per-bracket Gamma condition_ids for resolution monitoring.
                "bracket_condition_ids": {bd["token_id"]: bd["condition_id"] for bd in upper_brackets},
            },
        )

        return StrategySignal(
            opportunity=opportunity,
            trades=trades,
            max_total_cost=upper_cost * size,
            min_expected_return=(Decimal("1") - prefix_bid[best_k]) * size,
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self, signal: StrategySignal) -> tuple[bool, str]:
        meta = signal.opportunity.metadata
        if meta.get("type") != "conditional_arb":
            return False, "not_conditional_arb"

        if not all(t.side == "BUY" for t in signal.trades):
            return False, "not_all_buys"

        sizes = {t.size for t in signal.trades}
        if len(sizes) != 1:
            return False, "size_mismatch"

        return True, "ok"
