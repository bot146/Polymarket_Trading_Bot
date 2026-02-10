"""Value betting strategy — the real edge.

This is how quantitative prediction market funds actually make money.
It's NOT arbitrage (those opportunities are competed away in microseconds).
It's VALUE BETTING: identify markets where the Polymarket crowd price is
WRONG relative to your model's fair probability estimate.

Key insight from quantitative finance applied to prediction markets:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The crowd is efficient ON AVERAGE but makes systematic errors:
1. **Favorite–longshot bias**: Heavy favorites (>80%) are overpriced,
   longshots (<20%) are overpriced. The value is in the middle.
2. **Recency bias**: Prices overshoot after breaking news, then
   mean-revert as the crowd calms down.
3. **Round number anchoring**: Prices cluster around 0.50, 0.25, 0.75 etc.
4. **Neglect of base rates**: Crowd ignores historical base rates
   (e.g., incumbents win X% of elections, Fed hikes Y% of the time).
5. **Low-liquidity mispricing**: Thin markets have wider mispricings.
6. **Deadline drift**: As expiry approaches, prices mechanically converge
   to 0 or 1 — but the transition can be too slow/fast.

This strategy implements multiple "edge signals" that each detect a
different systematic error. When multiple signals agree (ensemble),
the expected edge multiplies and the bet is placed.

Inspired by:
- Renaissance Technologies' "basket of weak predictors" approach
- Pinnacle Sports closing line value methodology
- Nate Silver's FiveThirtyEight model calibration research
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import logging
import math
import time
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
# Edge signal helpers
# ---------------------------------------------------------------------------

@dataclass
class EdgeSignal:
    """A single edge signal from one sub-model."""
    name: str
    fair_probability: float  # Our estimate of the true probability
    confidence: float  # 0-1 how confident we are in this signal
    edge_bps: float  # Edge in basis points (fair_prob - market_price)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MarketHistory:
    """Rolling price history for a single token."""
    token_id: str
    condition_id: str
    outcome: str
    prices: deque  # deque of (timestamp, price) tuples
    volumes: deque  # deque of (timestamp, volume) tuples
    max_len: int = 120  # Keep ~4 minutes at 2-second intervals

    def __post_init__(self):
        if not isinstance(self.prices, deque):
            self.prices = deque(maxlen=self.max_len)
        if not isinstance(self.volumes, deque):
            self.volumes = deque(maxlen=self.max_len)

    def add(self, price: float, volume: float, ts: float) -> None:
        self.prices.append((ts, price))
        self.volumes.append((ts, volume))

    @property
    def n(self) -> int:
        return len(self.prices)

    @property
    def latest_price(self) -> float | None:
        return self.prices[-1][1] if self.prices else None

    def price_series(self) -> list[float]:
        return [p for _, p in self.prices]

    def mean_price(self, lookback: int | None = None) -> float:
        series = self.price_series()
        if lookback:
            series = series[-lookback:]
        return sum(series) / len(series) if series else 0.0

    def std_price(self, lookback: int | None = None) -> float:
        series = self.price_series()
        if lookback:
            series = series[-lookback:]
        if len(series) < 2:
            return 0.0
        m = sum(series) / len(series)
        return math.sqrt(sum((x - m) ** 2 for x in series) / (len(series) - 1))

    def momentum(self, short: int = 5, long: int = 30) -> float:
        """Short-term vs long-term mean — momentum signal."""
        series = self.price_series()
        if len(series) < long:
            return 0.0
        short_mean = sum(series[-short:]) / short
        long_mean = sum(series[-long:]) / long
        if long_mean == 0:
            return 0.0
        return (short_mean - long_mean) / long_mean

    def volatility(self, lookback: int = 20) -> float:
        """Annualized volatility from price returns."""
        series = self.price_series()
        if len(series) < lookback + 1:
            return 0.0
        returns = []
        for i in range(-lookback, 0):
            if series[i - 1] > 0:
                returns.append(series[i] / series[i - 1] - 1)
        if len(returns) < 2:
            return 0.0
        m = sum(returns) / len(returns)
        var = sum((r - m) ** 2 for r in returns) / (len(returns) - 1)
        return math.sqrt(var)

    def price_velocity(self, lookback: int = 10) -> float:
        """Price change per observation over lookback."""
        series = self.price_series()
        if len(series) < lookback:
            return 0.0
        return (series[-1] - series[-lookback]) / lookback


# ---------------------------------------------------------------------------
# Sub-model: Favorite-Longshot Bias Detector
# ---------------------------------------------------------------------------

def favorite_longshot_edge(price: float) -> EdgeSignal | None:
    """Detect favorite-longshot bias.

    Research shows prediction market prices are systematically biased:
    - Outcomes priced 0.80-0.95 win LESS than the price implies
    - Outcomes priced 0.05-0.20 win LESS than the price implies
    - Outcomes priced 0.30-0.70 are most accurately priced

    The "Kelly zone" is 0.25-0.75 where the crowd is best calibrated.
    Outside that, we can fade the bias.

    Source: Snowberg & Wolfers (2010), Ottaviani & Sørensen (2008)
    """
    if price < 0.01 or price > 0.99:
        return None

    # Calibration correction curve (empirically derived)
    # Maps market_price -> our fair_probability estimate
    # Heavy favorites: overpriced by ~3-5%
    # Heavy longshots: overpriced by ~3-8%
    # Middle range: roughly correct

    correction = 0.0
    if price >= 0.90:
        # Heavy favorite — overshoot
        correction = -0.03 * ((price - 0.85) / 0.15) ** 1.5
    elif price >= 0.80:
        # Moderate favorite — slight overshoot
        correction = -0.01 * ((price - 0.75) / 0.25)
    elif price <= 0.10:
        # Heavy longshot — overpriced
        correction = -0.04 * ((0.15 - price) / 0.15) ** 1.5
    elif price <= 0.20:
        # Moderate longshot — slight overshoot
        correction = -0.01 * ((0.25 - price) / 0.25)
    else:
        # Kelly zone — crowd is well-calibrated, no edge
        return None

    if abs(correction) < 0.005:
        return None

    fair_prob = max(0.01, min(0.99, price + correction))
    edge_bps = (fair_prob - price) * 10000

    return EdgeSignal(
        name="favorite_longshot_bias",
        fair_probability=fair_prob,
        confidence=0.55,  # Low confidence — this is a weak but persistent signal
        edge_bps=edge_bps,
        metadata={"correction": correction, "zone": "heavy_fav" if price > 0.80 else "heavy_long"},
    )


# ---------------------------------------------------------------------------
# Sub-model: Mean Reversion Detector
# ---------------------------------------------------------------------------

def mean_reversion_edge(history: MarketHistory) -> EdgeSignal | None:
    """Detect mean reversion after sharp moves.

    When price moves sharply in one direction, it tends to partially
    revert. This is well-documented in prediction markets:
    - Sharp moves on rumors partially revert as facts emerge
    - Overreaction to breaking news

    We use Bollinger Band-style z-score to detect stretched prices.
    """
    if history.n < 20:
        return None

    price = history.latest_price
    if price is None or price <= 0:
        return None

    mean = history.mean_price(lookback=30)
    std = history.std_price(lookback=30)

    if std < 0.005:
        return None  # Too stable, no edge

    z_score = (price - mean) / std

    if abs(z_score) < 1.5:
        return None  # Not stretched enough

    # Predict partial reversion toward the mean
    reversion_pct = 0.3  # Expect 30% reversion
    fair_prob = price + reversion_pct * (mean - price)
    fair_prob = max(0.01, min(0.99, fair_prob))

    edge_bps = (fair_prob - price) * 10000

    if abs(edge_bps) < 50:
        return None

    return EdgeSignal(
        name="mean_reversion",
        fair_probability=fair_prob,
        confidence=min(0.70, 0.40 + abs(z_score) * 0.10),
        edge_bps=edge_bps,
        metadata={
            "z_score": round(z_score, 3),
            "mean": round(mean, 4),
            "std": round(std, 4),
            "reversion_target": round(fair_prob, 4),
        },
    )


# ---------------------------------------------------------------------------
# Sub-model: Momentum / Trend Following
# ---------------------------------------------------------------------------

def momentum_edge(history: MarketHistory) -> EdgeSignal | None:
    """Detect momentum — continuation of recent trends.

    Prediction market prices have positive short-term autocorrelation:
    - News breaks → price moves → more traders pile in → more movement
    - This effect lasts ~2-10 minutes before mean reversion kicks in

    We look for strong short-term momentum (5 ticks vs 30 ticks).
    """
    if history.n < 30:
        return None

    price = history.latest_price
    if price is None or price <= 0 or price >= 1:
        return None

    mom = history.momentum(short=5, long=30)
    vel = history.price_velocity(lookback=5)

    # Need meaningful momentum
    if abs(mom) < 0.005:
        return None

    # Only trade momentum in the middle range where there's room to run
    if price < 0.10 or price > 0.90:
        return None

    # Momentum continuation estimate: price will move further in same direction
    continuation = mom * 0.4  # Expect 40% continuation of observed momentum
    fair_prob = price * (1 + continuation)
    fair_prob = max(0.01, min(0.99, fair_prob))

    edge_bps = (fair_prob - price) * 10000

    if abs(edge_bps) < 30:
        return None

    return EdgeSignal(
        name="momentum",
        fair_probability=fair_prob,
        confidence=min(0.60, 0.35 + abs(mom) * 5),
        edge_bps=edge_bps,
        metadata={
            "momentum": round(mom, 5),
            "velocity": round(vel, 6),
            "direction": "up" if mom > 0 else "down",
        },
    )


# ---------------------------------------------------------------------------
# Sub-model: Volume-Price Divergence
# ---------------------------------------------------------------------------

def volume_divergence_edge(history: MarketHistory, market_volume: float) -> EdgeSignal | None:
    """Detect when volume and price diverge.

    Smart money often trades quietly — if volume spikes but price doesn't
    move (absorption), it suggests the current price is about to break.

    Conversely, if price moves on tiny volume, the move is fragile and
    likely to reverse.
    """
    if history.n < 15:
        return None

    price = history.latest_price
    if price is None or price <= 0 or price >= 1:
        return None

    # Simple heuristic: compare price change magnitude to volume
    price_change = abs(history.price_velocity(lookback=10))
    vol = history.volatility(lookback=10)

    if vol < 0.001:
        return None

    # High volume, low price change → absorption, expect breakout
    # Low volume, high price change → fragile move, expect reversal
    # For now, use a simplified version based on market volume vs typical

    # Low-volume markets are more likely mispriced
    if market_volume < 50000:
        liquidity_premium = 0.01  # 1% extra edge for illiquid markets
    elif market_volume < 200000:
        liquidity_premium = 0.005
    else:
        liquidity_premium = 0.0

    if liquidity_premium <= 0:
        return None

    # In low-liquidity markets, prices tend toward the mid (0.50) more than
    # the market suggests — the bid/ask spread itself creates bias
    if price > 0.50:
        fair_prob = price - liquidity_premium
    else:
        fair_prob = price + liquidity_premium

    fair_prob = max(0.01, min(0.99, fair_prob))
    edge_bps = (fair_prob - price) * 10000

    if abs(edge_bps) < 20:
        return None

    return EdgeSignal(
        name="volume_divergence",
        fair_probability=fair_prob,
        confidence=0.45,
        edge_bps=edge_bps,
        metadata={
            "market_volume": market_volume,
            "liquidity_premium": round(liquidity_premium, 4),
        },
    )


# ---------------------------------------------------------------------------
# Sub-model: Deadline Convergence
# ---------------------------------------------------------------------------

def round_number_edge(price: float) -> EdgeSignal | None:
    """Detect clustering around round numbers (anchoring bias).

    Prices systematically cluster at 0.25, 0.50, 0.75 etc. because:
    - Humans think in percentages: "50/50", "75% likely"
    - This creates artificial liquidity at these levels
    - True probabilities are rarely exactly round numbers

    The edge: if price is very close to a round number but the underlying
    distribution is continuous, the true probability is slightly off.
    """
    if price < 0.03 or price > 0.97:
        return None

    # Round numbers that act as "attractors"
    attractors = [0.10, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.70, 0.75, 0.80, 0.90]

    closest = min(attractors, key=lambda a: abs(price - a))
    distance = abs(price - closest)

    # Only trigger if very close to a round number (within 1.5%)
    if distance > 0.015:
        return None

    # The "pull" of the round number means the true probability is slightly
    # further from it than the market shows. Push away from the attractor.
    push_away = 0.008  # 0.8% away from the round number
    if price > closest:
        fair_prob = price + push_away
    elif price < closest:
        fair_prob = price - push_away
    else:
        # Exactly on the attractor — could go either way, skip
        return None

    fair_prob = max(0.01, min(0.99, fair_prob))
    edge_bps = (fair_prob - price) * 10000

    if abs(edge_bps) < 15:
        return None

    return EdgeSignal(
        name="round_number_anchoring",
        fair_probability=fair_prob,
        confidence=0.40,
        edge_bps=edge_bps,
        metadata={"closest_attractor": closest, "distance": round(distance, 4)},
    )


# ---------------------------------------------------------------------------
# Ensemble combiner
# ---------------------------------------------------------------------------

def ensemble_fair_probability(signals: list[EdgeSignal]) -> tuple[float, float]:
    """Combine multiple weak edge signals into one strong signal.

    Uses confidence-weighted average — inspired by the "wisdom of crowds"
    but applied to our own sub-models.

    Returns:
        (combined_fair_probability, combined_confidence)
    """
    if not signals:
        return 0.5, 0.0

    total_weight = 0.0
    weighted_sum = 0.0

    for sig in signals:
        weight = sig.confidence ** 2  # Square confidence to emphasize strong signals
        weighted_sum += sig.fair_probability * weight
        total_weight += weight

    if total_weight == 0:
        return 0.5, 0.0

    combined_prob = weighted_sum / total_weight

    # Combined confidence increases with agreement between signals
    probs = [s.fair_probability for s in signals]
    agreement = 1.0 - (max(probs) - min(probs)) if len(probs) > 1 else 0.5
    avg_confidence = sum(s.confidence for s in signals) / len(signals)

    # More signals that agree → higher confidence
    n_bonus = min(0.15, len(signals) * 0.05)
    combined_confidence = min(0.85, avg_confidence * agreement + n_bonus)

    return combined_prob, combined_confidence


# ---------------------------------------------------------------------------
# Main Strategy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValueBettingConfig:
    # Minimum combined edge (in basis points) to place a bet
    min_edge_bps: Decimal = Decimal("50")

    # Minimum number of agreeing sub-signals to trade
    min_agreeing_signals: int = 1

    # Maximum price — don't buy above this (leaves room for profit)
    max_entry_price: Decimal = Decimal("0.95")

    # Minimum price — don't buy below (too speculative)
    min_entry_price: Decimal = Decimal("0.05")

    # Max order size per signal
    max_order_usdc: Decimal = Decimal("10")

    # Maker fee
    maker_fee_rate: Decimal = Decimal("0.005")

    # Max signals per scan to avoid spam
    max_signals_per_scan: int = 8

    # Kelly fraction — what fraction of Kelly optimal to bet
    # Full Kelly is too aggressive, 1/4 Kelly is standard for HFT
    kelly_fraction: Decimal = Decimal("0.25")


class ValueBettingStrategy(Strategy):
    """Ensemble value-betting strategy.

    Combines multiple weak edge signals (favorite-longshot bias,
    mean reversion, momentum, volume divergence, round number anchoring)
    into a single bet when enough signals agree.

    This is the approach that actually makes money in prediction markets:
    not arbitrage (too competitive), but DIRECTIONAL BETS where the crowd
    is systematically wrong.
    """

    def __init__(
        self,
        config: ValueBettingConfig | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(name=StrategyType.AI_PROBABILITY.value, enabled=enabled)
        self.config = config or ValueBettingConfig()

        # Rolling price history per token
        self._histories: dict[str, MarketHistory] = {}

    def scan(self, market_data: dict[str, Any]) -> list[StrategySignal]:
        signals: list[StrategySignal] = []
        markets = market_data.get("markets", [])
        now = time.time()

        for market in markets:
            condition_id = str(market.get("condition_id") or "")
            question = str(market.get("question") or "")
            market_volume = float(market.get("volume", 0))

            for token in market.get("tokens", []) or []:
                if len(signals) >= self.config.max_signals_per_scan:
                    return signals

                token_id = str(token.get("token_id") or "")
                outcome = str(token.get("outcome") or "")
                price = token.get("best_ask") or token.get("price")

                if price is None:
                    continue

                price = float(price)
                volume = float(token.get("volume", 0))

                if price < float(self.config.min_entry_price) or price > float(self.config.max_entry_price):
                    continue

                # Update rolling history
                key = token_id
                if key not in self._histories:
                    self._histories[key] = MarketHistory(
                        token_id=token_id,
                        condition_id=condition_id,
                        outcome=outcome,
                        prices=deque(maxlen=120),
                        volumes=deque(maxlen=120),
                    )
                self._histories[key].add(price, volume, now)
                history = self._histories[key]

                # Collect edge signals from all sub-models
                edge_signals: list[EdgeSignal] = []

                # 1. Favorite-longshot bias
                fl_sig = favorite_longshot_edge(price)
                if fl_sig:
                    edge_signals.append(fl_sig)

                # 2. Mean reversion
                mr_sig = mean_reversion_edge(history)
                if mr_sig:
                    edge_signals.append(mr_sig)

                # 3. Momentum
                mom_sig = momentum_edge(history)
                if mom_sig:
                    edge_signals.append(mom_sig)

                # 4. Volume divergence
                vd_sig = volume_divergence_edge(history, market_volume)
                if vd_sig:
                    edge_signals.append(vd_sig)

                # 5. Round number anchoring
                rn_sig = round_number_edge(price)
                if rn_sig:
                    edge_signals.append(rn_sig)

                if len(edge_signals) < self.config.min_agreeing_signals:
                    continue

                # Check signal agreement — majority must agree on direction
                buy_signals = [s for s in edge_signals if s.edge_bps > 0]
                sell_signals = [s for s in edge_signals if s.edge_bps < 0]

                if len(buy_signals) > len(sell_signals):
                    agreeing = buy_signals
                    direction = "BUY"
                elif len(sell_signals) > len(buy_signals):
                    agreeing = sell_signals
                    direction = "SELL"
                else:
                    continue  # No majority, skip

                # For now we only support BUY (easier execution on Polymarket)
                if direction != "BUY":
                    continue

                if len(agreeing) < self.config.min_agreeing_signals:
                    continue

                # Combine signals
                fair_prob, combined_confidence = ensemble_fair_probability(agreeing)

                edge = fair_prob - price
                edge_bps = edge * 10000

                if edge_bps < float(self.config.min_edge_bps):
                    continue

                # Kelly criterion position sizing
                # f* = (p * b - q) / b where b = odds, p = win_prob, q = 1-p
                # For binary outcomes, b = (1/price) - 1
                if price <= 0 or price >= 1:
                    continue
                b = (1.0 / price) - 1.0
                q = 1.0 - fair_prob
                kelly_full = (fair_prob * b - q) / b if b > 0 else 0
                kelly_bet = kelly_full * float(self.config.kelly_fraction)

                if kelly_bet <= 0:
                    continue

                # Size = kelly fraction * max_order, capped
                raw_size = Decimal(str(kelly_bet)) * self.config.max_order_usdc
                price_dec = Decimal(str(price))
                size = min(raw_size / price_dec, self.config.max_order_usdc / price_dec).quantize(Decimal("0.01"))

                if size <= 0:
                    continue

                # Fee-adjusted expected profit
                fee = price_dec * size * self.config.maker_fee_rate
                expected_profit = Decimal(str(edge)) * size - fee

                if expected_profit <= 0:
                    continue

                opportunity = Opportunity(
                    strategy_type=StrategyType.AI_PROBABILITY,
                    expected_profit=expected_profit,
                    confidence=Decimal(str(round(combined_confidence, 3))),
                    urgency=5,
                    metadata={
                        "condition_id": condition_id,
                        "question": question,
                        "token_id": token_id,
                        "outcome": outcome,
                        "market_price": price,
                        "fair_probability": round(fair_prob, 5),
                        "edge_bps": round(edge_bps, 1),
                        "kelly_fraction": round(kelly_bet, 4),
                        "n_signals": len(agreeing),
                        "signal_names": [s.name for s in agreeing],
                        "sub_signals": [
                            {"name": s.name, "fair_prob": round(s.fair_probability, 4),
                             "confidence": round(s.confidence, 3), "edge_bps": round(s.edge_bps, 1)}
                            for s in agreeing
                        ],
                        "history_ticks": history.n,
                    },
                )

                trade = Trade(
                    token_id=token_id,
                    side="BUY",
                    size=size,
                    price=price_dec,
                    order_type="GTC",
                )

                signal = StrategySignal(
                    opportunity=opportunity,
                    trades=[trade],
                    max_total_cost=price_dec * size,
                    min_expected_return=Decimal("0"),
                )

                log.info(
                    "🧠 VALUE BET: %s %s@%.4f → fair=%.4f edge=%+.1fbps "
                    "kelly=%.3f signals=[%s] condition=%s",
                    direction, outcome, price, fair_prob, edge_bps,
                    kelly_bet, ",".join(s.name for s in agreeing),
                    condition_id[:8],
                )

                signals.append(signal)

        return signals

    def validate(self, signal: StrategySignal) -> tuple[bool, str]:
        if signal.opportunity.strategy_type != StrategyType.AI_PROBABILITY:
            return False, "not_value_betting"

        if len(signal.trades) != 1:
            return False, "unexpected_trade_count"

        trade = signal.trades[0]
        if trade.side != "BUY":
            return False, "only_buy_supported"

        if trade.price < self.config.min_entry_price or trade.price > self.config.max_entry_price:
            return False, "price_out_of_range"

        if trade.size <= 0:
            return False, "invalid_size"

        if signal.max_total_cost > self.config.max_order_usdc:
            return False, "exceeds_max_order"

        # Verify edge is still meaningful
        edge_bps = signal.opportunity.metadata.get("edge_bps", 0)
        if edge_bps < float(self.config.min_edge_bps) * 0.5:
            return False, "edge_too_small"

        return True, "ok"
