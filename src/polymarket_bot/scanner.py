"""Market scanner for fetching and processing Polymarket market data.

This module interfaces with Polymarket's Gamma API to discover markets
and provide real-time data to trading strategies.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

GAMMA_API_BASE = "https://gamma-api.polymarket.com"

# Default limit for fetching markets when no specific limit is provided
# Set high to capture all markets - Polymarket typically has 1000-3000 active markets
# but we set a higher limit to ensure we don't miss any markets as the platform grows
# This can be overridden via MARKET_FETCH_LIMIT environment variable
DEFAULT_FETCH_LIMIT = 10000

# Threshold for warning when fetched markets approach the limit
# If we fetch within this many markets of the limit, warn the user
LIMIT_WARNING_THRESHOLD = 10


@dataclass(frozen=True)
class MarketInfo:
    """Information about a Polymarket market."""
    condition_id: str
    question: str
    end_date: str | None
    tokens: list[TokenInfo]
    volume: Decimal
    liquidity: Decimal
    active: bool
    closed: bool
    resolved: bool
    winning_outcome: str | None = None
    neg_risk_market_id: str | None = None  # Shared ID for multi-outcome groups
    group_item_title: str | None = None  # Bracket label (e.g. "250-500k")
    # Liquidity reward fields (from Gamma API)
    rewards_min_size: Decimal | None = None
    rewards_max_spread: Decimal | None = None
    rewards_daily_rate: Decimal | None = None
    # Price / spread metadata
    spread: Decimal | None = None
    one_day_price_change: float | None = None
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None


@dataclass(frozen=True)
class TokenInfo:
    """Information about a market token (YES/NO)."""
    token_id: str
    outcome: str
    price: Decimal
    volume: Decimal


class MarketScanner:
    """Scans Polymarket for trading opportunities."""

    def __init__(self, api_base: str = GAMMA_API_BASE, fetch_limit: int | None = None):
        """Initialize the market scanner.
        
        Args:
            api_base: Base URL for the Gamma API
            fetch_limit: Maximum number of markets to fetch (None = use DEFAULT_FETCH_LIMIT)
        """
        self.api_base = api_base
        self.fetch_limit = fetch_limit if fetch_limit is not None else DEFAULT_FETCH_LIMIT
        self._markets_cache: dict[str, MarketInfo] = {}
        self._markets_cache_by_token: dict[str, MarketInfo] = {}
        self._last_refresh = 0.0
        self._refresh_interval = 60.0  # Cache for 60 seconds

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make a GET request to Gamma API with retries."""
        url = f"{self.api_base}{endpoint}"
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            log.error(f"HTTP error fetching {endpoint}: {e.response.status_code} - {e.response.text}")
            raise
        except requests.exceptions.RequestException as e:
            log.error(f"Request failed for {endpoint}: {e}")
            raise

    def get_all_markets(self, limit: int | None = None, active_only: bool = True) -> list[MarketInfo]:
        """Fetch all markets from Gamma API.
        
        Args:
            limit: Maximum number of markets to fetch. If None, uses the scanner's configured
                   fetch_limit (set in __init__ or DEFAULT_FETCH_LIMIT).
            active_only: If True, only return active (non-closed) markets.
            
        Returns:
            List of MarketInfo objects.
        """
        try:
            # Use provided limit, scanner's fetch_limit, or default
            fetch_limit = limit if limit is not None else self.fetch_limit
            
            # Special handling: 0 means unlimited (within API constraints)
            if fetch_limit == 0:
                fetch_limit = DEFAULT_FETCH_LIMIT
            
            # Gamma API endpoint for markets
            # - active_only=True  => explicit active open markets only
            # - active_only=False => full universe (do not force active=False,
            #   which would return only inactive markets)
            params: dict = {"limit": fetch_limit}
            if active_only:
                params["active"] = True
                params["closed"] = False  # Exclude resolved / settled markets
            response = self._get("/markets", params=params)
            
            markets = []
            parse_errors = 0
            for market_data in response:
                try:
                    markets.append(self._parse_market(market_data))
                except Exception as e:
                    parse_errors += 1
                    log.debug(f"Failed to parse market: {e}")
                    continue
            
            # Log comprehensive stats (debug — fires every scan cycle)
            log.debug(
                f"Fetched {len(markets)} markets from Gamma API "
                f"(requested_limit={fetch_limit}, active_only={active_only}, parse_errors={parse_errors})"
            )
            if len(markets) >= fetch_limit - LIMIT_WARNING_THRESHOLD:
                log.warning(
                    f"Retrieved {len(markets)} markets, close to limit of {fetch_limit}. "
                    "There may be more markets available. Consider increasing MARKET_FETCH_LIMIT if needed."
                )
            
            return markets
            
        except Exception as e:
            log.error(f"Failed to fetch markets: {e}")
            return []

    def get_market(self, condition_id: str) -> MarketInfo | None:
        """Fetch a specific market by condition ID."""
        # Gamma /markets/{id} often rejects 0x-style condition ids; skip straight
        # to cache/list fallback for those to avoid expensive retry noise.
        if not str(condition_id).startswith("0x"):
            try:
                response = self._get(f"/markets/{condition_id}")
                return self._parse_market(response)
            except Exception as e:
                log.debug(f"Primary market lookup failed for {condition_id}: {e}")

        # Fallback path: Gamma /markets/{id} may reject condition_id-style hashes
        # with 422. In that case, resolve by scanning market lists and matching
        # the `conditionId` field directly.
        try:
            cached = self.get_cached_market(condition_id)
            if cached is not None:
                return cached

            # Force refresh including closed/resolved markets for lifecycle checks.
            self.refresh_cache(force=True)
            cached = self._markets_cache.get(condition_id)
            if cached is not None:
                return cached

            # Final fallback: explicit fetch and linear match (case-insensitive).
            needle = str(condition_id).lower()
            for market in self.get_all_markets(limit=None, active_only=False):
                if str(market.condition_id).lower() == needle:
                    return market
        except Exception as e:
            log.debug("Condition-id fallback lookup failed for %s: %s", condition_id, e)

        log.error("Failed to fetch market %s via both id and condition-id lookup", condition_id)
        return None

    def get_market_by_token(self, token_id: str) -> MarketInfo | None:
        """Fetch market containing a specific CLOB token_id."""
        # Direct Gamma lookup by token id is the most reliable path for negRisk
        # bracket tokens used by arb strategies.
        try:
            response = self._get("/markets", params={"limit": 1, "clob_token_ids": str(token_id)})
            if isinstance(response, list) and response:
                return self._parse_market(response[0])
        except Exception as e:
            log.debug("Token-id lookup failed for %s: %s", token_id, e)

        self.refresh_cache()
        market = self._markets_cache_by_token.get(str(token_id))
        if market is not None:
            return market

        # Force refresh including closed/resolved markets and retry.
        self.refresh_cache(force=True)
        return self._markets_cache_by_token.get(str(token_id))

    def get_high_volume_markets(
        self,
        min_volume: Decimal = Decimal("10000"),
        limit: int | None = None
    ) -> list[MarketInfo]:
        """Get high-volume markets suitable for arbitrage.
        
        High volume markets typically have:
        - More liquidity
        - Tighter spreads
        - Less slippage risk
        
        Args:
            min_volume: Minimum volume threshold
            limit: Maximum number of markets to return after filtering (None = all)
        """
        markets = self.get_all_markets(limit=None)  # Fetch all markets first
        high_volume = [m for m in markets if m.volume >= min_volume and m.active]
        high_volume.sort(key=lambda m: m.volume, reverse=True)
        
        # Log filtering stats (debug — fires every scan cycle)
        log.debug(
            f"High-volume market filter: {len(markets)} total markets -> "
            f"{len(high_volume)} markets with volume >= ${min_volume:,.0f}"
        )
        
        # Apply limit after filtering if specified
        if limit is not None:
            high_volume = high_volume[:limit]
            
        return high_volume

    def get_resolved_markets(self, limit: int | None = None) -> list[MarketInfo]:
        """Get recently resolved markets.
        
        Useful for detecting guaranteed win opportunities where
        winning shares still trade below $1.
        
        Args:
            limit: Maximum number of markets to fetch. If None, fetches all available
                   resolved markets up to DEFAULT_FETCH_LIMIT.
        """
        try:
            # Use provided limit or default to fetching all markets
            fetch_limit = limit if limit is not None else DEFAULT_FETCH_LIMIT
            
            # Look for closed/resolved markets
            response = self._get("/markets", params={"limit": fetch_limit, "closed": True})
            
            markets = []
            for market_data in response:
                try:
                    market = self._parse_market(market_data)
                    if market.resolved:
                        markets.append(market)
                except Exception:
                    continue
                    
            log.debug(f"Found {len(markets)} resolved markets (limit={fetch_limit})")
            return markets
            
        except Exception as e:
            log.error(f"Failed to fetch resolved markets: {e}")
            return []

    def get_crypto_markets(self, limit: int | None = None) -> list[MarketInfo]:
        """Get crypto-related markets (fast-moving, good for arbitrage).
        
        Args:
            limit: Maximum number of markets to return. If None, returns all matching markets.
        """
        markets = self.get_all_markets(limit=None)  # Fetch all markets first
        # Simple keyword filter - could be made more sophisticated
        crypto_keywords = ["btc", "bitcoin", "eth", "ethereum", "crypto", "sol", "solana"]
        crypto_markets = [
            m for m in markets
            if any(kw in m.question.lower() for kw in crypto_keywords)
        ]
        
        # Apply limit if specified
        if limit is not None:
            crypto_markets = crypto_markets[:limit]
            
        return crypto_markets

    def _parse_market(self, data: dict[str, Any]) -> MarketInfo:
        """Parse market data from Gamma API response.

        The Gamma API uses camelCase field names and encodes token data as
        JSON-stringified arrays in separate fields:
        - ``conditionId`` (str): market condition identifier
        - ``outcomes`` (JSON str): e.g. '["Yes", "No"]'
        - ``outcomePrices`` (JSON str): e.g. '["0.55", "0.45"]'
        - ``clobTokenIds`` (JSON str): e.g. '["abc123...", "def456..."]'
        """
        import json as _json

        # Parse token data from the three parallel JSON arrays
        tokens: list[TokenInfo] = []
        winner_outcome_from_tokens: str | None = None
        try:
            outcomes_raw = data.get("outcomes") or "[]"
            prices_raw = data.get("outcomePrices") or "[]"
            token_ids_raw = data.get("clobTokenIds") or "[]"

            outcomes = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            prices = _json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            token_ids = _json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw

            # Also pull per-token volume from the newer ``tokens`` field if present
            tokens_extra: list[dict] = data.get("tokens", []) or []
            volume_by_id: dict[str, Decimal] = {}
            for tex in tokens_extra:
                if isinstance(tex, dict):
                    tid = str(tex.get("token_id", ""))
                    vol = tex.get("volume", 0)
                    if tid:
                        volume_by_id[tid] = Decimal(str(vol))
                    if bool(tex.get("winner", False)) and winner_outcome_from_tokens is None:
                        winner_outcome_from_tokens = str(tex.get("outcome") or "").upper() or None

            for i, outcome in enumerate(outcomes):
                token_id = str(token_ids[i]) if i < len(token_ids) else ""
                price = Decimal(str(prices[i])) if i < len(prices) else Decimal("0")
                volume = volume_by_id.get(token_id, Decimal("0"))
                tokens.append(TokenInfo(
                    token_id=token_id,
                    outcome=str(outcome),
                    price=price,
                    volume=volume,
                ))
        except Exception as e:
            log.debug("Token parsing fallback for market %s: %s", data.get("conditionId", "?")[:12], e)

        # Use total market volume as fallback for per-token volume
        market_volume = Decimal(str(data.get("volume", 0)))
        if tokens and all(t.volume == 0 for t in tokens):
            per_token = market_volume / len(tokens) if len(tokens) > 0 else Decimal("0")
            tokens = [
                TokenInfo(token_id=t.token_id, outcome=t.outcome, price=t.price, volume=per_token)
                for t in tokens
            ]

        # Parse optional reward / spread fields (may be absent or null)
        def _dec_or_none(key: str) -> Decimal | None:
            v = data.get(key)
            return Decimal(str(v)) if v is not None else None

        # Gamma has used multiple field names across payload variants.
        winning_outcome = (
            data.get("winning_outcome")
            or data.get("winningOutcome")
            or data.get("winner")
            or winner_outcome_from_tokens
        )
        if winning_outcome is not None:
            winning_outcome = str(winning_outcome).upper()

        return MarketInfo(
            condition_id=str(data.get("conditionId") or data.get("condition_id") or ""),
            question=str(data.get("question", "")),
            end_date=data.get("endDateIso") or data.get("end_date_iso"),
            tokens=tokens,
            volume=market_volume,
            liquidity=Decimal(str(data.get("liquidity", 0))),
            active=bool(data.get("active", True)),
            closed=bool(data.get("closed", False)),
            resolved=bool(data.get("resolved", False)),
            winning_outcome=winning_outcome,
            neg_risk_market_id=data.get("negRiskMarketID") or data.get("neg_risk_market_id"),
            group_item_title=data.get("groupItemTitle") or data.get("group_item_title"),
            rewards_min_size=_dec_or_none("rewardsMinSize"),
            rewards_max_spread=_dec_or_none("rewardsMaxSpread"),
            rewards_daily_rate=_dec_or_none("rewardsDailyRate"),
            spread=_dec_or_none("spread"),
            one_day_price_change=float(data["oneDayPriceChange"]) if data.get("oneDayPriceChange") is not None else None,
            best_bid=_dec_or_none("bestBid"),
            best_ask=_dec_or_none("bestAsk"),
        )

    def refresh_cache(self, force: bool = False) -> None:
        """Refresh the market cache."""
        now = time.time()
        if force or (now - self._last_refresh) > self._refresh_interval:
            # Include closed/resolved markets so resolution monitoring can find
            # recently settled conditions by condition_id.
            markets = self.get_all_markets(limit=None, active_only=False)
            self._markets_cache = {m.condition_id: m for m in markets}
            token_map: dict[str, MarketInfo] = {}
            for market in markets:
                for token in market.tokens:
                    tid = str(token.token_id)
                    if tid:
                        token_map[tid] = market
            self._markets_cache_by_token = token_map
            self._last_refresh = now
            log.info(f"Market cache refreshed with {len(self._markets_cache)} markets")

    def get_cached_market(self, condition_id: str) -> MarketInfo | None:
        """Get a market from cache (refresh if stale)."""
        self.refresh_cache()
        return self._markets_cache.get(condition_id)

    # ------------------------------------------------------------------
    # Resolution-time helpers
    # ------------------------------------------------------------------

    @staticmethod
    def parse_end_date(end_date: str | None) -> datetime | None:
        """Parse a market's end_date string into a timezone-aware datetime.

        Handles ISO-8601 date or datetime strings from Gamma API (e.g.
        ``"2025-12-31"``, ``"2025-12-31T23:59:59Z"``).

        Returns ``None`` if the value is missing or unparseable.
        """
        if not end_date:
            return None
        try:
            # Try full ISO datetime first
            dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
        try:
            # Date-only string → assume end-of-day UTC
            dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            )
            return dt
        except ValueError:
            return None

    @staticmethod
    def hours_to_resolution(end_date: str | None) -> float | None:
        """Return hours until market resolution, or None if unknown.

        Returns a **negative** value for markets whose end_date is already
        in the past.  Callers that need non-negative values should clamp
        explicitly; ``filter_by_resolution_window()`` relies on negative
        values to exclude expired markets.
        """
        dt = MarketScanner.parse_end_date(end_date)
        if dt is None:
            return None
        now = datetime.now(timezone.utc)
        return (dt - now).total_seconds() / 3600.0

    def filter_by_resolution_window(
        self,
        markets: list[MarketInfo],
        min_days: float = 0.0,
        max_days: float = 30.0,
    ) -> list[MarketInfo]:
        """Filter markets to those resolving within a time window.

        Args:
            markets: List of markets to filter.
            min_days: Minimum days to resolution (0 = no minimum).
            max_days: Maximum days to resolution (0 = no maximum / unlimited).

        Returns:
            Markets whose end_date falls within [now + min_days, now + max_days].
            Markets without a parseable end_date are **excluded** when max_days > 0.
        """
        if min_days <= 0 and max_days <= 0:
            return markets  # No filtering requested

        min_hours = min_days * 24.0
        max_hours = max_days * 24.0

        filtered: list[MarketInfo] = []
        excluded_no_date = 0
        excluded_outside = 0

        for market in markets:
            hours = self.hours_to_resolution(market.end_date)
            if hours is None:
                if max_days > 0:
                    excluded_no_date += 1
                    continue
                else:
                    # No max → include markets without dates
                    filtered.append(market)
                    continue

            # Exclude markets whose end_date is already in the past
            if hours < 0:
                excluded_outside += 1
                continue

            if min_hours > 0 and hours < min_hours:
                excluded_outside += 1
                continue
            if max_hours > 0 and hours > max_hours:
                excluded_outside += 1
                continue

            filtered.append(market)

        if excluded_no_date or excluded_outside:
            log.debug(
                "Resolution window filter: %d → %d markets "
                "(excluded: %d no date, %d outside window [%.0f-%.0f days])",
                len(markets), len(filtered),
                excluded_no_date, excluded_outside, min_days, max_days,
            )

        return filtered
