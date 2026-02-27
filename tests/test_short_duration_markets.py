"""Tests for short-duration market scanning and visibility.

Covers:
- MarketInfo new fields (series_ticker, event_start_time, fee_type)
- get_short_duration_markets() detection logic
- endDate precision (prefer precise endDate over day-only endDateIso)
- OrchestratorConfig scan_short_duration flag
- Orchestrator stats include short-duration counts
"""

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from polymarket_bot.scanner import MarketInfo, MarketScanner, TokenInfo


# ── Fixtures ──────────────────────────────────────────────────────────


def _market_data(
    condition_id: str = "0xabc123",
    question: str = "Bitcoin Up or Down - Feb 27, 10:40AM-10:45AM ET",
    outcomes: list[str] | None = None,
    prices: list[str] | None = None,
    token_ids: list[str] | None = None,
    volume: float = 0,
    liquidity: float = 12000,
    active: bool = True,
    closed: bool = False,
    end_date: str = "2026-02-27T15:45:00Z",
    end_date_iso: str = "2026-02-27",
    series_slug: str = "btc-up-or-down-5m",
    event_start_time: str = "2026-02-27T15:40:00Z",
    fee_type: str = "crypto_15_min",
    **extra,
) -> dict:
    """Build a Gamma-API-like market dict."""
    return {
        "conditionId": condition_id,
        "question": question,
        "outcomes": json.dumps(outcomes or ["Up", "Down"]),
        "outcomePrices": json.dumps(prices or ["0.50", "0.50"]),
        "clobTokenIds": json.dumps(token_ids or ["tok_yes", "tok_no"]),
        "volume": str(volume),
        "liquidity": str(liquidity),
        "active": active,
        "closed": closed,
        "resolved": False,
        "endDate": end_date,
        "endDateIso": end_date_iso,
        "seriesSlug": series_slug,
        "eventStartTime": event_start_time,
        "feeType": fee_type,
        **extra,
    }


# ── MarketInfo field tests ────────────────────────────────────────────


class TestMarketInfoFields:
    def test_series_metadata_fields(self):
        scanner = MarketScanner()
        data = _market_data()
        m = scanner._parse_market(data)

        assert m.series_ticker == "btc-up-or-down-5m"
        assert m.event_start_time == "2026-02-27T15:40:00Z"
        assert m.fee_type == "crypto_15_min"

    def test_series_metadata_defaults_to_none(self):
        scanner = MarketScanner()
        # Standard market without series metadata
        data = _market_data(series_slug="", event_start_time="", fee_type="")
        m = scanner._parse_market(data)

        assert m.series_ticker is None
        assert m.event_start_time is None
        assert m.fee_type is None

    def test_end_date_prefers_precise(self):
        """endDate (with time) should be preferred over endDateIso (day-only)."""
        scanner = MarketScanner()
        data = _market_data(
            end_date="2026-02-27T15:45:00Z",
            end_date_iso="2026-02-27",
        )
        m = scanner._parse_market(data)

        # Should use the precise endDate, not the day-only endDateIso
        assert m.end_date == "2026-02-27T15:45:00Z"

    def test_end_date_falls_back_to_iso(self):
        """If endDate is missing, fall back to endDateIso."""
        scanner = MarketScanner()
        data = _market_data()
        data.pop("endDate", None)
        data["endDateIso"] = "2025-12-31"
        m = scanner._parse_market(data)

        assert m.end_date == "2025-12-31"


# ── get_short_duration_markets tests ─────────────────────────────────


class TestGetShortDurationMarkets:
    def _mock_scanner(self, market_dicts: list[dict]) -> MarketScanner:
        scanner = MarketScanner()
        scanner._get = MagicMock(return_value=market_dicts)
        return scanner

    def test_detects_by_series_slug(self):
        """Markets with recurrence suffix in series slug are detected."""
        scanner = self._mock_scanner([
            _market_data(series_slug="btc-up-or-down-5m"),
        ])
        result = scanner.get_short_duration_markets()
        assert len(result) == 1
        assert result[0].series_ticker == "btc-up-or-down-5m"

    def test_detects_by_question_pattern(self):
        """'Up or Down' in question triggers short-duration detection."""
        scanner = self._mock_scanner([
            _market_data(
                question="XRP Up or Down - Feb 27, 10:00AM-10:05AM ET",
                series_slug="",
            ),
        ])
        result = scanner.get_short_duration_markets()
        assert len(result) == 1

    def test_detects_by_crypto_fee_type(self):
        """crypto_15_min fee with event_start_time triggers detection."""
        scanner = self._mock_scanner([
            _market_data(
                question="Some generic crypto market",
                series_slug="",
                fee_type="crypto_15_min",
                event_start_time="2026-02-27T15:00:00Z",
            ),
        ])
        result = scanner.get_short_duration_markets()
        assert len(result) == 1

    def test_ignores_standard_markets(self):
        """Standard markets without short-duration signals are excluded."""
        scanner = self._mock_scanner([
            _market_data(
                question="Will Bitcoin hit $100k?",
                series_slug="",
                fee_type="",
                event_start_time="",
            ),
        ])
        result = scanner.get_short_duration_markets()
        assert len(result) == 0

    def test_liquidity_filter(self):
        """Markets below min_liquidity are excluded."""
        scanner = self._mock_scanner([
            _market_data(liquidity=100),  # Below default 500
        ])
        result = scanner.get_short_duration_markets(min_liquidity=Decimal("500"))
        assert len(result) == 0

    def test_liquidity_passes(self):
        """Markets at or above min_liquidity pass."""
        scanner = self._mock_scanner([
            _market_data(liquidity=1000),
        ])
        result = scanner.get_short_duration_markets(min_liquidity=Decimal("500"))
        assert len(result) == 1

    def test_inactive_excluded(self):
        """Inactive markets are excluded."""
        scanner = self._mock_scanner([
            _market_data(active=False),
        ])
        result = scanner.get_short_duration_markets()
        assert len(result) == 0

    def test_multiple_series_counted(self):
        """Multiple series are tracked with correct counts."""
        scanner = self._mock_scanner([
            _market_data(condition_id="1", question="BTC Up or Down - 10:00", series_slug="btc-up-or-down-5m"),
            _market_data(condition_id="2", question="ETH Up or Down - 10:00", series_slug="eth-up-or-down-5m"),
            _market_data(condition_id="3", question="BTC Up or Down - 10:05", series_slug="btc-up-or-down-5m"),
        ])
        result = scanner.get_short_duration_markets()
        assert len(result) == 3

    def test_sports_not_matched(self):
        """Sports markets with sports_fees don't match (no 'crypto' in fee type)."""
        scanner = self._mock_scanner([
            _market_data(
                question="Baylor Bears vs Houston Cougars",
                series_slug="",
                fee_type="sports_fees",
                event_start_time="2026-02-27T20:00:00Z",
            ),
        ])
        result = scanner.get_short_duration_markets()
        assert len(result) == 0

    def test_api_error_returns_empty(self):
        """API errors are caught and return empty list."""
        scanner = MarketScanner()
        scanner._get = MagicMock(side_effect=Exception("API error"))
        result = scanner.get_short_duration_markets()
        assert result == []

    def test_15m_series_detected(self):
        """15-minute series slug is detected."""
        scanner = self._mock_scanner([
            _market_data(
                question="Bitcoin Up or Down - 15 min",
                series_slug="btc-15m",
            ),
        ])
        result = scanner.get_short_duration_markets()
        assert len(result) == 1

    def test_over_or_under_pattern(self):
        """'over or under' in question triggers detection."""
        scanner = self._mock_scanner([
            _market_data(
                question="BTC price over or under $50,000?",
                series_slug="",
                fee_type="",
                event_start_time="",
            ),
        ])
        result = scanner.get_short_duration_markets()
        assert len(result) == 1


# ── Config tests ─────────────────────────────────────────────────────


class TestShortDurationConfig:
    def test_config_defaults(self):
        from polymarket_bot.config import Settings

        s = Settings()
        assert s.enable_short_duration_scan is True
        assert s.short_duration_min_liquidity == Decimal("500")

    def test_config_from_env(self):
        with patch.dict("os.environ", {
            "ENABLE_SHORT_DURATION_SCAN": "false",
            "SHORT_DURATION_MIN_LIQUIDITY": "1000",
        }):
            from polymarket_bot.config import load_settings
            s = load_settings()
            assert s.enable_short_duration_scan is False
            assert s.short_duration_min_liquidity == Decimal("1000")


# ── Orchestrator stats tests ────────────────────────────────────────


class TestOrchestratorShortDuration:
    def test_stats_include_short_duration(self):
        from polymarket_bot.config import Settings
        from polymarket_bot.orchestrator import OrchestratorConfig, StrategyOrchestrator

        s = Settings()
        oc = OrchestratorConfig(scan_short_duration=True)
        orch = StrategyOrchestrator(s, oc)

        stats = orch.get_stats()
        assert "short_duration_markets" in stats
        assert "short_duration_series" in stats
        assert stats["short_duration_markets"] == 0
        assert stats["short_duration_series"] == {}

    def test_scan_short_duration_flag(self):
        from polymarket_bot.orchestrator import OrchestratorConfig

        oc = OrchestratorConfig(scan_short_duration=False)
        assert oc.scan_short_duration is False

        oc2 = OrchestratorConfig()
        assert oc2.scan_short_duration is True
