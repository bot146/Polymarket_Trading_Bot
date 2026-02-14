"""Multi-outcome arbitrage strategy.

In negRisk grouped markets (e.g. "Who will win the 2028 election?" with 30+
candidate brackets), exactly one bracket must resolve YES.  If the sum of the
best-ask prices for every bracket's YES token is less than $1.00 (after fees),
buying one YES share in every bracket guarantees a profit because exactly one
share will pay $1.00.

This is the multi-outcome equivalent of the binary YES+NO hedge arb, but it
tends to have slightly wider edges because the large number of brackets makes
it harder for the market to keep every ask perfectly efficient.
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

# Minimum number of brackets in a group for the arb to be structurally valid.
# Groups with too few brackets are likely just correlated sub-markets (e.g.
# "win conference" + "win finals") rather than mutually-exclusive outcomes.
MIN_BRACKETS = 3

# We require SUM(YES mid) to be close to 1.0 — this confirms the group is
# truly exhaustive (all outcomes present).  If the sum is far below 1.0, some
# brackets are missing and buying "all" doesn't guarantee a win.
SUM_MID_MIN = Decimal("0.90")
SUM_MID_MAX = Decimal("1.10")


class MultiOutcomeArbStrategy(Strategy):
    """Buy one YES share in every bracket of a multi-outcome group for < $1.

    Edge = $1.00 − SUM(best_ask) − fees.
    """

    def __init__(
        self,
        name: str = "multi_outcome_arb",
        min_edge_cents: Decimal = Decimal("0.5"),
        max_order_usdc: Decimal = Decimal("50"),
        taker_fee_rate: Decimal = Decimal("0.02"),
        enabled: bool = True,
    ):
        super().__init__(name=name, enabled=enabled)
        self.min_edge_cents = min_edge_cents
        self.max_order_usdc = max_order_usdc
        self.taker_fee_rate = taker_fee_rate
        self._signal_cooldown: dict[str, float] = {}  # group_id → last signal epoch
        self._cooldown_seconds: float = 120.0          # don't re-signal same group within 2 min

    def scan(self, market_data: dict[str, Any]) -> list[StrategySignal]:
        """Scan for multi-outcome arb across negRisk grouped markets.

        market_data["markets"] entries may include:
            - "neg_risk_market_id": shared group identifier
            - "group_item_title": bracket label
            - "tokens"[0]["best_ask"]: executable YES ask from CLOB book

        We group markets by neg_risk_market_id, then check whether buying every
        YES token at its best ask sums to < $1.
        """
        signals: list[StrategySignal] = []
        markets = market_data.get("markets", [])

        # 1. Group markets by negRiskMarketID
        groups: dict[str, list[dict]] = defaultdict(list)
        for m in markets:
            nrid = m.get("neg_risk_market_id")
            if nrid:
                groups[nrid].append(m)

        # 2. Evaluate each group
        now = time.time()
        for group_id, brackets in groups.items():
            if len(brackets) < MIN_BRACKETS:
                continue

            # Cooldown: skip groups we recently signaled
            last_signal = self._signal_cooldown.get(group_id, 0)
            if now - last_signal < self._cooldown_seconds:
                continue

            # Collect YES token info for each bracket
            yes_asks: list[Decimal] = []
            yes_mids: list[Decimal] = []
            bracket_trades: list[dict] = []
            valid = True

            for bracket in brackets:
                tokens = bracket.get("tokens", [])
                # YES token is the first token in the list
                yes_token = None
                for t in tokens:
                    if t.get("outcome", "").upper() == "YES":
                        yes_token = t
                        break
                if not yes_token:
                    # Take first token as YES if no explicit outcome label
                    yes_token = tokens[0] if tokens else None

                if not yes_token or not yes_token.get("token_id"):
                    valid = False
                    break

                best_ask = yes_token.get("best_ask")
                mid_price = yes_token.get("price", 0)
                if best_ask is None or best_ask <= 0:
                    valid = False
                    break

                yes_asks.append(Decimal(str(best_ask)))
                yes_mids.append(Decimal(str(mid_price)))
                bracket_trades.append({
                    "token_id": yes_token["token_id"],
                    "best_ask": Decimal(str(best_ask)),
                    "condition_id": bracket.get("condition_id", ""),
                    "label": bracket.get("group_item_title")
                            or bracket.get("question", "")[:40],
                })

            if not valid or not yes_asks:
                continue

            # 3. Validate exhaustiveness — SUM(mid) should be ≈ 1.0
            sum_mid = sum(yes_mids)
            if sum_mid < SUM_MID_MIN or sum_mid > SUM_MID_MAX:
                log.debug(
                    "Multi-arb group %s: SUM(mid)=%.4f outside [%.2f, %.2f] — "
                    "skipping (likely non-exhaustive)",
                    group_id[:12], sum_mid, SUM_MID_MIN, SUM_MID_MAX,
                )
                continue

            # 4. Calculate edge
            sum_ask = sum(yes_asks)
            total_fees = sum_ask * self.taker_fee_rate
            edge = Decimal("1") - sum_ask - total_fees
            edge_cents = edge * Decimal("100")

            if edge_cents < self.min_edge_cents:
                continue

            # 5. Calculate size — each bracket gets the same number of shares
            # Total cost = sum_ask * size, so max_size = max_order / sum_ask
            size = (self.max_order_usdc / sum_ask).quantize(Decimal("0.01"))
            if size <= 0:
                continue

            expected_profit = edge * size

            log.info(
                "🎯 MULTI-ARB: %d brackets, SUM(ask)=%.4f, edge=%.2f¢, "
                "size=%.2f, profit=$%.4f  group=%s",
                len(brackets),
                float(sum_ask),
                float(edge_cents),
                float(size),
                float(expected_profit),
                group_id[:12],
            )

            # 6. Build trades — one BUY per bracket
            trades = [
                Trade(
                    token_id=bt["token_id"],
                    side="BUY",
                    size=size,
                    price=bt["best_ask"],
                    order_type="FOK",  # Fill-or-kill to avoid partial exposure
                )
                for bt in bracket_trades
            ]

            opportunity = Opportunity(
                strategy_type=StrategyType.MULTI_OUTCOME_ARB,
                expected_profit=expected_profit,
                confidence=Decimal("0.95"),
                urgency=8,  # High urgency — arb windows close fast
                metadata={
                    "condition_id": group_id,  # Use group ID as condition_id
                    "type": "multi_outcome_arb",
                    "num_brackets": len(brackets),
                    "sum_ask": float(sum_ask),
                    "sum_mid": float(sum_mid),
                    "edge_cents": float(edge_cents),
                    "total_fees_pct": float(self.taker_fee_rate * 100),
                    "bracket_labels": [bt["label"] for bt in bracket_trades],
                    # Per-bracket Gamma condition_ids for resolution monitoring.
                    "bracket_condition_ids": {bt["token_id"]: bt["condition_id"] for bt in bracket_trades},
                },
            )

            signal = StrategySignal(
                opportunity=opportunity,
                trades=trades,
                max_total_cost=sum_ask * size,
                min_expected_return=size,  # Exactly one bracket pays $1/share
            )
            signals.append(signal)
            self._signal_cooldown[group_id] = now

        return signals

    def validate(self, signal: StrategySignal) -> tuple[bool, str]:
        """Validate a multi-outcome arb signal."""
        meta = signal.opportunity.metadata
        if meta.get("type") != "multi_outcome_arb":
            return False, "not_multi_outcome_arb"

        # All trades must be BUY
        if not all(t.side == "BUY" for t in signal.trades):
            return False, "not_all_buys"

        # All trades must have the same size
        sizes = {t.size for t in signal.trades}
        if len(sizes) != 1:
            return False, "size_mismatch"

        # Sum of asks must still be < $1
        sum_ask = sum(t.price for t in signal.trades)
        if sum_ask >= Decimal("1"):
            return False, "edge_disappeared"

        return True, "ok"
