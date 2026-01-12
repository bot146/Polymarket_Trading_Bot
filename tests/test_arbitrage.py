from decimal import Decimal

from polymarket_bot.arbitrage import compute_hedge_opportunity


def test_compute_hedge_opportunity_edge_positive():
    opp = compute_hedge_opportunity(
        yes_token_id="YES",
        no_token_id="NO",
        yes_ask=Decimal("0.48"),
        no_ask=Decimal("0.49"),
    )
    assert opp.total_cost == Decimal("0.97")
    assert opp.edge == Decimal("0.03")


def test_compute_hedge_opportunity_edge_negative():
    opp = compute_hedge_opportunity(
        yes_token_id="YES",
        no_token_id="NO",
        yes_ask=Decimal("0.51"),
        no_ask=Decimal("0.50"),
    )
    assert opp.edge == Decimal("-0.01")
