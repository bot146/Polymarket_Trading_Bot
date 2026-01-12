"""Market scanner for fetching and processing Polymarket market data.

This module interfaces with Polymarket's Gamma API to discover markets
and provide real-time data to trading strategies.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
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
            response = self._get("/markets", params={"limit": fetch_limit, "active": active_only})
            
            markets = []
            parse_errors = 0
            for market_data in response:
                try:
                    markets.append(self._parse_market(market_data))
                except Exception as e:
                    parse_errors += 1
                    log.debug(f"Failed to parse market: {e}")
                    continue
            
            # Log comprehensive stats
            log.info(
                f"Fetched {len(markets)} markets from Gamma API "
                f"(requested_limit={fetch_limit}, active_only={active_only}, parse_errors={parse_errors})"
            )
            if len(markets) >= fetch_limit - 10:
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
        try:
            response = self._get(f"/markets/{condition_id}")
            return self._parse_market(response)
        except Exception as e:
            log.error(f"Failed to fetch market {condition_id}: {e}")
            return None

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
        
        # Log filtering stats
        log.info(
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
                    
            log.info(f"Found {len(markets)} resolved markets (limit={fetch_limit})")
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
        """Parse market data from API response."""
        # Parse tokens
        tokens = []
        tokens_data = data.get("tokens", [])
        
        for token_data in tokens_data:
            tokens.append(TokenInfo(
                token_id=str(token_data.get("token_id", "")),
                outcome=str(token_data.get("outcome", "")),
                price=Decimal(str(token_data.get("price", 0))),
                volume=Decimal(str(token_data.get("volume", 0))),
            ))

        return MarketInfo(
            condition_id=str(data.get("condition_id", "")),
            question=str(data.get("question", "")),
            end_date=data.get("end_date_iso"),
            tokens=tokens,
            volume=Decimal(str(data.get("volume", 0))),
            liquidity=Decimal(str(data.get("liquidity", 0))),
            active=bool(data.get("active", True)),
            closed=bool(data.get("closed", False)),
            resolved=bool(data.get("resolved", False)),
            winning_outcome=data.get("winning_outcome"),
        )

    def refresh_cache(self, force: bool = False) -> None:
        """Refresh the market cache."""
        now = time.time()
        if force or (now - self._last_refresh) > self._refresh_interval:
            markets = self.get_all_markets(limit=None)  # Fetch all markets for cache
            self._markets_cache = {m.condition_id: m for m in markets}
            self._last_refresh = now
            log.info(f"Market cache refreshed with {len(self._markets_cache)} markets")

    def get_cached_market(self, condition_id: str) -> MarketInfo | None:
        """Get a market from cache (refresh if stale)."""
        self.refresh_cache()
        return self._markets_cache.get(condition_id)
