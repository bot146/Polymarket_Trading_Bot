"""Order book depth verification.

Before executing trades, verify that sufficient liquidity exists at the
intended price level. This prevents partial fills and slippage disasters.

Uses Polymarket's CLOB API to fetch the full order book for a token and
checks that the available quantity at or better than the limit price
meets a minimum threshold.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import requests

log = logging.getLogger(__name__)

CLOB_API_BASE = "https://clob.polymarket.com"


@dataclass(frozen=True)
class DepthCheck:
    """Result of an order book depth check."""
    token_id: str
    side: str  # BUY or SELL
    limit_price: Decimal
    available_size: Decimal
    available_notional: Decimal  # available_size * price
    sufficient: bool
    levels_checked: int


class OrderBookDepthChecker:
    """Checks order book depth before placing trades.

    For BUY orders: checks how much liquidity is on the ASK side at or below
    the limit price.
    For SELL orders: checks how much liquidity is on the BID side at or above
    the limit price.
    """

    def __init__(
        self,
        min_depth_usdc: Decimal = Decimal("10"),
        api_base: str = CLOB_API_BASE,
        timeout: float = 5.0,
    ) -> None:
        self.min_depth_usdc = min_depth_usdc
        self.api_base = api_base
        self.timeout = timeout
        # Cache to avoid hammering the API on multi-leg trades
        self._cache: dict[str, dict[str, Any]] = {}

    def check_depth(
        self,
        token_id: str,
        side: str,
        limit_price: Decimal,
        required_size: Decimal,
    ) -> DepthCheck:
        """Check if sufficient liquidity exists at the intended price.

        Args:
            token_id: The token to check
            side: "BUY" or "SELL"
            limit_price: The price we intend to trade at
            required_size: The number of shares we want

        Returns:
            DepthCheck with sufficiency assessment
        """
        book = self._fetch_book(token_id)
        if book is None:
            # If we can't fetch the book, be conservative
            return DepthCheck(
                token_id=token_id,
                side=side,
                limit_price=limit_price,
                available_size=Decimal("0"),
                available_notional=Decimal("0"),
                sufficient=False,
                levels_checked=0,
            )

        if side.upper() == "BUY":
            # Check ASK side (we're buying, so we need asks <= limit_price)
            levels = book.get("asks", [])
            return self._aggregate_levels(
                token_id=token_id,
                side=side,
                limit_price=limit_price,
                levels=levels,
                compare_fn=lambda level_price: level_price <= limit_price,
            )
        else:
            # Check BID side (we're selling, so we need bids >= limit_price)
            levels = book.get("bids", [])
            return self._aggregate_levels(
                token_id=token_id,
                side=side,
                limit_price=limit_price,
                levels=levels,
                compare_fn=lambda level_price: level_price >= limit_price,
            )

    def check_trades(
        self,
        trades: list[dict[str, Any]],
    ) -> tuple[bool, list[DepthCheck]]:
        """Check depth for a list of trades.

        Args:
            trades: List of dicts with token_id, side, price, size keys

        Returns:
            Tuple of (all_sufficient, list_of_checks)
        """
        checks = []
        for trade in trades:
            check = self.check_depth(
                token_id=trade["token_id"],
                side=trade["side"],
                limit_price=Decimal(str(trade["price"])),
                required_size=Decimal(str(trade["size"])),
            )
            checks.append(check)

        all_sufficient = all(c.sufficient for c in checks)
        return all_sufficient, checks

    def clear_cache(self) -> None:
        """Clear the order book cache."""
        self._cache.clear()

    def _fetch_book(self, token_id: str) -> dict[str, Any] | None:
        """Fetch order book from CLOB API with caching."""
        if token_id in self._cache:
            return self._cache[token_id]

        try:
            url = f"{self.api_base}/book"
            params = {"token_id": token_id}
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            book = resp.json()
            self._cache[token_id] = book
            return book
        except Exception as e:
            log.warning("Failed to fetch order book for %s: %s", token_id[:12], e)
            return None

    def _aggregate_levels(
        self,
        *,
        token_id: str,
        side: str,
        limit_price: Decimal,
        levels: list,
        compare_fn,
    ) -> DepthCheck:
        """Aggregate available liquidity across price levels."""
        total_size = Decimal("0")
        total_notional = Decimal("0")
        levels_checked = 0

        for level in levels:
            # Levels can be [price, size] or {"price": ..., "size": ...}
            if isinstance(level, list) and len(level) >= 2:
                price = Decimal(str(level[0]))
                size = Decimal(str(level[1]))
            elif isinstance(level, dict):
                price = Decimal(str(level.get("price", 0)))
                size = Decimal(str(level.get("size", 0)))
            else:
                continue

            if not compare_fn(price):
                continue

            total_size += size
            total_notional += price * size
            levels_checked += 1

        sufficient = total_notional >= self.min_depth_usdc

        if not sufficient:
            log.debug(
                "Insufficient depth for %s %s: available=$%.2f, required=$%.2f",
                side,
                token_id[:12],
                float(total_notional),
                float(self.min_depth_usdc),
            )

        return DepthCheck(
            token_id=token_id,
            side=side,
            limit_price=limit_price,
            available_size=total_size,
            available_notional=total_notional,
            sufficient=sufficient,
            levels_checked=levels_checked,
        )
