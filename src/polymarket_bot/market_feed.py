"""Enhanced WebSocket integration for real-time market data.

This module provides better integration between WebSocket feeds and
the strategy orchestrator, ensuring strategies have real-time bid/ask data.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from polymarket_bot.wss import MarketWssClient

log = logging.getLogger(__name__)


class EnhancedMarketFeed:
    """Enhanced market data feed with better bid/ask tracking."""

    def __init__(self, asset_ids: list[str]):
        self.asset_ids = asset_ids
        self.wss = MarketWssClient(asset_ids=asset_ids)
        
        # Enhanced data structures
        self.best_bid: dict[str, float] = {}
        self.best_ask: dict[str, float] = {}
        self.mid_price: dict[str, float] = {}
        self.spread: dict[str, float] = {}
        self.last_update: dict[str, float] = {}
        
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the WebSocket feed."""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info(f"Enhanced market feed started for {len(self.asset_ids)} assets")

    def stop(self) -> None:
        """Stop the WebSocket feed."""
        self._running = False
        if self.wss:
            self.wss.stop()

    def _run(self) -> None:
        """Run the WebSocket client."""
        self.wss.run_forever()

    def get_market_data(self) -> dict[str, Any]:
        """Get current market data snapshot.
        
        Returns:
            Dict with bid/ask data for all tracked assets.

        Thread-safety:
            The underlying ``MarketWssClient`` mutates ``best_bid`` / ``best_ask``
            dicts from the WebSocket reader thread.  We take a snapshot under the
            lock to avoid torn reads.
        """
        with self._lock:
            # Snapshot the dicts atomically (dict.copy is fast).
            try:
                raw_bid = dict(self.wss.best_bid)
                raw_ask = dict(self.wss.best_ask)
            except Exception:
                raw_bid = {}
                raw_ask = {}

            self.best_bid.update(raw_bid)
            self.best_ask.update(raw_ask)
            
            # Calculate mid prices and spreads
            for asset_id in self.asset_ids:
                bid = self.best_bid.get(asset_id)
                ask = self.best_ask.get(asset_id)
                
                if bid is not None and ask is not None:
                    self.mid_price[asset_id] = (bid + ask) / 2
                    self.spread[asset_id] = ask - bid
            
            return {
                "best_bid": dict(self.best_bid),
                "best_ask": dict(self.best_ask),
                "mid_price": dict(self.mid_price),
                "spread": dict(self.spread),
            }

    def add_assets(self, asset_ids: list[str]) -> None:
        """Add more assets to track (requires restart)."""
        for asset_id in asset_ids:
            if asset_id not in self.asset_ids:
                self.asset_ids.append(asset_id)
        log.info(f"Asset tracking expanded to {len(self.asset_ids)} assets")

    def is_data_ready(self, asset_id: str) -> bool:
        """Check if we have valid bid/ask data for an asset."""
        with self._lock:
            return (
                asset_id in self.best_bid and
                asset_id in self.best_ask and
                self.best_bid[asset_id] is not None and
                self.best_ask[asset_id] is not None
            )

    def get_spread_bps(self, asset_id: str) -> float | None:
        """Get spread in basis points for an asset."""
        with self._lock:
            if not (
                asset_id in self.best_bid and
                asset_id in self.best_ask and
                self.best_bid[asset_id] is not None and
                self.best_ask[asset_id] is not None
            ):
                return None

            bid = self.best_bid[asset_id]
            ask = self.best_ask[asset_id]

        mid = (bid + ask) / 2
        
        if mid <= 0:
            return None
        
        spread = ask - bid
        return (spread / mid) * 10000  # Convert to basis points
