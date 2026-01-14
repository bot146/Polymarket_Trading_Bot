from decimal import Decimal
import time

from polymarket_bot.paper_trading import PaperBlotter


def test_gtc_does_not_fill_until_cross() -> None:
    blotter = PaperBlotter()

    order = blotter.submit(
        token_id="t1",
        side="BUY",
        price=Decimal("0.40"),
        size=Decimal("10"),
        order_type="GTC",
    )

    # Market is above our bid -> no fill.
    fills = blotter.update_market(token_id="t1", best_bid=Decimal("0.39"), best_ask=Decimal("0.41"))
    assert fills == []
    assert order.is_open()

    # Market crosses down to our limit -> fills at best ask.
    fills = blotter.update_market(token_id="t1", best_bid=Decimal("0.39"), best_ask=Decimal("0.40"))
    assert len(fills) == 1
    assert fills[0].order_id == order.order_id
    assert fills[0].fill_price == Decimal("0.40")
    assert fills[0].fill_size == Decimal("10")

    assert not order.is_open()


def test_cancel_stale_gtc_orders_by_price_and_age() -> None:
    blotter = PaperBlotter()

    # Seed a book snapshot.
    blotter.update_market(token_id="t1", best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))

    # A stale BUY far below best bid.
    o1 = blotter.submit(
        token_id="t1",
        side="BUY",
        price=Decimal("0.40"),
        size=Decimal("10"),
        order_type="GTC",
    )

    canceled = blotter.cancel_stale_gtc_orders(token_id="t1", max_price_distance=Decimal("0.02"))
    assert o1.order_id in canceled
    assert not o1.is_open()

    # Age-based cancel
    o2 = blotter.submit(
        token_id="t1",
        side="BUY",
        price=Decimal("0.50"),
        size=Decimal("10"),
        order_type="GTC",
    )
    # Force order to look old.
    o2.created_ts = time.time() - 100

    canceled2 = blotter.cancel_stale_gtc_orders(
        token_id="t1",
        max_price_distance=Decimal("0.50"),
        max_age_seconds=1.0,
    )
    assert o2.order_id in canceled2
    assert not o2.is_open()
