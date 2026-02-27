"""Tests for short-duration momentum strategy and crypto price feed.

Tests cover:
- Market question parsing (parse_up_down_market)
- Edge calculation (maker vs taker fees)
- Confidence and urgency scoring
- Signal generation with mocked price feed
- Cooldown enforcement
- Validate() momentum reversal check
- Direction probability estimation
- PriceSnapshot analytics
"""

from __future__ import annotations

import math
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from polymarket_bot.crypto_price_feed import CryptoPriceFeed, PriceSnapshot
from polymarket_bot.strategies.short_duration_strategy import (
    ShortDurationConfig,
    ShortDurationStrategy,
    parse_up_down_market,
)
from polymarket_bot.strategy import StrategyType


# ── parse_up_down_market tests ───────────────────────────────────────


class TestParseUpDownMarket:
    def test_bitcoin_standard(self):
        q = "Bitcoin Up or Down - February 27, 5:15PM-5:20PM ET"
        assert parse_up_down_market(q) == "btc"

    def test_ethereum(self):
        assert parse_up_down_market("Ethereum Up or Down - March 1, 10AM ET") == "eth"

    def test_solana(self):
        assert parse_up_down_market("Solana Up or Down - Feb 28") == "sol"

    def test_xrp(self):
        assert parse_up_down_market("XRP Up or Down - today") == "xrp"

    def test_ripple(self):
        assert parse_up_down_market("Ripple Up or Down - tomorrow") == "xrp"

    def test_btc_abbreviation(self):
        assert parse_up_down_market("BTC Up or Down - now") == "btc"

    def test_eth_abbreviation(self):
        assert parse_up_down_market("ETH Up or Down - now") == "eth"

    def test_sol_abbreviation(self):
        assert parse_up_down_market("SOL Up or Down - now") == "sol"

    def test_case_insensitive(self):
        assert parse_up_down_market("BITCOIN UP OR DOWN - whatever") == "btc"

    def test_not_up_down(self):
        assert parse_up_down_market("Will Bitcoin hit $100k?") is None

    def test_empty(self):
        assert parse_up_down_market("") is None

    def test_partial_match(self):
        assert parse_up_down_market("Dogecoin Up or Down") is None


# ── Helpers ──────────────────────────────────────────────────────────


def _make_snapshot(
    ticker: str = "btc",
    price: float = 100000.0,
    momentum: float = 0.3,
    trend: float = 0.8,
    vol: float = 0.002,
    prob: float = 0.56,
) -> PriceSnapshot:
    return PriceSnapshot(
        ticker=ticker,
        price=price,
        timestamp=time.time(),
        ret_1m=0.001,
        ret_5m=0.003,
        ret_15m=0.005,
        ret_1h=0.01,
        momentum_score=momentum,
        volatility_1h=vol,
        trend_strength=trend,
        direction_probability=prob,
    )


def _make_market(
    condition_id: str = "cid_btc_001",
    question: str = "Bitcoin Up or Down - Feb 28, 10:00AM-10:05AM ET",
    up_price: float = 0.50,
    down_price: float = 0.50,
    active: bool = True,
    hours_ahead: float = 0.5,
) -> dict:
    """Create a synthetic short-duration market dict."""
    from datetime import datetime, timedelta, timezone

    end_dt = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "condition_id": condition_id,
        "question": question,
        "active": active,
        "end_date": end_str,
        "tokens": [
            {
                "token_id": "tok_up_001",
                "outcome": "Up",
                "price": up_price,
                "best_ask": up_price + 0.01,
                "best_bid": up_price - 0.01,
            },
            {
                "token_id": "tok_down_001",
                "outcome": "Down",
                "price": down_price,
                "best_ask": down_price + 0.01,
                "best_bid": down_price - 0.01,
            },
        ],
    }


def _make_strategy(
    snapshots: dict[str, PriceSnapshot] | None = None,
    config: ShortDurationConfig | None = None,
) -> ShortDurationStrategy:
    """Create a strategy with mocked price feed."""
    cfg = config or ShortDurationConfig()
    feed = MagicMock(spec=CryptoPriceFeed)
    feed.get_all_snapshots.return_value = snapshots or {}
    strat = ShortDurationStrategy(config=cfg, price_feed=feed, enabled=True)
    strat._snapshots = snapshots or {}
    strat._last_feed_refresh = time.time()  # Skip initial refresh
    return strat


# ── Signal generation tests ──────────────────────────────────────────


class TestShortDurationSignalGeneration:
    def test_generates_buy_up_signal_on_positive_momentum(self):
        """Positive momentum → buy 'Up' token."""
        snap = _make_snapshot(momentum=0.4, trend=0.9, prob=0.57)
        strat = _make_strategy({"btc": snap})

        market = _make_market(up_price=0.50)
        signals = strat.scan({"markets": [market]})

        assert len(signals) == 1
        sig = signals[0]
        assert sig.trades[0].side == "BUY"
        assert sig.trades[0].token_id == "tok_up_001"
        assert sig.opportunity.strategy_type == StrategyType.SHORT_DURATION
        assert sig.opportunity.metadata["favored_direction"] == "Up"
        assert sig.opportunity.metadata["ticker"] == "btc"

    def test_generates_buy_down_signal_on_negative_momentum(self):
        """Negative momentum → buy 'Down' token."""
        snap = _make_snapshot(momentum=-0.4, trend=0.9, prob=0.43)
        strat = _make_strategy({"btc": snap})

        market = _make_market(down_price=0.50)
        signals = strat.scan({"markets": [market]})

        assert len(signals) == 1
        sig = signals[0]
        assert sig.trades[0].token_id == "tok_down_001"
        assert sig.opportunity.metadata["favored_direction"] == "Down"

    def test_no_signal_when_momentum_zero(self):
        """Zero momentum → no directional signal."""
        snap = _make_snapshot(momentum=0.0, prob=0.50)
        strat = _make_strategy({"btc": snap})

        market = _make_market()
        signals = strat.scan({"markets": [market]})
        assert len(signals) == 0

    def test_no_signal_when_probability_below_threshold(self):
        """Direction probability below min_probability → skip."""
        snap = _make_snapshot(momentum=0.1, trend=0.5, prob=0.51)
        strat = _make_strategy({"btc": snap})

        market = _make_market()
        signals = strat.scan({"markets": [market]})
        assert len(signals) == 0

    def test_no_signal_for_non_up_down_market(self):
        """Markets that aren't Up/Down → skip."""
        snap = _make_snapshot()
        strat = _make_strategy({"btc": snap})

        market = _make_market(question="Will Bitcoin exceed $100k?")
        signals = strat.scan({"markets": [market]})
        assert len(signals) == 0

    def test_no_signal_for_inactive_market(self):
        """Inactive markets → skip."""
        snap = _make_snapshot()
        strat = _make_strategy({"btc": snap})

        market = _make_market(active=False)
        signals = strat.scan({"markets": [market]})
        assert len(signals) == 0

    def test_no_signal_when_too_far_from_resolution(self):
        """Markets > max_hours_to_resolution → skip."""
        snap = _make_snapshot()
        strat = _make_strategy({"btc": snap})

        market = _make_market(hours_ahead=5.0)  # 5h > default 2h max
        signals = strat.scan({"markets": [market]})
        assert len(signals) == 0

    def test_respects_max_signals_per_scan(self):
        """Cap at max_signals_per_scan."""
        snap = _make_snapshot(momentum=0.5, trend=1.0, prob=0.58)
        cfg = ShortDurationConfig(max_signals_per_scan=2)
        strat = _make_strategy({"btc": snap}, config=cfg)

        markets = [
            _make_market(condition_id=f"cid_{i}")
            for i in range(5)
        ]
        signals = strat.scan({"markets": markets})
        assert len(signals) <= 2


# ── Cooldown tests ───────────────────────────────────────────────────


class TestCooldown:
    def test_cooldown_prevents_duplicate_signal(self):
        """Same market within cooldown → no second signal."""
        snap = _make_snapshot(momentum=0.5, trend=0.9, prob=0.57)
        strat = _make_strategy({"btc": snap})

        market = _make_market()

        signals1 = strat.scan({"markets": [market]})
        assert len(signals1) == 1

        signals2 = strat.scan({"markets": [market]})
        assert len(signals2) == 0

    def test_different_markets_not_blocked(self):
        """Different condition_ids → independent cooldowns."""
        snap = _make_snapshot(momentum=0.5, trend=0.9, prob=0.57)
        strat = _make_strategy({"btc": snap})

        m1 = _make_market(condition_id="cid_1")
        m2 = _make_market(condition_id="cid_2")

        signals1 = strat.scan({"markets": [m1]})
        signals2 = strat.scan({"markets": [m2]})
        assert len(signals1) == 1
        assert len(signals2) == 1


# ── Fee & edge calculation tests ─────────────────────────────────────


class TestEdgeCalculation:
    def test_maker_fee_makes_trade_viable(self):
        """At maker fee rate (0.5%), a 53% probability is enough edge."""
        snap = _make_snapshot(momentum=0.3, trend=0.8, prob=0.56)
        cfg = ShortDurationConfig(
            prefer_maker=True,
            min_edge_cents=Decimal("0.5"),
            min_probability=Decimal("0.53"),
        )
        strat = _make_strategy({"btc": snap}, config=cfg)

        market = _make_market(up_price=0.50)
        signals = strat.scan({"markets": [market]})
        assert len(signals) == 1

        # Edge should be > 0
        edge_cents = signals[0].opportunity.metadata["edge_cents"]
        assert edge_cents > 0.5

    def test_taker_fee_kills_small_edge(self):
        """At taker rate (10%), a 53% probability is NOT enough edge."""
        snap = _make_snapshot(momentum=0.2, trend=0.6, prob=0.53)
        cfg = ShortDurationConfig(
            prefer_maker=False,  # Forces taker (10% fee)
            min_edge_cents=Decimal("0.5"),
        )
        strat = _make_strategy({"btc": snap}, config=cfg)

        market = _make_market(up_price=0.50)
        signals = strat.scan({"markets": [market]})
        # 53% prob - 50¢ price - 5¢ fee = -2¢ → no signal
        assert len(signals) == 0

    def test_maker_order_type_is_gtc(self):
        """Maker preference → GTC order type."""
        snap = _make_snapshot(momentum=0.5, trend=0.9, prob=0.57)
        cfg = ShortDurationConfig(prefer_maker=True)
        strat = _make_strategy({"btc": snap}, config=cfg)

        market = _make_market(up_price=0.50)
        signals = strat.scan({"markets": [market]})
        assert len(signals) == 1
        assert signals[0].trades[0].order_type == "GTC"

    def test_taker_order_type_is_fok(self):
        """Taker mode → FOK order type."""
        snap = _make_snapshot(momentum=0.8, trend=1.0, prob=0.60)
        cfg = ShortDurationConfig(prefer_maker=False)
        strat = _make_strategy({"btc": snap}, config=cfg)

        market = _make_market(up_price=0.45)  # Lower price for taker edge
        signals = strat.scan({"markets": [market]})
        assert len(signals) == 1
        assert signals[0].trades[0].order_type == "FOK"

    def test_maker_limit_price_one_tick_below_ask(self):
        """Maker orders place limit at best_ask - $0.01."""
        snap = _make_snapshot(momentum=0.5, trend=0.9, prob=0.57)
        cfg = ShortDurationConfig(prefer_maker=True)
        strat = _make_strategy({"btc": snap}, config=cfg)

        market = _make_market(up_price=0.50)
        # best_ask = 0.51
        signals = strat.scan({"markets": [market]})
        assert len(signals) == 1
        # market_price = best_ask (0.51), limit = 0.51 - 0.01 = 0.50
        assert signals[0].trades[0].price == Decimal("0.50")


# ── Confidence & urgency tests ───────────────────────────────────────


class TestConfidenceAndUrgency:
    def test_high_trend_gives_high_confidence(self):
        """trend_strength=1.0, strong momentum → confidence > 0.6."""
        snap = _make_snapshot(momentum=0.8, trend=1.0, vol=0.001, prob=0.60)
        conf = ShortDurationStrategy._compute_confidence(snap, Decimal("0.60"))
        assert conf > Decimal("0.60")

    def test_low_trend_gives_lower_confidence(self):
        """trend_strength=0.3 → reduced confidence."""
        snap = _make_snapshot(momentum=0.3, trend=0.3, vol=0.001, prob=0.55)
        conf = ShortDurationStrategy._compute_confidence(snap, Decimal("0.55"))
        low = conf

        snap2 = _make_snapshot(momentum=0.3, trend=1.0, vol=0.001, prob=0.55)
        conf2 = ShortDurationStrategy._compute_confidence(snap2, Decimal("0.55"))
        assert conf2 > low

    def test_high_volatility_reduces_confidence(self):
        """High volatility → confidence penalty."""
        snap_low_vol = _make_snapshot(vol=0.001)
        snap_high_vol = _make_snapshot(vol=0.010)
        c1 = ShortDurationStrategy._compute_confidence(snap_low_vol, Decimal("0.55"))
        c2 = ShortDurationStrategy._compute_confidence(snap_high_vol, Decimal("0.55"))
        assert c1 > c2

    def test_confidence_bounded_0_95(self):
        """Confidence never exceeds 0.95."""
        snap = _make_snapshot(momentum=1.0, trend=1.0, vol=0.0, prob=0.62)
        conf = ShortDurationStrategy._compute_confidence(snap, Decimal("0.95"))
        assert conf <= Decimal("0.95")

    def test_urgency_near_resolution(self):
        """< 10 minutes → urgency 9."""
        assert ShortDurationStrategy._compute_urgency(0.1) == 9  # 6 minutes

    def test_urgency_half_hour(self):
        """< 30 minutes → urgency 7."""
        assert ShortDurationStrategy._compute_urgency(0.4) == 7

    def test_urgency_one_hour(self):
        """< 1 hour → urgency 5."""
        assert ShortDurationStrategy._compute_urgency(0.8) == 5

    def test_urgency_two_hours(self):
        """> 1 hour → urgency 3."""
        assert ShortDurationStrategy._compute_urgency(1.5) == 3


# ── Validate tests ───────────────────────────────────────────────────


class TestValidate:
    def test_valid_signal_passes(self):
        snap = _make_snapshot(momentum=0.3, trend=0.8, prob=0.56)
        strat = _make_strategy({"btc": snap})

        market = _make_market(up_price=0.50)
        signals = strat.scan({"markets": [market]})
        assert len(signals) == 1

        ok, reason = strat.validate(signals[0])
        assert ok
        assert reason == "ok"

    def test_momentum_reversal_fails_validation(self):
        """If momentum flips before execution, reject."""
        snap_initial = _make_snapshot(momentum=0.5, trend=0.9, prob=0.57)
        strat = _make_strategy({"btc": snap_initial})

        market = _make_market(up_price=0.50)
        signals = strat.scan({"markets": [market]})
        assert len(signals) == 1

        # Now momentum flips negative
        snap_reversed = _make_snapshot(momentum=-0.3, trend=0.7, prob=0.45)
        strat._snapshots["btc"] = snap_reversed

        ok, reason = strat.validate(signals[0])
        assert not ok
        assert reason == "momentum_reversed"


# ── CryptoPriceFeed analytics tests ─────────────────────────────────


class TestPriceFeedAnalytics:
    def test_direction_probability_bounded(self):
        """P(up) always between 0.38 and 0.62."""
        snap = PriceSnapshot(
            ticker="btc", price=100000, timestamp=time.time(),
            momentum_score=1.0, trend_strength=1.0, volatility_1h=0.0,
        )
        prob = CryptoPriceFeed._estimate_direction_prob(snap)
        assert 0.38 <= prob <= 0.62

    def test_direction_probability_neutral_at_zero_momentum(self):
        """Zero momentum → probability near 0.50."""
        snap = PriceSnapshot(
            ticker="btc", price=100000, timestamp=time.time(),
            momentum_score=0.0, trend_strength=0.5, volatility_1h=0.002,
        )
        prob = CryptoPriceFeed._estimate_direction_prob(snap)
        assert abs(prob - 0.50) < 0.01

    def test_positive_momentum_increases_up_probability(self):
        """Positive momentum → P(up) > 0.50."""
        snap = PriceSnapshot(
            ticker="btc", price=100000, timestamp=time.time(),
            momentum_score=0.5, trend_strength=0.8, volatility_1h=0.002,
        )
        prob = CryptoPriceFeed._estimate_direction_prob(snap)
        assert prob > 0.50

    def test_negative_momentum_decreases_up_probability(self):
        """Negative momentum → P(up) < 0.50."""
        snap = PriceSnapshot(
            ticker="btc", price=100000, timestamp=time.time(),
            momentum_score=-0.5, trend_strength=0.8, volatility_1h=0.002,
        )
        prob = CryptoPriceFeed._estimate_direction_prob(snap)
        assert prob < 0.50

    def test_high_volatility_dampens_adjustment(self):
        """High vol → probability closer to 0.50 (less confident)."""
        base = PriceSnapshot(
            ticker="btc", price=100000, timestamp=time.time(),
            momentum_score=0.5, trend_strength=0.8, volatility_1h=0.001,
        )
        vol = PriceSnapshot(
            ticker="btc", price=100000, timestamp=time.time(),
            momentum_score=0.5, trend_strength=0.8, volatility_1h=0.02,
        )
        p_low = CryptoPriceFeed._estimate_direction_prob(base)
        p_high = CryptoPriceFeed._estimate_direction_prob(vol)
        # High vol should be closer to 0.50
        assert abs(p_high - 0.50) < abs(p_low - 0.50)

    def test_return_at_lookback_basic(self):
        """Return calculation from price history."""
        now_ms = time.time() * 1000
        pts = [
            [now_ms - 300_000, 100.0],  # 5 min ago: $100
            [now_ms, 101.0],            # now: $101
        ]
        ret = CryptoPriceFeed._return_at_lookback(pts, now_ms, minutes=5)
        assert ret is not None
        assert abs(ret - 0.01) < 0.001  # 1% return

    def test_return_at_lookback_no_data(self):
        """Empty price history → None."""
        ret = CryptoPriceFeed._return_at_lookback([], time.time() * 1000, minutes=5)
        assert ret is None


# ── StrategyType enum test ───────────────────────────────────────────


def test_short_duration_strategy_type_exists():
    """StrategyType.SHORT_DURATION is in the enum."""
    assert StrategyType.SHORT_DURATION == "short_duration"


# ── Multi-ticker tests ───────────────────────────────────────────────


class TestMultiTicker:
    def test_eth_market_uses_eth_snapshot(self):
        """ETH market → uses ETH price snapshot."""
        snap = _make_snapshot(ticker="eth", momentum=0.5, trend=0.9, prob=0.57)
        strat = _make_strategy({"eth": snap})

        market = _make_market(
            condition_id="cid_eth_001",
            question="Ethereum Up or Down - today",
        )
        signals = strat.scan({"markets": [market]})
        assert len(signals) == 1
        assert signals[0].opportunity.metadata["ticker"] == "eth"

    def test_no_snapshot_for_ticker_skips(self):
        """If no price data for ticker, skip market."""
        # Only have BTC data, but market is ETH
        snap = _make_snapshot(ticker="btc")
        strat = _make_strategy({"btc": snap})

        market = _make_market(
            question="Ethereum Up or Down - today",
        )
        signals = strat.scan({"markets": [market]})
        assert len(signals) == 0


# ── Config integration test ──────────────────────────────────────────


def test_config_loads_short_duration_settings():
    """Verify Settings dataclass has short-duration fields."""
    from polymarket_bot.config import Settings

    s = Settings()
    assert s.enable_short_duration_strategy is True
    assert s.short_duration_max_order_usdc == Decimal("5")
    assert s.short_duration_min_probability == Decimal("0.53")
    assert s.short_duration_prefer_maker is True
    assert s.short_duration_cooldown_seconds == 60.0


def test_orchestrator_config_has_short_duration_flag():
    """OrchestratorConfig has enable_short_duration."""
    from polymarket_bot.orchestrator import OrchestratorConfig

    cfg = OrchestratorConfig()
    assert cfg.enable_short_duration is True
