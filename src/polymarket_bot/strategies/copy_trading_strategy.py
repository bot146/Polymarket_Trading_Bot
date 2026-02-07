"""Copy / whale-following trading strategy.

Monitors large Polymarket trades on the Polygon blockchain and follows them
when the trade size exceeds a configurable threshold.

Data sources:
- Polygonscan API (free tier, requires API key in POLYGONSCAN_API_KEY env var)
- Polymarket CLOB activity endpoint (public, rate-limited)

The strategy keeps a rolling buffer of recent large trades and emits signals
when a whale buys a YES or NO outcome aggressively.
"""

from __future__ import annotations

import logging
import os
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

# Polymarket's USDC-conditional token exchange contract on Polygon.
POLYMARKET_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# Public Gamma Markets API for recent trades.
GAMMA_ACTIVITY_URL = "https://gamma-api.polymarket.com/activity"


@dataclass(frozen=True)
class WhaleTrade:
    """A detected large trade on Polymarket."""
    timestamp: float
    address: str
    condition_id: str
    token_id: str
    outcome: str  # YES or NO
    side: str  # BUY or SELL
    size: Decimal
    price: Decimal
    usdc_value: Decimal


class CopyTradingStrategy(Strategy):
    """Follow large traders on Polymarket.

    How it works:
    1. Every scan cycle, fetch recent Polymarket trades from the Gamma activity
       API (public, no auth needed).
    2. Filter for trades above ``min_trade_usdc`` threshold.
    3. Optionally filter for specific whale addresses.
    4. Emit a BUY signal following the whale's direction.

    The signal uses GTC (maker) orders to avoid paying taker fees.

    Parameters:
        min_trade_usdc: Minimum trade value to consider following.
        max_order_usdc: Max size of our copy order.
        whale_addresses: Set of addresses to specifically follow (empty = follow all large trades).
        taker_fee_rate: For P&L estimation.
        lookback_seconds: How far back to look for whale trades.
    """

    def __init__(
        self,
        min_trade_usdc: Decimal = Decimal("1000"),
        max_order_usdc: Decimal = Decimal("30"),
        whale_addresses: set[str] | None = None,
        taker_fee_rate: Decimal = Decimal("0.02"),
        lookback_seconds: float = 120.0,
        enabled: bool = True,
    ) -> None:
        super().__init__(name="copy_trading", enabled=enabled)
        self.min_trade_usdc = min_trade_usdc
        self.max_order_usdc = max_order_usdc
        self.whale_addresses = {a.lower() for a in (whale_addresses or set())}
        self.taker_fee_rate = taker_fee_rate
        self.lookback_seconds = lookback_seconds

        self._last_fetch_ts = 0.0
        self._fetch_interval = 15.0  # Don't hit the API more than every 15s
        self._seen_trade_ids: set[str] = set()
        self._recent_whale_trades: list[WhaleTrade] = []

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def scan(self, market_data: dict[str, Any]) -> list[StrategySignal]:
        signals: list[StrategySignal] = []

        now = time.time()
        if now - self._last_fetch_ts < self._fetch_interval:
            return signals  # Throttle API calls

        self._last_fetch_ts = now

        # Fetch recent activity
        whale_trades = self._fetch_recent_whale_trades()
        if not whale_trades:
            return signals

        # Build a map of markets from the scan data for metadata enrichment.
        market_by_cid: dict[str, dict] = {}
        for m in market_data.get("markets", []):
            cid = m.get("condition_id")
            if cid:
                market_by_cid[cid] = m

        for wt in whale_trades:
            # Only follow BUY signals (whale buying = bullish signal)
            if wt.side != "BUY":
                continue

            # Price sanity: don't follow if whale is buying near $1
            if wt.price >= Decimal("0.95"):
                continue

            # Don't follow if price is near $0 (too risky)
            if wt.price <= Decimal("0.05"):
                continue

            # Calculate our copy order
            copy_price = wt.price  # Match the whale's entry
            if copy_price <= 0:
                continue
            copy_size = (self.max_order_usdc / copy_price).quantize(Decimal("0.01"))
            if copy_size <= 0:
                continue

            # Expected profit assumes convergence to fair value
            # Conservative: assume 5% edge
            fee = copy_price * self.taker_fee_rate
            expected_edge = copy_price * Decimal("0.05")
            expected_profit = (expected_edge - fee) * copy_size

            if expected_profit <= 0:
                continue

            # Build market metadata
            market = market_by_cid.get(wt.condition_id, {})
            question = market.get("question", "unknown")

            opportunity = Opportunity(
                strategy_type=StrategyType.COPY_TRADING,
                expected_profit=expected_profit,
                confidence=Decimal("0.55"),  # Lower confidence — we're just following
                urgency=7,  # Moderately urgent
                metadata={
                    "condition_id": wt.condition_id,
                    "whale_address": wt.address,
                    "whale_size_usdc": float(wt.usdc_value),
                    "whale_outcome": wt.outcome,
                    "whale_price": float(wt.price),
                    "question": question,
                    "token_id": wt.token_id,
                    "strategy_sub_type": "copy_trading",
                },
            )

            trades = [
                Trade(
                    token_id=wt.token_id,
                    side="BUY",
                    size=copy_size,
                    price=copy_price,
                    order_type="GTC",  # Maker for lower fees
                ),
            ]

            signal = StrategySignal(
                opportunity=opportunity,
                trades=trades,
                max_total_cost=copy_price * copy_size,
                min_expected_return=copy_size,
            )

            log.info(
                "🐋 WHALE COPY: %s bought $%.0f of %s @$%.4f — "
                "copying $%.2f (expected profit $%.4f)",
                wt.address[:10],
                float(wt.usdc_value),
                wt.outcome,
                float(wt.price),
                float(copy_price * copy_size),
                float(expected_profit),
            )

            signals.append(signal)

        return signals

    def validate(self, signal: StrategySignal) -> tuple[bool, str]:
        if signal.opportunity.strategy_type != StrategyType.COPY_TRADING:
            return False, "not_copy_trading"
        if len(signal.trades) != 1:
            return False, "invalid_trade_count"
        trade = signal.trades[0]
        if trade.side != "BUY":
            return False, "must_be_buy"
        if trade.price >= Decimal("0.95"):
            return False, "price_too_high"
        return True, "ok"

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _fetch_recent_whale_trades(self) -> list[WhaleTrade]:
        """Fetch recent large trades from Gamma activity API."""
        whale_trades: list[WhaleTrade] = []

        try:
            resp = requests.get(
                GAMMA_ACTIVITY_URL,
                params={"limit": "100"},
                timeout=10.0,
            )
            resp.raise_for_status()
            activities = resp.json()

            if not isinstance(activities, list):
                return whale_trades

            now = time.time()
            cutoff = now - self.lookback_seconds

            for activity in activities:
                try:
                    # Skip if we've already processed this trade
                    trade_id = activity.get("id", "")
                    if trade_id in self._seen_trade_ids:
                        continue

                    # Parse timestamp
                    ts_str = activity.get("timestamp")
                    if ts_str:
                        # Gamma uses ISO format or epoch
                        try:
                            ts = float(ts_str)
                        except (ValueError, TypeError):
                            continue
                    else:
                        continue

                    if ts < cutoff:
                        continue

                    # Parse trade details
                    side = str(activity.get("side", "")).upper()
                    if side not in {"BUY", "SELL"}:
                        continue

                    size = Decimal(str(activity.get("size", "0")))
                    price = Decimal(str(activity.get("price", "0")))
                    if size <= 0 or price <= 0:
                        continue

                    usdc_value = size * price

                    # Filter by minimum size
                    if usdc_value < self.min_trade_usdc:
                        continue

                    address = str(activity.get("maker_address", activity.get("user", ""))).lower()

                    # Filter by whale addresses if configured
                    if self.whale_addresses and address not in self.whale_addresses:
                        continue

                    condition_id = str(activity.get("condition_id", ""))
                    token_id = str(activity.get("asset_id", activity.get("token_id", "")))
                    outcome = str(activity.get("outcome", "UNKNOWN")).upper()

                    if not condition_id or not token_id:
                        continue

                    wt = WhaleTrade(
                        timestamp=ts,
                        address=address,
                        condition_id=condition_id,
                        token_id=token_id,
                        outcome=outcome,
                        side=side,
                        size=size,
                        price=price,
                        usdc_value=usdc_value,
                    )
                    whale_trades.append(wt)
                    self._seen_trade_ids.add(trade_id)

                except Exception:
                    continue

        except Exception as e:
            log.debug("Failed to fetch whale trades: %s", e)

        # Prune old seen IDs to prevent memory leak
        if len(self._seen_trade_ids) > 10000:
            self._seen_trade_ids = set(list(self._seen_trade_ids)[-5000:])

        self._recent_whale_trades = whale_trades
        return whale_trades
