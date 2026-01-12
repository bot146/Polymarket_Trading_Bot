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

    def __init__(self, api_base: str = GAMMA_API_BASE):
        self.api_base = api_base
        self._markets_cache: dict[str, MarketInfo] = {}
        self._last_refresh = 0.0
        self._refresh_interval = 60.0  # Cache for 60 seconds

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make a GET request to Gamma API with retries."""
        url = f"{self.api_base}{endpoint}"
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    def get_all_markets(self, limit: int = 100, active_only: bool = True) -> list[MarketInfo]:
        """Fetch all markets from Gamma API.
        
        Args:
            limit: Maximum number of markets to fetch.
            active_only: If True, only return active (non-closed) markets.
            
        Returns:
            List of MarketInfo objects.
        """
        try:
            # Gamma API endpoint for markets
            response = self._get("/markets", params={"limit": limit, "active": active_only})
            
            markets = []
            for market_data in response:
                try:
                    markets.append(self._parse_market(market_data))
                except Exception as e:
                    log.warning(f"Failed to parse market: {e}")
                    continue
                    
            log.info(f"Fetched {len(markets)} markets from Gamma API")
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
        limit: int = 50
    ) -> list[MarketInfo]:
        """Get high-volume markets suitable for arbitrage.
        
        High volume markets typically have:
        - More liquidity
        - Tighter spreads
        - Less slippage risk
        """
        markets = self.get_all_markets(limit=limit)
        high_volume = [m for m in markets if m.volume >= min_volume and m.active]
        high_volume.sort(key=lambda m: m.volume, reverse=True)
        return high_volume

    def get_resolved_markets(self, limit: int = 100) -> list[MarketInfo]:
        """Get recently resolved markets.
        
        Useful for detecting guaranteed win opportunities where
        winning shares still trade below $1.
        """
        try:
            # Look for closed/resolved markets
            response = self._get("/markets", params={"limit": limit, "closed": True})
            
            markets = []
            for market_data in response:
                try:
                    market = self._parse_market(market_data)
                    if market.resolved:
                        markets.append(market)
                except Exception:
                    continue
                    
            log.info(f"Found {len(markets)} resolved markets")
            return markets
            
        except Exception as e:
            log.error(f"Failed to fetch resolved markets: {e}")
            return []

    def get_crypto_markets(self, limit: int = 50) -> list[MarketInfo]:
        """Get crypto-related markets (fast-moving, good for arbitrage)."""
        markets = self.get_all_markets(limit=limit)
        # Simple keyword filter - could be made more sophisticated
        crypto_keywords = ["btc", "bitcoin", "eth", "ethereum", "crypto", "sol", "solana"]
        crypto_markets = [
            m for m in markets
            if any(kw in m.question.lower() for kw in crypto_keywords)
        ]
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
            markets = self.get_all_markets(limit=200)
            self._markets_cache = {m.condition_id: m for m in markets}
            self._last_refresh = now
            log.info(f"Market cache refreshed with {len(self._markets_cache)} markets")

    def get_cached_market(self, condition_id: str) -> MarketInfo | None:
        """Get a market from cache (refresh if stale)."""
        self.refresh_cache()
        return self._markets_cache.get(condition_id)
