"""Event-driven oracle sniping strategy.

This is the highest-alpha strategy: detect when a real-world event has already
occurred (e.g., Bitcoin crossed $100K) but Polymarket hasn't updated the market
price yet. Buy the winning outcome at a discount.

Architecture:
- ExternalOracle: fetches real-world data (crypto prices, sports scores, etc.)
- OracleSnipingStrategy: compares oracle data to Polymarket prices, generates signals

Supported oracle data sources:
- Crypto prices: CoinGecko API (free, no auth required)
- Extensible: add more data sources for sports, politics, etc.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import requests

from polymarket_bot.strategy import (
    Opportunity,
    Strategy,
    StrategySignal,
    StrategyType,
    Trade,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# External data oracles
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OracleResult:
    """Result from an external data oracle."""
    source: str
    asset: str
    current_value: float
    timestamp: float
    confidence: Decimal  # 0-1 how sure we are the data is correct
    metadata: dict[str, Any]


class CryptoOracle:
    """Fetches real-time crypto prices from CoinGecko (free, no auth).

    Supported tickers: BTC, ETH, SOL, MATIC, DOGE, XRP, ADA, DOT, LINK, etc.
    """

    COINGECKO_API = "https://api.coingecko.com/api/v3"

    # Map common short names to CoinGecko IDs
    TICKER_MAP = {
        "btc": "bitcoin",
        "bitcoin": "bitcoin",
        "eth": "ethereum",
        "ethereum": "ethereum",
        "sol": "solana",
        "solana": "solana",
        "matic": "matic-network",
        "polygon": "matic-network",
        "doge": "dogecoin",
        "dogecoin": "dogecoin",
        "xrp": "ripple",
        "ripple": "ripple",
        "ada": "cardano",
        "cardano": "cardano",
        "dot": "polkadot",
        "polkadot": "polkadot",
        "link": "chainlink",
        "chainlink": "chainlink",
        "avax": "avalanche-2",
        "avalanche": "avalanche-2",
        "bnb": "binancecoin",
    }

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self._cache: dict[str, tuple[float, float]] = {}  # ticker -> (price, ts)
        self._cache_ttl = 5.0  # Cache for 5 seconds

    def get_price(self, ticker: str) -> OracleResult | None:
        """Get current price for a crypto asset."""
        ticker_lower = ticker.lower().strip()
        coingecko_id = self.TICKER_MAP.get(ticker_lower)
        if not coingecko_id:
            return None

        # Check cache
        now = time.time()
        if ticker_lower in self._cache:
            cached_price, cached_ts = self._cache[ticker_lower]
            if now - cached_ts < self._cache_ttl:
                return OracleResult(
                    source="coingecko",
                    asset=ticker_lower,
                    current_value=cached_price,
                    timestamp=cached_ts,
                    confidence=Decimal("0.99"),
                    metadata={"coingecko_id": coingecko_id, "cached": True},
                )

        try:
            url = f"{self.COINGECKO_API}/simple/price"
            params = {"ids": coingecko_id, "vs_currencies": "usd"}
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()

            price = data.get(coingecko_id, {}).get("usd")
            if price is None:
                return None

            self._cache[ticker_lower] = (float(price), now)

            return OracleResult(
                source="coingecko",
                asset=ticker_lower,
                current_value=float(price),
                timestamp=now,
                confidence=Decimal("0.99"),
                metadata={"coingecko_id": coingecko_id},
            )
        except Exception as e:
            log.debug("CoinGecko fetch failed for %s: %s", ticker, e)
            return None

    def get_all_tracked(self) -> dict[str, float]:
        """Fetch prices for all known tickers at once."""
        ids = list(set(self.TICKER_MAP.values()))
        try:
            url = f"{self.COINGECKO_API}/simple/price"
            params = {"ids": ",".join(ids), "vs_currencies": "usd"}
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()

            now = time.time()
            result: dict[str, float] = {}
            for ticker, cg_id in self.TICKER_MAP.items():
                price = data.get(cg_id, {}).get("usd")
                if price is not None:
                    result[ticker] = float(price)
                    self._cache[ticker] = (float(price), now)
            return result
        except Exception as e:
            log.debug("CoinGecko bulk fetch failed: %s", e)
            return {}


# ---------------------------------------------------------------------------
# Market question parser
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParsedPriceMarket:
    """A parsed crypto price market from Polymarket."""
    ticker: str
    target_price: float
    direction: str  # "above" or "below"
    deadline: str | None  # Date string if parseable


def parse_crypto_price_market(question: str) -> ParsedPriceMarket | None:
    """Try to parse a Polymarket question as a crypto price prediction.

    Examples:
    - "Will Bitcoin be above $100,000 on March 31?"
    - "Will ETH reach $5,000 before April 2025?"
    - "Bitcoin above $110k?"
    """
    q = question.lower().strip()

    # Match patterns like "btc/bitcoin/eth... above/below/reach $X,XXX"
    ticker_pattern = "|".join(CryptoOracle.TICKER_MAP.keys())
    pattern = rf"(?:will\s+)?({ticker_pattern})\s+(?:be\s+)?(?:above|over|reach|hit|exceed|surpass|below|under)\s+\$?([\d,]+\.?\d*[kKmM]?)"
    match = re.search(pattern, q)
    if not match:
        return None

    ticker = match.group(1).strip()
    price_str = match.group(2).replace(",", "").strip()

    # Handle k/m suffixes
    multiplier = 1
    if price_str.endswith(("k", "K")):
        multiplier = 1000
        price_str = price_str[:-1]
    elif price_str.endswith(("m", "M")):
        multiplier = 1_000_000
        price_str = price_str[:-1]

    try:
        target_price = float(price_str) * multiplier
    except ValueError:
        return None

    # Determine direction
    direction = "above"
    if any(word in q for word in ["below", "under", "fall"]):
        direction = "below"

    return ParsedPriceMarket(
        ticker=ticker,
        target_price=target_price,
        direction=direction,
        deadline=None,  # Could be enhanced with date parsing
    )


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class OracleSnipingStrategy(Strategy):
    """Buy outcomes that have already occurred but Polymarket hasn't priced in.

    Flow:
    1. Scan active markets for crypto price questions
    2. Parse the target price from the question
    3. Check the actual current price via CoinGecko
    4. If the target is already met and the market is still below $0.95, BUY

    This exploits the lag between real-world events and market price updates.
    """

    def __init__(
        self,
        taker_fee_rate: Decimal = Decimal("0.02"),
        max_order_usdc: Decimal = Decimal("50"),
        min_confidence: Decimal = Decimal("0.95"),
        enabled: bool = True,
    ) -> None:
        super().__init__(name="oracle_sniping", enabled=enabled)
        self.taker_fee_rate = taker_fee_rate
        self.max_order_usdc = max_order_usdc
        self.min_confidence = min_confidence
        self.oracle = CryptoOracle()
        self._last_oracle_fetch = 0.0
        self._oracle_cache: dict[str, float] = {}

    def scan(self, market_data: dict[str, Any]) -> list[StrategySignal]:
        """Scan active markets for oracle-exploitable opportunities."""
        signals: list[StrategySignal] = []
        markets = market_data.get("markets", [])

        # Refresh oracle data periodically (bulk fetch)
        now = time.time()
        if now - self._last_oracle_fetch > 10.0:
            self._oracle_cache = self.oracle.get_all_tracked()
            self._last_oracle_fetch = now

        if not self._oracle_cache:
            return signals

        for market in markets:
            question = str(market.get("question", ""))
            condition_id = str(market.get("condition_id", ""))

            # Try to parse as crypto price market
            parsed = parse_crypto_price_market(question)
            if parsed is None:
                continue

            # Get actual current price
            actual_price = self._oracle_cache.get(parsed.ticker)
            if actual_price is None:
                continue

            # Determine if the outcome is already known
            outcome_known = False
            winning_outcome = None
            if parsed.direction == "above" and actual_price >= parsed.target_price:
                outcome_known = True
                winning_outcome = "YES"
            elif parsed.direction == "above" and actual_price < parsed.target_price * 0.95:
                # Price is far below target — NO is very likely (but not guaranteed
                # since there may be time remaining). Skip to be safe.
                pass
            elif parsed.direction == "below" and actual_price <= parsed.target_price:
                outcome_known = True
                winning_outcome = "YES"
            elif parsed.direction == "below" and actual_price > parsed.target_price * 1.05:
                outcome_known = True
                winning_outcome = "NO"

            if not outcome_known or not winning_outcome:
                continue

            # Find the winning token
            tokens = market.get("tokens", [])
            winning_token = None
            for token in tokens:
                if token.get("outcome", "").upper() == winning_outcome.upper():
                    winning_token = token
                    break

            if not winning_token:
                continue

            # Get current ask price
            ask_price = winning_token.get("best_ask") or winning_token.get("price")
            if ask_price is None:
                continue

            ask_price = Decimal(str(ask_price))

            # Only trade if there's significant discount (after fees)
            # Polymarket fee: fee_rate * min(price, 1-price)
            fee = self.taker_fee_rate * min(ask_price, Decimal("1") - ask_price)
            net_profit_per_share = Decimal("1") - ask_price - fee

            # Need at least 3 cents profit after fees
            if net_profit_per_share < Decimal("0.03"):
                continue

            # Don't buy if already near $1
            if ask_price > Decimal("0.97"):
                continue

            # Calculate position size
            if ask_price <= 0:
                continue
            size = (self.max_order_usdc / ask_price).quantize(Decimal("0.01"))
            if size <= 0:
                continue

            expected_profit = net_profit_per_share * size

            opportunity = Opportunity(
                strategy_type=StrategyType.ORACLE_SNIPING,
                expected_profit=expected_profit,
                confidence=Decimal("0.98"),
                urgency=10,  # CRITICAL — these disappear fast
                metadata={
                    "condition_id": condition_id,
                    "winning_token_id": winning_token.get("token_id"),
                    "winning_outcome": winning_outcome,
                    "ask_price": float(ask_price),
                    "question": question,
                    "oracle_source": "coingecko",
                    "oracle_ticker": parsed.ticker,
                    "oracle_price": actual_price,
                    "target_price": parsed.target_price,
                    "direction": parsed.direction,
                    "net_profit_per_share": float(net_profit_per_share),
                    "strategy_sub_type": "oracle_sniping",
                },
            )

            trades = [
                Trade(
                    token_id=winning_token.get("token_id"),
                    side="BUY",
                    size=size,
                    price=ask_price,
                    order_type="FOK",  # Atomic — must fill or nothing
                ),
            ]

            signal = StrategySignal(
                opportunity=opportunity,
                trades=trades,
                max_total_cost=ask_price * size,
                min_expected_return=size,
            )

            signals.append(signal)
            log.warning(
                "🔮 ORACLE SNIPE: %s is at $%.2f (target $%.0f %s) — "
                "%s trading at $%.4f — expected profit $%.4f",
                parsed.ticker.upper(),
                actual_price,
                parsed.target_price,
                parsed.direction,
                winning_outcome,
                float(ask_price),
                float(expected_profit),
            )

        return signals

    def validate(self, signal: StrategySignal) -> tuple[bool, str]:
        """Validate oracle sniping signal."""
        if len(signal.trades) != 1:
            return False, "invalid_trade_count"

        trade = signal.trades[0]
        if trade.side != "BUY":
            return False, "must_be_buy"

        if trade.price >= Decimal("1"):
            return False, "price_too_high"

        # Re-verify with oracle if possible
        meta = signal.opportunity.metadata
        ticker = meta.get("oracle_ticker")
        if ticker:
            result = self.oracle.get_price(ticker)
            if result:
                target = meta.get("target_price", 0)
                direction = meta.get("direction", "above")
                if direction == "above" and result.current_value < target:
                    return False, "oracle_condition_no_longer_met"
                elif direction == "below" and result.current_value > target:
                    return False, "oracle_condition_no_longer_met"

        return True, "ok"
