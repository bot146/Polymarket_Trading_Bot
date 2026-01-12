from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from tenacity import retry, stop_after_attempt, wait_exponential_jitter
from websocket import WebSocketApp

from polymarket_bot.clob_client import ApiCreds

log = logging.getLogger(__name__)

CLOB_WSS_BASE = "wss://ws-subscriptions-clob.polymarket.com"


@dataclass(frozen=True)
class WssAuth:
    apiKey: str
    secret: str
    passphrase: str


class MarketWssClient:
    """Market-channel websocket client that maintains best bid/ask per asset.

    This is intentionally simple and defensive: reconnects on errors, pings to keep alive.
    """

    def __init__(
        self,
        asset_ids: list[str],
        on_event: Optional[Callable[[dict], None]] = None,
        url_base: str = CLOB_WSS_BASE,
        verbose: bool = False,
    ) -> None:
        self.asset_ids = list(dict.fromkeys(asset_ids))
        self.on_event = on_event
        self.url_base = url_base
        self.verbose = verbose

        self._ws: WebSocketApp | None = None
        self._stop = threading.Event()

        # best bid/ask in *price* terms (0..1)
        self.best_bid: dict[str, float] = {}
        self.best_ask: dict[str, float] = {}

    def stop(self) -> None:
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def _on_open(self, ws: WebSocketApp) -> None:
        msg = {"assets_ids": self.asset_ids, "type": "market"}
        ws.send(json.dumps(msg))

        def _ping() -> None:
            while not self._stop.is_set():
                try:
                    ws.send("PING")
                except Exception:
                    return
                time.sleep(10)

        threading.Thread(target=_ping, daemon=True).start()
        log.info("WSS connected (market). subscribed_assets=%d", len(self.asset_ids))

    def _on_message(self, ws: WebSocketApp, message: str) -> None:
        if self.verbose:
            log.debug("wss message: %s", message[:5000])

        try:
            data = json.loads(message)
        except Exception:
            return

        # Handle both dict and list responses from WebSocket
        if isinstance(data, list):
            # If data is a list, process each item
            for item in data:
                if isinstance(item, dict):
                    self._process_market_update(item)
            # Fire on_event callback with the list
            if self.on_event:
                try:
                    self.on_event(data)
                except Exception:
                    log.exception("Error in on_event callback")
            return

        # Handle dict response
        if not isinstance(data, dict):
            return

        self._process_market_update(data)

        if self.on_event:
            try:
                self.on_event(data)
            except Exception:
                log.exception("Error in on_event callback")

    def _process_market_update(self, data: dict) -> None:
        """Process a single market update from WebSocket."""
        # The exact schema can vary. We update best bid/ask opportunistically.
        # Common patterns include fields like: asset_id, bids, asks, price, changes, etc.
        asset_id = data.get("asset_id") or data.get("assetId")

        if asset_id:
            bids = data.get("bids")
            asks = data.get("asks")
            if isinstance(bids, list) and bids:
                # Each level often like [price, size] or {price, size}
                top = bids[0]
                price = top[0] if isinstance(top, list) else top.get("price")
                if price is not None:
                    self.best_bid[str(asset_id)] = float(price)

            if isinstance(asks, list) and asks:
                top = asks[0]
                price = top[0] if isinstance(top, list) else top.get("price")
                if price is not None:
                    self.best_ask[str(asset_id)] = float(price)

    def _on_error(self, ws: WebSocketApp, error: Exception) -> None:
        log.warning("WSS error: %s", error)

    def _on_close(self, ws: WebSocketApp, close_status_code: int, close_msg: str) -> None:
        log.warning("WSS closed: code=%s msg=%s", close_status_code, close_msg)

    @retry(stop=stop_after_attempt(50), wait=wait_exponential_jitter(initial=0.5, max=20))
    def run_forever(self) -> None:
        if self._stop.is_set():
            return

        url = f"{self.url_base}/ws/market"
        self._ws = WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws.run_forever()
        # If we got here without stop, tenacity will retry.
        if not self._stop.is_set():
            raise RuntimeError("Websocket disconnected")


class UserWssClient:
    """User-channel websocket client (order status updates).

    Requires auth and a list of market condition IDs to filter.
    """

    def __init__(
        self,
        market_ids: list[str],
        creds: ApiCreds,
        on_event: Optional[Callable[[dict], None]] = None,
        url_base: str = CLOB_WSS_BASE,
        verbose: bool = False,
    ) -> None:
        self.market_ids = list(dict.fromkeys(market_ids))
        self.creds = creds
        self.on_event = on_event
        self.url_base = url_base
        self.verbose = verbose

        self._ws: WebSocketApp | None = None
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def _on_open(self, ws: WebSocketApp) -> None:
        auth = {"apiKey": self.creds.api_key, "secret": self.creds.api_secret, "passphrase": self.creds.api_passphrase}
        msg = {"markets": self.market_ids, "type": "user", "auth": auth}
        ws.send(json.dumps(msg))

        def _ping() -> None:
            while not self._stop.is_set():
                try:
                    ws.send("PING")
                except Exception:
                    return
                time.sleep(10)

        threading.Thread(target=_ping, daemon=True).start()
        log.info("WSS connected (user). markets=%d", len(self.market_ids))

    def _on_message(self, ws: WebSocketApp, message: str) -> None:
        if self.verbose:
            log.debug("wss user message: %s", message[:5000])

        try:
            data = json.loads(message)
        except Exception:
            return

        if self.on_event:
            try:
                self.on_event(data)
            except Exception:
                log.exception("Error in on_event callback")

    def _on_error(self, ws: WebSocketApp, error: Exception) -> None:
        log.warning("User WSS error: %s", error)

    def _on_close(self, ws: WebSocketApp, close_status_code: int, close_msg: str) -> None:
        log.warning("User WSS closed: code=%s msg=%s", close_status_code, close_msg)

    @retry(stop=stop_after_attempt(50), wait=wait_exponential_jitter(initial=0.5, max=20))
    def run_forever(self) -> None:
        if self._stop.is_set():
            return

        url = f"{self.url_base}/ws/user"
        self._ws = WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws.run_forever()
        if not self._stop.is_set():
            raise RuntimeError("User websocket disconnected")
