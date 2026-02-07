"""Statistical arbitrage strategy for correlated markets.

Identifies markets whose YES prices tend to move together and trades when
the z-score of the spread exceeds a threshold.

v2 changes (from review):
- Real rolling correlation and z-score instead of hardcoded keyword matching.
- Maker fee deduction from expected profit.
- Condition-aware position metadata.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
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


# ---------------------------------------------------------------------------
# Rolling stats helpers
# ---------------------------------------------------------------------------

@dataclass
class _RollingWindow:
    """Fixed-size rolling window of floats for mean/std/correlation."""
    maxlen: int
    _values: deque[float] = field(default_factory=lambda: deque())

    def __post_init__(self) -> None:
        self._values = deque(maxlen=self.maxlen)

    def push(self, v: float) -> None:
        self._values.append(v)

    def full(self) -> bool:
        return len(self._values) >= self.maxlen

    @property
    def n(self) -> int:
        return len(self._values)

    def mean(self) -> float:
        if not self._values:
            return 0.0
        return sum(self._values) / len(self._values)

    def std(self) -> float:
        if len(self._values) < 2:
            return 0.0
        m = self.mean()
        var = sum((x - m) ** 2 for x in self._values) / (len(self._values) - 1)
        return math.sqrt(var)


def _pearson(xs: deque[float], ys: deque[float]) -> float:
    """Pearson correlation of two equal-length deques."""
    n = min(len(xs), len(ys))
    if n < 5:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx == 0 or sy == 0:
        return 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sx * sy)


# ---------------------------------------------------------------------------
# Pair tracker
# ---------------------------------------------------------------------------

@dataclass
class _PairState:
    """Tracks rolling spread statistics for a market pair."""
    condition_a: str
    condition_b: str
    prices_a: _RollingWindow
    prices_b: _RollingWindow
    spread_window: _RollingWindow

    def push(self, price_a: float, price_b: float) -> None:
        self.prices_a.push(price_a)
        self.prices_b.push(price_b)
        self.spread_window.push(price_a - price_b)

    def ready(self) -> bool:
        return self.spread_window.full()

    def correlation(self) -> float:
        return _pearson(self.prices_a._values, self.prices_b._values)

    def z_score(self) -> float:
        std = self.spread_window.std()
        if std < 1e-9:
            return 0.0
        current_spread = (
            self.prices_a._values[-1] - self.prices_b._values[-1]
            if self.prices_a._values and self.prices_b._values
            else 0.0
        )
        return (current_spread - self.spread_window.mean()) / std


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class StatisticalArbitrageStrategy(Strategy):
    """Trade mean-reversion of the spread between correlated markets.

    How it works:
    1. Every scan cycle, record YES prices for every active market.
    2. For every pair of markets that has accumulated enough history
       (``lookback`` ticks), compute the rolling spread z-score.
    3. If the z-score exceeds ``z_entry`` and the pair correlation is
       above ``min_correlation``, emit a signal: BUY the cheap side,
       SELL the expensive side.
    4. Expected profit is computed net of maker fees.

    Parameters:
        lookback: Number of price observations for rolling window.
        z_entry: Minimum |z-score| to trigger a signal.
        min_correlation: Minimum Pearson correlation to consider a pair.
        max_order_usdc: Maximum order size per leg.
        maker_fee_rate: Maker fee as a decimal (0.005 = 0.5%).
    """

    def __init__(
        self,
        name: str = "statistical_arbitrage",
        lookback: int = 30,
        z_entry: float = 2.0,
        min_correlation: float = 0.60,
        max_order_usdc: Decimal = Decimal("30"),
        maker_fee_rate: Decimal = Decimal("0.005"),
        enabled: bool = True,
    ):
        super().__init__(name=name, enabled=enabled)
        self.lookback = max(10, lookback)
        self.z_entry = z_entry
        self.min_correlation = min_correlation
        self.max_order_usdc = max_order_usdc
        self.maker_fee_rate = maker_fee_rate

        # pair key → _PairState
        self._pairs: dict[str, _PairState] = {}
        # condition_id → latest YES price
        self._last_prices: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def scan(self, market_data: dict[str, Any]) -> list[StrategySignal]:
        signals: list[StrategySignal] = []
        markets = market_data.get("markets", [])

        # 1. Snapshot current YES prices.
        current_prices: dict[str, float] = {}
        token_map: dict[str, dict] = {}  # condition_id → first YES token dict
        market_map: dict[str, dict] = {}  # condition_id → market dict

        for market in markets:
            cid = market.get("condition_id")
            if not cid:
                continue
            for token in market.get("tokens", []):
                if token.get("outcome", "").upper() == "YES":
                    price = token.get("price")
                    if price is not None:
                        current_prices[cid] = float(price)
                        token_map[cid] = token
                        market_map[cid] = market
                    break

        if len(current_prices) < 2:
            return signals

        # 2. Update pair windows.  We only track pairs of markets that have
        #    appeared together at least once (lazy initialisation).
        cids = sorted(current_prices.keys())
        for i, cid_a in enumerate(cids):
            for cid_b in cids[i + 1:]:
                key = f"{cid_a}||{cid_b}"
                if key not in self._pairs:
                    self._pairs[key] = _PairState(
                        condition_a=cid_a,
                        condition_b=cid_b,
                        prices_a=_RollingWindow(maxlen=self.lookback),
                        prices_b=_RollingWindow(maxlen=self.lookback),
                        spread_window=_RollingWindow(maxlen=self.lookback),
                    )
                self._pairs[key].push(current_prices[cid_a], current_prices[cid_b])

        # 3. Scan for signals.
        for key, pair in self._pairs.items():
            if not pair.ready():
                continue

            corr = pair.correlation()
            if abs(corr) < self.min_correlation:
                continue

            z = pair.z_score()
            if abs(z) < self.z_entry:
                continue

            # Determine which side is cheap / expensive.
            cid_a = pair.condition_a
            cid_b = pair.condition_b

            price_a = current_prices.get(cid_a)
            price_b = current_prices.get(cid_b)
            if price_a is None or price_b is None:
                continue

            token_a = token_map.get(cid_a)
            token_b = token_map.get(cid_b)
            if token_a is None or token_b is None:
                continue

            # Positive z ⇒ A is expensive relative to B → short A, long B.
            if z > 0:
                expensive_cid, cheap_cid = cid_a, cid_b
                expensive_token, cheap_token = token_a, token_b
                expensive_price, cheap_price = Decimal(str(price_a)), Decimal(str(price_b))
            else:
                expensive_cid, cheap_cid = cid_b, cid_a
                expensive_token, cheap_token = token_b, token_a
                expensive_price, cheap_price = Decimal(str(price_b)), Decimal(str(price_a))

            # Position sizing (equal USDC per leg).
            size_exp = (self.max_order_usdc / expensive_price).quantize(Decimal("0.01")) if expensive_price > 0 else Decimal("0")
            size_chp = (self.max_order_usdc / cheap_price).quantize(Decimal("0.01")) if cheap_price > 0 else Decimal("0")
            if size_exp <= 0 or size_chp <= 0:
                continue

            # Expected profit = spread * min_size - fees on both legs.
            divergence = abs(expensive_price - cheap_price)
            min_size = min(size_exp, size_chp)
            fee_cost = (expensive_price * self.maker_fee_rate + cheap_price * self.maker_fee_rate) * min_size
            expected_profit = divergence * min_size - fee_cost
            if expected_profit <= 0:
                continue

            expensive_token_id = expensive_token.get("token_id")
            cheap_token_id = cheap_token.get("token_id")
            if not expensive_token_id or not cheap_token_id:
                continue

            opportunity = Opportunity(
                strategy_type=StrategyType.STATISTICAL_ARBITRAGE,
                expected_profit=expected_profit,
                confidence=Decimal(str(round(min(abs(corr), 0.95), 2))),
                urgency=6,
                metadata={
                    "condition_id": expensive_cid,  # primary for position tracking
                    "pair_key": key,
                    "z_score": round(z, 3),
                    "correlation": round(corr, 3),
                    "expensive_condition": expensive_cid,
                    "cheap_condition": cheap_cid,
                    "expensive_price": float(expensive_price),
                    "cheap_price": float(cheap_price),
                    "divergence": float(divergence),
                    "fee_cost": float(fee_cost),
                    "lookback": self.lookback,
                },
            )

            trades = [
                Trade(
                    token_id=expensive_token_id,
                    side="SELL",
                    size=size_exp,
                    price=expensive_price,
                    order_type="GTC",
                ),
                Trade(
                    token_id=cheap_token_id,
                    side="BUY",
                    size=size_chp,
                    price=cheap_price,
                    order_type="GTC",
                ),
            ]

            signal = StrategySignal(
                opportunity=opportunity,
                trades=trades,
                max_total_cost=cheap_price * size_chp + expensive_price * size_exp,
                min_expected_return=Decimal("0"),
            )

            log.info(
                "📈 Stat-arb signal: z=%.2f corr=%.2f spread=%.4f "
                "short %s@%.4f long %s@%.4f profit=$%.4f",
                z, corr, float(divergence),
                expensive_cid[:8], float(expensive_price),
                cheap_cid[:8], float(cheap_price),
                float(expected_profit),
            )

            signals.append(signal)

        return signals

    def validate(self, signal: StrategySignal) -> tuple[bool, str]:
        if signal.opportunity.strategy_type != StrategyType.STATISTICAL_ARBITRAGE:
            return False, "not_stat_arb_strategy"
        if len(signal.trades) != 2:
            return False, "invalid_trade_count"
        sides = [t.side for t in signal.trades]
        if "BUY" not in sides or "SELL" not in sides:
            return False, "must_have_long_and_short"
        z = signal.opportunity.metadata.get("z_score", 0)
        if abs(z) < self.z_entry:
            return False, "z_score_too_small"
        return True, "ok"
