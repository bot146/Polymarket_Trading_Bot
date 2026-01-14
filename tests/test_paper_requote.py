from __future__ import annotations

from decimal import Decimal

from polymarket_bot.config import Settings
from polymarket_bot.unified_executor import UnifiedExecutor


def test_paper_requote_places_fresh_quotes_after_cancel() -> None:
    settings = Settings(
        trading_mode="paper",
        kill_switch=False,
        enable_paper_requote=True,
        requote_max_age_seconds=0.0,
        requote_max_distance=Decimal("0.0"),
        requote_cooldown_ms=0,
    )
    ex = UnifiedExecutor(client=None, settings=settings, position_manager=None)

    # Place an initial maker order we expect to be immediately stale via age.
    ex.paper_blotter.submit(
        token_id="t1",
        side="BUY",
        price=Decimal("0.40"),
        size=Decimal("10"),
        order_type="GTC",
        condition_id="c1",
    )

    # Feed a top-of-book update; stale cancels should occur, then requote should place.
    ex.on_market_update(token_id="t1", best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))

    open_orders = [o for o in ex.paper_blotter.iter_open_orders() if o.token_id == "t1" and o.order_type == "GTC"]
    # We should have at least one refreshed quote.
    assert len(open_orders) >= 1
    prices = {o.side: o.price for o in open_orders}
    assert prices.get("BUY") == Decimal("0.50")
    assert prices.get("SELL") == Decimal("0.51")
    assert ex.paper_orders_requoted >= 1


def test_paper_requote_pairs_by_condition() -> None:
    settings = Settings(
        trading_mode="paper",
        kill_switch=False,
        enable_paper_requote=True,
        requote_max_age_seconds=0.0,
        requote_max_distance=Decimal("0.0"),
        requote_cooldown_ms=0,
    )
    ex = UnifiedExecutor(client=None, settings=settings, position_manager=None)

    # Seed initial maker orders for BOTH tokens in the condition.
    ex.paper_blotter.submit(
        token_id="t_yes",
        side="BUY",
        price=Decimal("0.40"),
        size=Decimal("10"),
        order_type="GTC",
        condition_id="c1",
    )
    ex.paper_blotter.submit(
        token_id="t_no",
        side="BUY",
        price=Decimal("0.60"),
        size=Decimal("10"),
        order_type="GTC",
        condition_id="c1",
    )

    # Provide top-of-book for both.
    ex.on_market_update(token_id="t_yes", best_bid=Decimal("0.41"), best_ask=Decimal("0.42"))
    ex.on_market_update(token_id="t_no", best_bid=Decimal("0.58"), best_ask=Decimal("0.59"))

    # Trigger staleness via age on YES update: should cause paired requote.
    ex.on_market_update(token_id="t_yes", best_bid=Decimal("0.41"), best_ask=Decimal("0.42"))

    open_yes = [o for o in ex.paper_blotter.iter_open_orders() if o.token_id == "t_yes" and o.order_type == "GTC" and o.condition_id == "c1"]
    open_no = [o for o in ex.paper_blotter.iter_open_orders() if o.token_id == "t_no" and o.order_type == "GTC" and o.condition_id == "c1"]
    assert len(open_yes) >= 1
    assert len(open_no) >= 1

    yes_prices = {o.side: o.price for o in open_yes}
    no_prices = {o.side: o.price for o in open_no}
    assert yes_prices.get("BUY") == Decimal("0.41")
    assert yes_prices.get("SELL") == Decimal("0.42")
    assert no_prices.get("BUY") == Decimal("0.58")
    assert no_prices.get("SELL") == Decimal("0.59")


def test_paper_requote_cooldown_prevents_thrashing() -> None:
    settings = Settings(
        trading_mode="paper",
        kill_switch=False,
        enable_paper_requote=True,
        requote_max_age_seconds=0.0,
        requote_max_distance=Decimal("0.0"),
        requote_cooldown_ms=10_000,
    )
    ex = UnifiedExecutor(client=None, settings=settings, position_manager=None)

    ex.paper_blotter.submit(
        token_id="t1",
        side="BUY",
        price=Decimal("0.40"),
        size=Decimal("10"),
        order_type="GTC",
    )

    ex.on_market_update(token_id="t1", best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    first = ex.paper_orders_requoted

    # Immediate second update should not requote due to cooldown.
    ex.on_market_update(token_id="t1", best_bid=Decimal("0.52"), best_ask=Decimal("0.53"))
    assert ex.paper_orders_requoted == first
