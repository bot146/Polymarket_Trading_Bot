from decimal import Decimal
from typing import Any, cast

from polymarket_bot.orchestrator import OrchestratorConfig, StrategyOrchestrator


class _FakeToken:
    def __init__(self, token_id: str, outcome: str, price: float, volume: float = 0.0):
        self.token_id = token_id
        self.outcome = outcome
        self.price = price
        self.volume = volume


class _FakeMarket:
    def __init__(self, condition_id: str, question: str, tokens: list[_FakeToken], volume: float = 0.0, active: bool = True):
        self.condition_id = condition_id
        self.question = question
        self.tokens = tokens
        self.volume = volume
        self.active = active
        self.neg_risk_market_id = None
        self.group_item_title = None
        self.end_date = None
        self.liquidity = 0.0
        self.spread = None
        self.one_day_price_change = None
        self.rewards_min_size = None
        self.rewards_max_spread = None
        self.rewards_daily_rate = None


class _FakeScanner:
    def __init__(self, markets: list[_FakeMarket]):
        self._markets = markets

    def get_high_volume_markets(self, min_volume, limit=None):
        return self._markets

    def get_resolved_markets(self, limit=None):
        return []


class _FakeFeed:
    def __init__(self, best_bid: dict[str, float], best_ask: dict[str, float]):
        self._best_bid = best_bid
        self._best_ask = best_ask

    def start(self):
        return None

    def get_market_data(self):
        return {"best_bid": dict(self._best_bid), "best_ask": dict(self._best_ask)}


class _Settings:
    # minimal Settings surface the orchestrator touches
    market_fetch_limit = 0
    min_edge_cents = 1
    edge_buffer_cents = 0
    max_order_usdc = 5
    min_market_volume = 0
    # fee rates used by strategy constructors
    maker_fee_rate = Decimal("0")
    taker_fee_rate = Decimal("0")
    # oracle sniping
    enable_oracle_sniping = False
    oracle_min_confidence = Decimal("0.7")
    # copy trading
    enable_copy_trading = False
    whale_min_trade_usdc = Decimal("500")
    whale_addresses = ""


def test_orchestrator_prefers_wss_best_ask_over_gamma_price(monkeypatch):
    cfg = OrchestratorConfig(enable_arbitrage=False, enable_guaranteed_win=False, enable_stat_arb=False, enable_sniping=False)
    orch = StrategyOrchestrator(cast(Any, _Settings()), cfg)

    markets = [
        _FakeMarket(
            condition_id="c1",
            question="Q",
            volume=1000.0,
            tokens=[
                _FakeToken(token_id="t1", outcome="YES", price=0.55, volume=100.0),
                _FakeToken(token_id="t2", outcome="NO", price=0.45, volume=100.0),
            ],
        )
    ]

    orch.scanner = cast(Any, _FakeScanner(markets))
    orch._feed_started = True
    orch._feed = cast(Any, _FakeFeed(best_bid={"t1": 0.50, "t2": 0.40}, best_ask={"t1": 0.51, "t2": 0.41}))

    data = orch._gather_market_data()
    assert data["markets"], "expected at least one market"

    t1 = next(t for t in data["markets"][0]["tokens"] if t["token_id"] == "t1")
    assert t1["best_ask"] == 0.51
    assert t1["best_bid"] == 0.50


def test_orchestrator_falls_back_to_gamma_price_when_no_best_ask(monkeypatch):
    cfg = OrchestratorConfig(enable_arbitrage=False, enable_guaranteed_win=False, enable_stat_arb=False, enable_sniping=False)
    orch = StrategyOrchestrator(cast(Any, _Settings()), cfg)

    markets = [
        _FakeMarket(
            condition_id="c1",
            question="Q",
            volume=1000.0,
            tokens=[
                _FakeToken(token_id="t1", outcome="YES", price=0.55, volume=100.0),
            ],
        )
    ]

    orch.scanner = cast(Any, _FakeScanner(markets))
    orch._feed_started = True
    orch._feed = cast(Any, _FakeFeed(best_bid={}, best_ask={}))

    data = orch._gather_market_data()
    t1 = data["markets"][0]["tokens"][0]
    assert t1["best_ask"] == 0.55
    # best_bid now also falls back to Gamma price (same as best_ask) so strategies
    # that require both sides (market-making, sniping) aren't starved.
    assert t1["best_bid"] == 0.55
