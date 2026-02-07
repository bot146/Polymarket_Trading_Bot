"""Paper trading primitives.

The current paper mode in `UnifiedExecutor` assumes every signal is filled as
specified, which is fine for taker-style strategies but unrealistic for maker
quoting (GTC) strategies.

This module introduces a small in-memory blotter + fill model that can be
incrementally integrated into the executor.

Design goals:
- No network calls.
- Deterministic and unit-testable.
- Keep the public surface tiny so we can iterate without churn.

Contract (v0):
- `PaperOrder` represents a single submitted order. It may be partially filled.
- `PaperBlotter` stores open orders.
- `PaperBlotter.update_market(token_id, best_bid, best_ask)` can generate fills
  when the market crosses the order price.

Queue position modeling (v1):
- `fill_probability`: chance a maker order fills when the market crosses its price
  (simulates being at the back of the queue).
- `require_volume_cross`: if True, only fill when we see *through* the price level
  (i.e., BUY fills when best_ask < order.price, not <=).

This intentionally doesn't try to perfectly model Polymarket matching.
It's just enough realism to stop maker orders from magically filling.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import time
from typing import Iterable


class PaperOrderStatus(str, Enum):
    OPEN = "open"
    FILLED = "filled"
    CANCELED = "canceled"


@dataclass
class PaperFill:
    order_id: str
    token_id: str
    side: str  # BUY|SELL
    fill_price: Decimal
    fill_size: Decimal


@dataclass
class PaperOrder:
    order_id: str
    token_id: str
    side: str  # BUY|SELL
    price: Decimal
    size: Decimal
    order_type: str  # FOK|IOC|GTC
    condition_id: str | None = None
    filled_size: Decimal = Decimal("0")
    status: PaperOrderStatus = PaperOrderStatus.OPEN
    created_ts: float = 0.0

    @property
    def remaining(self) -> Decimal:
        return max(Decimal("0"), self.size - self.filled_size)

    def is_open(self) -> bool:
        return self.status == PaperOrderStatus.OPEN and self.remaining > 0


class PaperBlotter:
    """In-memory order blotter for paper mode.

    Parameters:
        fill_probability: probability (0–1) that a resting maker GTC order fills
            when the market crosses its price.  Default 1.0 = always fill on cross
            (v0 behaviour).  Lower values simulate queue position.
        require_volume_cross: if *True*, BUY GTC orders only fill when
            best_ask **strictly less** than order price (not equal), simulating
            that someone has to trade *through* the price level for a back-of-
            queue order to be reached.  Similarly for SELL.
    """

    def __init__(
        self,
        *,
        fill_probability: float = 1.0,
        require_volume_cross: bool = False,
    ) -> None:
        self._next_id = 1
        self._orders: dict[str, PaperOrder] = {}
        self._best_bid: dict[str, Decimal | None] = {}
        self._best_ask: dict[str, Decimal | None] = {}
        self._fill_probability = max(0.0, min(1.0, fill_probability))
        self._require_volume_cross = require_volume_cross

    def submit(
        self,
        *,
        token_id: str,
        side: str,
        price: Decimal,
        size: Decimal,
        order_type: str,
        condition_id: str | None = None,
    ) -> PaperOrder:
        order_id = f"paper_{self._next_id}"
        self._next_id += 1
        order = PaperOrder(
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            order_type=order_type,
            condition_id=condition_id,
            created_ts=time.time(),
        )
        self._orders[order_id] = order
        return order

    def get_last_top_of_book(self, *, token_id: str) -> tuple[Decimal | None, Decimal | None]:
        return self._best_bid.get(token_id), self._best_ask.get(token_id)

    def cancel_stale_gtc_orders(
        self,
        *,
        token_id: str,
        max_price_distance: Decimal,
        max_age_seconds: float | None = None,
    ) -> list[str]:
        """Cancel open GTC orders for token_id if they are stale.

        Stale definition (simple):
        - If we know best_bid/best_ask:
          - BUY order: cancel if |order.price - best_bid| > max_price_distance
          - SELL order: cancel if |order.price - best_ask| > max_price_distance
        - Additionally, if max_age_seconds is provided, cancel if now-created_ts > max_age_seconds.
        """
        now = time.time()
        best_bid, best_ask = self.get_last_top_of_book(token_id=token_id)

        canceled: list[str] = []
        for o in list(self.iter_open_orders()):
            if o.token_id != token_id or o.order_type != "GTC":
                continue

            if max_age_seconds is not None and o.created_ts and (now - o.created_ts) > max_age_seconds:
                if self.cancel(o.order_id):
                    canceled.append(o.order_id)
                continue

            # If we don't know the book, we can't judge price-staleness.
            if best_bid is None and best_ask is None:
                continue

            ref = best_bid if o.side == "BUY" else best_ask
            if ref is None:
                continue

            if abs(o.price - ref) > max_price_distance:
                if self.cancel(o.order_id):
                    canceled.append(o.order_id)

        return canceled

    def open_gtc_orders_for_condition(self, condition_id: str) -> list[PaperOrder]:
        return [
            o
            for o in self._orders.values()
            if o.order_type == "GTC" and o.is_open() and o.condition_id == condition_id
        ]

    def open_gtc_token_ids_for_condition(self, condition_id: str) -> list[str]:
        """Return token_ids that currently have open maker orders for condition_id."""
        token_ids: list[str] = []
        for o in self.open_gtc_orders_for_condition(condition_id):
            token_ids.append(o.token_id)
        # preserve order while de-duping
        return list(dict.fromkeys(token_ids))

    def known_gtc_token_ids_for_condition(self, condition_id: str) -> list[str]:
        """Return token_ids that have EVER had a GTC order for this condition.

        This is used by the requote engine so it can re-establish quotes even
        if staleness cancellation removed all currently-open orders.
        """
        token_ids: list[str] = []
        for o in self._orders.values():
            if o.order_type == "GTC" and o.condition_id == condition_id:
                token_ids.append(o.token_id)
        return list(dict.fromkeys(token_ids))

    def cancel_stale_gtc_orders_for_condition(
        self,
        *,
        condition_id: str,
        max_price_distance: Decimal,
        max_age_seconds: float | None = None,
    ) -> list[str]:
        """Cancel stale GTC orders for all tokens participating in condition_id."""
        canceled: list[str] = []
        for token_id in self.open_gtc_token_ids_for_condition(condition_id):
            canceled.extend(
                self.cancel_stale_gtc_orders(
                    token_id=token_id,
                    max_price_distance=max_price_distance,
                    max_age_seconds=max_age_seconds,
                )
            )
        return canceled

    def get_reference_gtc_size_for_condition(self, *, condition_id: str) -> dict[str, Decimal]:
        """Best-effort per-token reference sizes for condition-level requoting."""
        sizes: dict[str, Decimal] = {}
        token_ids = self.open_gtc_token_ids_for_condition(condition_id)
        for token_id in token_ids:
            ref = self.get_reference_gtc_size(token_id=token_id)
            if ref is not None:
                sizes[token_id] = ref
        return sizes

    def cancel(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if not order or order.status != PaperOrderStatus.OPEN:
            return False
        order.status = PaperOrderStatus.CANCELED
        return True

    def iter_open_orders(self) -> Iterable[PaperOrder]:
        return (o for o in self._orders.values() if o.is_open())

    def get(self, order_id: str) -> PaperOrder | None:
        return self._orders.get(order_id)

    def get_reference_gtc_size(self, *, token_id: str) -> Decimal | None:
        """Return a best-effort reference size for a token's maker quoting."""
        for o in reversed(list(self._orders.values())):
            if o.token_id == token_id and o.order_type == "GTC":
                return o.size
        return None

    def get_reference_gtc_condition_id(self, *, token_id: str) -> str | None:
        """Return a best-effort reference condition_id for a token's maker orders."""
        for o in reversed(list(self._orders.values())):
            if o.token_id == token_id and o.order_type == "GTC" and o.condition_id:
                return o.condition_id
        return None

    def update_market(self, *, token_id: str, best_bid: Decimal | None, best_ask: Decimal | None) -> list[PaperFill]:
        """Update market and fill any now-marketable GTC orders.

        Rules (with queue-position modelling):
        - BUY fills if best_ask crosses the order price.
        - SELL fills if best_bid crosses the order price.
        - If `require_volume_cross` is True, crossing is *strict* (< / >).
          Otherwise crossing includes equality (<= / >=).
        - Even when crossed, the order only fills with probability
          `fill_probability` (simulates queue position).

        We fill the *entire* remaining size once marketable.
        """
        fills: list[PaperFill] = []
        if best_bid is None and best_ask is None:
            return fills

        # Track most recent top-of-book snapshot for staleness logic.
        self._best_bid[token_id] = best_bid
        self._best_ask[token_id] = best_ask

        for order in list(self.iter_open_orders()):
            if order.token_id != token_id:
                continue
            if order.order_type != "GTC":
                continue

            crossed = False
            fill_price_val: Decimal | None = None

            if order.side == "BUY" and best_ask is not None:
                if self._require_volume_cross:
                    crossed = best_ask < order.price
                else:
                    crossed = best_ask <= order.price
                fill_price_val = best_ask

            elif order.side == "SELL" and best_bid is not None:
                if self._require_volume_cross:
                    crossed = best_bid > order.price
                else:
                    crossed = best_bid >= order.price
                fill_price_val = best_bid

            if not crossed or fill_price_val is None:
                continue

            # Probabilistic fill — simulates queue position.
            if self._fill_probability < 1.0 and random.random() > self._fill_probability:
                continue

            fill_size = order.remaining
            order.filled_size += fill_size
            order.status = PaperOrderStatus.FILLED
            fills.append(
                PaperFill(
                    order_id=order.order_id,
                    token_id=order.token_id,
                    side=order.side,
                    fill_price=fill_price_val,
                    fill_size=fill_size,
                )
            )

        return fills
