from decimal import Decimal

from polymarket_bot.arbitrage import HedgeOpportunity
from polymarket_bot.config import Settings
from polymarket_bot.executor import execute_hedge


class DummyClobClient:
    """A small fake client that records create_order/post_order calls and returns predictable responses."""

    def __init__(self):
        self.created = []
        self.posted = []

    def create_order(self, order_args):
        # record the args and return a lightweight signed placeholder
        self.created.append(order_args)
        return {"signed": True, "order_args": order_args}

    def post_order(self, signed_order, orderType=None):
        # record the signed order + type and return a dict with an orderID
        self.posted.append((signed_order, orderType))
        # Return an order id that encodes the call index so assertions can be precise
        idx = len(self.posted)
        return {"orderID": f"order-{idx}-ok"}


def test_execute_hedge_posts_orders_live_mode():
    """Verify that execute_hedge calls create_order and post_order when in live mode.

    This test uses a DummyClobClient to avoid any network calls and to assert the
    executor's live code path is exercised end-to-end.
    """

    # Construct an opportunity with a positive edge so sizing proceeds
    opp = HedgeOpportunity(
        yes_token_id="YES",
        no_token_id="NO",
        yes_ask=Decimal("0.48"),
        no_ask=Decimal("0.49"),
        total_cost=Decimal("0.97"),
        edge=Decimal("0.03"),
    )

    # Live, with kill_switch disabled
    settings = Settings(trading_mode="live", kill_switch=False)

    client = DummyClobClient()

    result = execute_hedge(client=client, settings=settings, opp=opp)

    # Should report success and return order ids coming from our fake client
    assert result.placed is True
    assert result.yes_order_id == "order-1-ok"
    assert result.no_order_id == "order-2-ok"

    # Ensure the client saw two create_order and two post_order calls
    assert len(client.created) == 2
    assert len(client.posted) == 2
