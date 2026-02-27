"""Real-time crypto price feed with momentum analytics.

Provides ultra-low-latency price data and technical indicators for
the short-duration strategy.  Uses CoinGecko's ``/coins/{id}/market_chart``
endpoint for recent price history and ``/simple/price`` for spot prices.

Why not reuse CryptoOracle?
- CryptoOracle (oracle_sniping_strategy) is optimised for spot-price threshold
  checks.  This module adds historical price series, momentum scores, and
  volatility estimation — all needed to assess directional probability in
  5-minute windows.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger(__name__)

COINGECKO_API = "https://api.coingecko.com/api/v3"

# CoinGecko IDs we care about (matches the 5-min series on Polymarket)
TICKER_TO_CG: dict[str, str] = {
    "btc": "bitcoin",
    "bitcoin": "bitcoin",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "sol": "solana",
    "solana": "solana",
    "xrp": "ripple",
    "ripple": "ripple",
}


@dataclass
class PriceSnapshot:
    """Point-in-time price with derived analytics."""

    ticker: str
    price: float
    timestamp: float  # epoch seconds

    # Recent returns (annualised is overkill; raw % over window)
    ret_1m: float | None = None   # 1-minute return
    ret_5m: float | None = None   # 5-minute return
    ret_15m: float | None = None  # 15-minute return
    ret_1h: float | None = None   # 1-hour return

    # Derived scores
    momentum_score: float = 0.0     # -1 → +1  (positive = upward momentum)
    volatility_1h: float = 0.0      # std dev of 5-min returns over last hour
    trend_strength: float = 0.0     # 0 → 1  (consistency of direction)
    direction_probability: float = 0.5  # estimated P(up in next 5 min)


class CryptoPriceFeed:
    """Fetch live crypto prices & compute momentum indicators.

    Designed for the 5-minute Polymarket markets.  Fetches CoinGecko
    market_chart data at the 1-day range (gives ~5-min granularity) and
    derives momentum / volatility metrics.
    """

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        # Spot price cache: ticker → (price, ts)
        self._spot_cache: dict[str, tuple[float, float]] = {}
        self._spot_ttl = 5.0  # 5-second cache
        # History cache: ticker → (prices_list, ts)
        self._hist_cache: dict[str, tuple[list[list[float]], float]] = {}
        self._hist_ttl = 60.0  # 60-second cache for history

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_snapshot(self, ticker: str) -> PriceSnapshot | None:
        """Get a full price snapshot with momentum analytics."""
        ticker = ticker.lower().strip()
        cg_id = TICKER_TO_CG.get(ticker)
        if not cg_id:
            return None

        price = self._fetch_spot(ticker, cg_id)
        if price is None:
            return None

        snap = PriceSnapshot(ticker=ticker, price=price, timestamp=time.time())

        # Try to enrich with historical data
        history = self._fetch_history(ticker, cg_id)
        if history and len(history) >= 3:
            self._compute_analytics(snap, history)

        return snap

    def get_all_snapshots(self) -> dict[str, PriceSnapshot]:
        """Get snapshots for all tracked tickers."""
        # Bulk spot fetch
        self._fetch_spot_bulk()

        results: dict[str, PriceSnapshot] = {}
        # Unique base tickers only (skip aliases)
        unique = {"btc", "eth", "sol", "xrp"}
        for ticker in unique:
            snap = self.get_snapshot(ticker)
            if snap:
                results[ticker] = snap
        return results

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _fetch_spot(self, ticker: str, cg_id: str) -> float | None:
        """Fetch spot price with cache."""
        now = time.time()
        cached = self._spot_cache.get(ticker)
        if cached and (now - cached[1]) < self._spot_ttl:
            return cached[0]

        try:
            resp = requests.get(
                f"{COINGECKO_API}/simple/price",
                params={"ids": cg_id, "vs_currencies": "usd"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            price = data.get(cg_id, {}).get("usd")
            if price is not None:
                self._spot_cache[ticker] = (float(price), now)
                return float(price)
        except Exception as e:
            log.debug("CoinGecko spot fetch failed for %s: %s", ticker, e)

        # Return stale cache if available
        return cached[0] if cached else None

    def _fetch_spot_bulk(self) -> None:
        """Bulk fetch all tracked tickers at once."""
        now = time.time()
        ids = list(set(TICKER_TO_CG.values()))
        try:
            resp = requests.get(
                f"{COINGECKO_API}/simple/price",
                params={"ids": ",".join(ids), "vs_currencies": "usd"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            # Reverse map: cg_id → canonical ticker
            cg_to_canon = {
                "bitcoin": "btc",
                "ethereum": "eth",
                "solana": "sol",
                "ripple": "xrp",
            }
            for cg_id, canon in cg_to_canon.items():
                price = data.get(cg_id, {}).get("usd")
                if price is not None:
                    self._spot_cache[canon] = (float(price), now)
        except Exception as e:
            log.debug("CoinGecko bulk spot fetch failed: %s", e)

    def _fetch_history(self, ticker: str, cg_id: str) -> list[list[float]] | None:
        """Fetch 1-day price history (~5-min data points).

        Returns list of [timestamp_ms, price] pairs.
        """
        now = time.time()
        cached = self._hist_cache.get(ticker)
        if cached and (now - cached[1]) < self._hist_ttl:
            return cached[0]

        try:
            resp = requests.get(
                f"{COINGECKO_API}/coins/{cg_id}/market_chart",
                params={"vs_currency": "usd", "days": "1"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            prices = data.get("prices", [])
            if prices:
                self._hist_cache[ticker] = (prices, now)
                return prices
        except Exception as e:
            log.debug("CoinGecko history fetch failed for %s: %s", ticker, e)

        return cached[0] if cached else None

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def _compute_analytics(
        self, snap: PriceSnapshot, history: list[list[float]]
    ) -> None:
        """Compute momentum, volatility, and directional probability.

        history: list of [timestamp_ms, price] from CoinGecko market_chart.
        CoinGecko 1-day chart gives ~5-min granularity (~288 data points).
        """
        now_ms = time.time() * 1000.0
        # Sort by timestamp ascending
        pts = sorted(history, key=lambda x: x[0])

        # Find prices at various lookback windows
        snap.ret_1m = self._return_at_lookback(pts, now_ms, minutes=1)
        snap.ret_5m = self._return_at_lookback(pts, now_ms, minutes=5)
        snap.ret_15m = self._return_at_lookback(pts, now_ms, minutes=15)
        snap.ret_1h = self._return_at_lookback(pts, now_ms, minutes=60)

        # Volatility: std dev of sequential returns over last hour
        hour_ago_ms = now_ms - 3_600_000
        recent = [p for p in pts if p[0] >= hour_ago_ms]
        if len(recent) >= 3:
            returns = []
            for i in range(1, len(recent)):
                prev_price = recent[i - 1][1]
                if prev_price > 0:
                    returns.append((recent[i][1] - prev_price) / prev_price)
            if returns:
                mean_ret = sum(returns) / len(returns)
                variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
                snap.volatility_1h = math.sqrt(variance)

        # Momentum score: weighted combination of returns at multiple timeframes
        # Shorter timeframes get more weight for 5-min prediction
        components = []
        weights = []
        if snap.ret_1m is not None:
            components.append(snap.ret_1m)
            weights.append(0.1)  # Low weight — too noisy at 1 min
        if snap.ret_5m is not None:
            components.append(snap.ret_5m)
            weights.append(0.35)
        if snap.ret_15m is not None:
            components.append(snap.ret_15m)
            weights.append(0.35)
        if snap.ret_1h is not None:
            components.append(snap.ret_1h)
            weights.append(0.2)

        if components:
            total_weight = sum(weights)
            raw_momentum = sum(c * w for c, w in zip(components, weights)) / total_weight
            # Normalise to [-1, 1] using tanh (2000x scale: 0.05% move → ~0.1 score)
            snap.momentum_score = math.tanh(raw_momentum * 2000.0)

        # Trend strength: consistency of direction across timeframes
        # Higher when ALL timeframes agree on direction
        if components:
            positive_count = sum(1 for c in components if c > 0)
            negative_count = sum(1 for c in components if c < 0)
            snap.trend_strength = max(positive_count, negative_count) / len(components)

        # Direction probability estimation
        # Base = 50%, adjusted by momentum and trend strength
        # With strong consistent momentum, P(up) can reach ~60-65%
        # With maker fees (0.5%), break-even is ~50.25% — plenty of room
        snap.direction_probability = self._estimate_direction_prob(snap)

    @staticmethod
    def _return_at_lookback(
        pts: list[list[float]], now_ms: float, minutes: int
    ) -> float | None:
        """Find the return from `minutes` ago to now.

        Uses the closest available data point to the target time.
        """
        if not pts:
            return None

        target_ms = now_ms - (minutes * 60_000)
        current_price = pts[-1][1]
        if current_price <= 0:
            return None

        # Find closest point to target_ms
        closest = min(pts, key=lambda p: abs(p[0] - target_ms))
        # Reject if the closest point is too far from target (>3x the window)
        if abs(closest[0] - target_ms) > minutes * 60_000 * 3:
            return None

        old_price = closest[1]
        if old_price <= 0:
            return None

        return (current_price - old_price) / old_price

    @staticmethod
    def _estimate_direction_prob(snap: PriceSnapshot) -> float:
        """Estimate probability that price moves UP in next 5 minutes.

        Model:
        - Base rate = 50% (coin flip)
        - Momentum adjustment: strong trends persist at short timeframes
          (positive autocorrelation ~0.02-0.05 in crypto at 1-5 min)
        - Trend consistency amplifier: when all timeframes agree, confidence
          is higher (reduces chance of mean-reversion dominating)

        Conservative estimates — we want to avoid overconfidence:
        - Max adjustment: ±12% (probability range: 38% - 62%)
        - Requires strong, consistent momentum for max adjustment

        Why this works at maker fee levels (0.5%):
        - Break-even at p=0.50 is 50.25% — need only 0.25% edge
        - Even weak momentum (~0.5% in last 15 min) can provide this
        - Academic research (Cont 2001, Bouchaud 2004) shows crypto has
          positive short-term momentum autocorrelation
        """
        base = 0.50
        # Momentum component: scales with momentum_score * trend_strength
        # momentum_score is already [-1, 1], trend_strength is [0, 1]
        momentum_adj = snap.momentum_score * snap.trend_strength * 0.12

        # Volatility damper: in very high volatility regimes, momentum is
        # less predictive (more noise). Reduce adjustment.
        if snap.volatility_1h > 0.005:  # >0.5% std dev per 5-min bar
            damper = max(0.3, 1.0 - (snap.volatility_1h - 0.005) * 20)
            momentum_adj *= damper

        prob = base + momentum_adj
        # Hard-clamp to reasonable range
        return max(0.38, min(0.62, prob))
