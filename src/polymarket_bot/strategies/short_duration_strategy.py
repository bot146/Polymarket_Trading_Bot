"""Short-duration momentum strategy for Polymarket 5-min crypto markets.

═══════════════════════════════════════════════════════════════════════
STRATEGY THESIS
═══════════════════════════════════════════════════════════════════════

Polymarket's recurring 5-minute "Up or Down" markets (BTC, ETH, SOL, XRP)
resolve via the Chainlink on-chain oracle after each 5-minute window.
The AMM seeds these at 50/50 odds when created (~24h before the event).
During live trading, order flow shifts the price — but often LAGS the
real-time crypto price action observable via CoinGecko / Binance feeds.

We exploit this lag by:
1. Reading real-time momentum from CoinGecko price data
2. Comparing our estimated P(Up) to the Polymarket market price
3. Placing MAKER orders when our edge exceeds the maker fee (0.5%)

Why maker orders are critical:
- Taker fee on these markets = 10% (crypto_15_min fee type, 1000 bps)
- Maker fee = 0.5% (50 bps) — standard across all Polymarket markets
- At p=0.50: taker break-even = 55%, maker break-even = 50.25%
- Our momentum model targets 52-62% probability → clear edge at maker rates

═══════════════════════════════════════════════════════════════════════
EDGE SOURCES
═══════════════════════════════════════════════════════════════════════

1. MOMENTUM PERSISTENCE (primary)
   Crypto prices exhibit positive short-term autocorrelation (Cont 2001).
   A 5-min window where BTC is already up 0.3% is more likely to close
   up than down.  The AMM's 50/50 seed doesn't account for this.

2. CROSS-ASSET CORRELATION
   BTC leads alt movement by seconds to minutes.  If BTC is trending
   strongly, ETH/SOL/XRP will likely follow — but their markets may
   still be at 50/50.

3. LATE-WINDOW CAPTURE (secondary)
   Within the last ~2 minutes of a 5-min window, the outcome is often
   deterministic (price already well above/below open).  If the market
   price hasn't caught up, we can capture the remaining edge.  Resolution
   delay of ~22 seconds after the window end gives additional buffer.

═══════════════════════════════════════════════════════════════════════
RISK MANAGEMENT
═══════════════════════════════════════════════════════════════════════

- Max position per market: configurable (default $5)
- Confidence threshold: only trade when P(direction) ≥ 53%
- Fee-aware edge: requires edge > maker_fee + min_edge
- Cooldown: 60 seconds per market after signal (avoid overtrading)
- Isolated from other strategies: own StrategyType, own priority band
- Markets auto-resolve in 5 minutes — no stuck positions

═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from polymarket_bot.crypto_price_feed import CryptoPriceFeed, PriceSnapshot
from polymarket_bot.strategy import (
    Opportunity,
    Strategy,
    StrategySignal,
    StrategyType,
    Trade,
)

log = logging.getLogger(__name__)


# ── Market question parsers ──────────────────────────────────────────

# "Bitcoin Up or Down - February 27, 5:15PM-5:20PM ET"
_UP_DOWN_RE = re.compile(
    r"(bitcoin|btc|ethereum|eth|solana|sol|xrp|ripple)\s+up\s+or\s+down",
    re.IGNORECASE,
)

# Map question ticker mentions to canonical short names
_TICKER_MAP: dict[str, str] = {
    "bitcoin": "btc",
    "btc": "btc",
    "ethereum": "eth",
    "eth": "eth",
    "solana": "sol",
    "sol": "sol",
    "xrp": "xrp",
    "ripple": "xrp",
}


def parse_up_down_market(question: str) -> str | None:
    """Extract the canonical ticker from an 'Up or Down' question.

    Returns 'btc', 'eth', 'sol', 'xrp', or None.
    """
    m = _UP_DOWN_RE.search(question)
    if m:
        return _TICKER_MAP.get(m.group(1).lower())
    return None


# ── Configuration ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ShortDurationConfig:
    """Tuning knobs for the short-duration momentum strategy."""

    # Edge / probability thresholds
    min_probability: Decimal = Decimal("0.53")  # Min P(direction) to act
    min_edge_cents: Decimal = Decimal("0.5")    # Min edge in cents after fees

    # Fee modelling (crypto_15_min markets)
    taker_fee_rate: Decimal = Decimal("0.10")   # 10% taker fee
    maker_fee_rate: Decimal = Decimal("0.005")  # 0.5% maker fee
    prefer_maker: bool = True                   # GTC maker orders (not FOK taker)

    # Sizing
    max_order_usdc: Decimal = Decimal("5")      # Conservative — 5-min markets
    min_order_usdc: Decimal = Decimal("2")      # Polymarket minimum

    # Timing
    min_hours_to_resolution: float = 0.0        # Trade even if resolving soon
    max_hours_to_resolution: float = 2.0        # Don't trade >2h out
    cooldown_seconds: float = 60.0              # Per-market cooldown

    # Signals per scan limits
    max_signals_per_scan: int = 4               # Don't spam with 40 signals


class ShortDurationStrategy(Strategy):
    """Momentum-based strategy for 5-min crypto Up/Down markets.

    Scans for short-duration markets, evaluates momentum via the crypto
    price feed, and generates maker-order signals when our estimated
    direction probability exceeds the market-implied probability by more
    than the maker fee.
    """

    def __init__(
        self,
        config: ShortDurationConfig | None = None,
        price_feed: CryptoPriceFeed | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(name="short_duration", enabled=enabled)
        self.config = config or ShortDurationConfig()
        self.feed = price_feed or CryptoPriceFeed()
        self._cooldowns: dict[str, float] = {}  # condition_id → last_signal_ts
        self._last_feed_refresh = 0.0
        self._snapshots: dict[str, PriceSnapshot] = {}

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def scan(self, market_data: dict[str, Any]) -> list[StrategySignal]:
        """Scan short-duration markets for momentum-based edge."""
        signals: list[StrategySignal] = []
        markets = market_data.get("markets", [])
        now = time.time()

        # Refresh crypto price feed (bulk, every 10s to stay within rate limits)
        if now - self._last_feed_refresh > 10.0:
            try:
                self._snapshots = self.feed.get_all_snapshots()
                self._last_feed_refresh = now
            except Exception as e:
                log.debug("Price feed refresh failed: %s", e)

        # Prune old cooldowns
        cutoff = now - self.config.cooldown_seconds * 2
        self._cooldowns = {k: v for k, v in self._cooldowns.items() if v > cutoff}

        for market in markets:
            if len(signals) >= self.config.max_signals_per_scan:
                break

            signal = self._evaluate_market(market, now)
            if signal is not None:
                signals.append(signal)

        return signals

    def validate(self, signal: StrategySignal) -> tuple[bool, str]:
        """Validate a short-duration signal before execution."""
        meta = signal.opportunity.metadata
        if meta.get("strategy_sub_type") != "short_duration_momentum":
            return False, "not_short_duration"

        if len(signal.trades) != 1:
            return False, "expected_single_trade"

        trade = signal.trades[0]
        if trade.side != "BUY":
            return False, "expected_buy"

        # Re-check momentum hasn't flipped
        ticker = meta.get("ticker", "")
        favored = meta.get("favored_direction", "")
        snap = self._snapshots.get(ticker)
        if snap:
            if favored == "Up" and snap.momentum_score < -0.1:
                return False, "momentum_reversed"
            if favored == "Down" and snap.momentum_score > 0.1:
                return False, "momentum_reversed"

        return True, "ok"

    # ------------------------------------------------------------------
    # Core evaluation logic
    # ------------------------------------------------------------------

    def _evaluate_market(
        self, market: dict[str, Any], now: float
    ) -> StrategySignal | None:
        """Evaluate a single market for momentum edge."""
        condition_id = market.get("condition_id", "")
        question = market.get("question", "")

        # 1. Is this a short-duration Up/Down market?
        ticker = parse_up_down_market(question)
        if ticker is None:
            return None

        # 2. Cooldown check
        if now - self._cooldowns.get(condition_id, 0) < self.config.cooldown_seconds:
            return None

        # 3. Must be active
        if not market.get("active", False):
            return None

        # 4. Resolution timing check
        end_date_str = market.get("end_date")
        hours_to_end = self._hours_until(end_date_str)
        if hours_to_end is None:
            return None
        if hours_to_end < self.config.min_hours_to_resolution:
            return None
        if hours_to_end > self.config.max_hours_to_resolution:
            return None

        # 5. Get momentum data
        snap = self._snapshots.get(ticker)
        if snap is None:
            return None

        direction_prob = Decimal(str(snap.direction_probability))

        # 6. Find the favored token
        tokens = market.get("tokens", [])
        if len(tokens) != 2:
            return None

        # Determine direction: momentum > 0 → favor "Up", < 0 → favor "Down"
        if snap.momentum_score > 0:
            favored_direction = "Up"
            favored_prob = direction_prob            # P(Up)
        elif snap.momentum_score < 0:
            favored_direction = "Down"
            favored_prob = Decimal("1") - direction_prob  # P(Down) = 1 - P(Up)
        else:
            return None  # No directional signal

        # 7. Minimum probability gate
        if favored_prob < self.config.min_probability:
            return None

        # 8. Find the favored token and its market price
        favored_token = None
        for t in tokens:
            outcome = t.get("outcome", "").strip()
            if outcome.lower() == favored_direction.lower():
                favored_token = t
                break

        if favored_token is None:
            return None

        # Market-implied probability = token price
        market_price = Decimal(str(
            favored_token.get("best_ask")
            or favored_token.get("price", 0)
        ))
        if market_price <= 0 or market_price >= Decimal("1"):
            return None

        # 9. Edge calculation (maker vs taker)
        if self.config.prefer_maker:
            fee_rate = self.config.maker_fee_rate
            order_type = "GTC"
        else:
            fee_rate = self.config.taker_fee_rate
            order_type = "FOK"

        # Fee = fee_rate * min(price, 1 - price)
        fee_per_share = fee_rate * min(market_price, Decimal("1") - market_price)
        cost_per_share = market_price + fee_per_share

        # Expected value: prob * $1.00 - cost
        ev_per_share = favored_prob - cost_per_share
        edge_cents = ev_per_share * Decimal("100")

        if edge_cents < self.config.min_edge_cents:
            return None

        # 10. Position sizing
        if cost_per_share <= 0:
            return None
        size = (self.config.max_order_usdc / cost_per_share).quantize(Decimal("0.01"))
        if size < self.config.min_order_usdc / cost_per_share:
            return None
        if size <= 0:
            return None

        expected_profit = ev_per_share * size

        # 11. Confidence mapping
        # Higher momentum consistency → higher confidence
        confidence = self._compute_confidence(snap, favored_prob)

        # 12. Urgency: closer to resolution → more urgent
        urgency = self._compute_urgency(hours_to_end)

        # For maker orders, place limit at the current best ask (or slightly below)
        # to get queue priority. For taker, just hit the ask.
        limit_price = market_price
        if self.config.prefer_maker and market_price > Decimal("0.01"):
            # Place limit 1 tick below ask to get maker rebate
            limit_price = market_price - Decimal("0.01")

        log.info(
            "⚡ SHORT-DUR: %s %s cid=%s price=%.3f prob=%.1f%% edge=%.1f¢ "
            "mom=%.3f trend=%.2f vol=%.4f size=%.1f type=%s  q=%s",
            ticker.upper(),
            favored_direction,
            condition_id[:12],
            float(market_price),
            float(favored_prob * 100),
            float(edge_cents),
            snap.momentum_score,
            snap.trend_strength,
            snap.volatility_1h,
            float(size),
            order_type,
            question[:50],
        )

        opportunity = Opportunity(
            strategy_type=StrategyType.SHORT_DURATION,
            expected_profit=expected_profit,
            confidence=confidence,
            urgency=urgency,
            metadata={
                "condition_id": condition_id,
                "type": "short_duration",
                "strategy_sub_type": "short_duration_momentum",
                "ticker": ticker,
                "favored_direction": favored_direction,
                "direction_probability": float(favored_prob),
                "market_price": float(market_price),
                "edge_cents": float(edge_cents),
                "momentum_score": snap.momentum_score,
                "trend_strength": snap.trend_strength,
                "volatility_1h": snap.volatility_1h,
                "hours_to_resolution": hours_to_end,
                "fee_type": order_type.lower(),
                "question": question[:80],
                "end_date": end_date_str,
                "ret_5m": snap.ret_5m,
                "ret_15m": snap.ret_15m,
                "ret_1h": snap.ret_1h,
            },
        )

        trades = [
            Trade(
                token_id=favored_token.get("token_id", ""),
                side="BUY",
                size=size,
                price=limit_price,
                order_type=order_type,
            ),
        ]

        signal = StrategySignal(
            opportunity=opportunity,
            trades=trades,
            max_total_cost=cost_per_share * size,
            min_expected_return=size,  # $1/share on win
        )

        self._cooldowns[condition_id] = now
        return signal

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hours_until(end_date_str: str | None) -> float | None:
        """Parse ISO date string and return hours until resolution."""
        if not end_date_str:
            return None
        try:
            s = end_date_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _compute_confidence(snap: PriceSnapshot, prob: Decimal) -> Decimal:
        """Map momentum analytics to a [0, 1] confidence score.

        Confidence reflects how trustworthy our probability estimate is:
        - trend_strength near 1.0 (all timeframes agree) → high confidence
        - momentum_score far from 0 → high confidence
        - Low volatility → more predictable → higher confidence
        """
        # Base: the probability itself scaled to [0.5, 1.0]
        base = float(prob)

        # Trend consistency bonus (+0-10%)
        trend_bonus = snap.trend_strength * 0.10

        # Momentum magnitude bonus (+0-5%)
        mom_bonus = min(abs(snap.momentum_score), 1.0) * 0.05

        # Volatility penalty (high vol → lower confidence)
        vol_penalty = min(snap.volatility_1h * 10, 0.10)

        raw = base + trend_bonus + mom_bonus - vol_penalty
        return Decimal(str(max(0.50, min(0.95, raw))))

    @staticmethod
    def _compute_urgency(hours_to_end: float) -> int:
        """Closer to resolution → higher urgency (0-10 scale).

        < 10 min  → 9
        < 30 min  → 7
        < 1 hour  → 5
        < 2 hours → 3
        """
        if hours_to_end < 10 / 60:
            return 9
        if hours_to_end < 0.5:
            return 7
        if hours_to_end < 1.0:
            return 5
        return 3
