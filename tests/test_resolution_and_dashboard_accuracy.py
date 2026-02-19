from __future__ import annotations

from decimal import Decimal

from polymarket_bot.dashboard import _render_html
from polymarket_bot.position_manager import PositionManager
from polymarket_bot.resolution_monitor import ResolutionMonitor
from polymarket_bot.scanner import MarketInfo, MarketScanner, TokenInfo


def test_scanner_get_market_falls_back_to_condition_id_lookup() -> None:
    scanner = MarketScanner()

    market_payload = {
        "conditionId": "0xabc123",
        "question": "Will test market resolve?",
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.55","0.45"]',
        "clobTokenIds": '["tok_yes","tok_no"]',
        "volume": "1000",
        "liquidity": "500",
        "active": False,
        "closed": True,
        "resolved": True,
        "winning_outcome": "YES",
    }

    def fake_get(endpoint: str, params=None):
        if endpoint == "/markets/0xabc123":
            raise RuntimeError("422 id is invalid")
        if endpoint == "/markets":
            return [market_payload]
        raise RuntimeError(f"unexpected endpoint: {endpoint}")

    scanner._get = fake_get  # type: ignore[method-assign]

    market = scanner.get_market("0xabc123")

    assert market is not None
    assert market.condition_id == "0xabc123"
    assert market.resolved is True


def test_get_all_markets_active_false_fetches_full_universe() -> None:
    scanner = MarketScanner(fetch_limit=123)
    captured = {}

    def fake_get(endpoint: str, params=None):
        captured["endpoint"] = endpoint
        captured["params"] = dict(params or {})
        return []

    scanner._get = fake_get  # type: ignore[method-assign]
    scanner.get_all_markets(limit=None, active_only=False)

    assert captured["endpoint"] == "/markets"
    assert captured["params"].get("limit") == 123
    assert "active" not in captured["params"]


def test_get_market_by_token_uses_clob_token_ids_query() -> None:
    scanner = MarketScanner()
    captured = {}

    market_payload = {
        "conditionId": "0xfromtoken",
        "question": "Token lookup market",
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.6","0.4"]',
        "clobTokenIds": '["tok_yes","tok_no"]',
        "volume": "100",
        "liquidity": "50",
        "active": False,
        "closed": True,
        "resolved": True,
        "winning_outcome": "YES",
    }

    def fake_get(endpoint: str, params=None):
        captured["endpoint"] = endpoint
        captured["params"] = dict(params or {})
        if endpoint == "/markets":
            return [market_payload]
        return []

    scanner._get = fake_get  # type: ignore[method-assign]
    market = scanner.get_market_by_token("tok_yes")

    assert captured["endpoint"] == "/markets"
    assert captured["params"].get("clob_token_ids") == "tok_yes"
    assert market is not None
    assert market.condition_id == "0xfromtoken"


def test_scanner_parse_market_accepts_camelcase_winning_outcome() -> None:
    scanner = MarketScanner()
    market = scanner._parse_market({
        "conditionId": "c1",
        "question": "Camel winner field",
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["1.0","0.0"]',
        "clobTokenIds": '["y","n"]',
        "resolved": True,
        "winningOutcome": "yes",
    })

    assert market.winning_outcome == "YES"


def test_scanner_parse_market_derives_winner_from_tokens_flag() -> None:
    scanner = MarketScanner()
    market = scanner._parse_market({
        "conditionId": "c2",
        "question": "Token winner flag",
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["1.0","0.0"]',
        "clobTokenIds": '["y","n"]',
        "resolved": True,
        "tokens": [
            {"token_id": "y", "outcome": "YES", "winner": True, "volume": "10"},
            {"token_id": "n", "outcome": "NO", "winner": False, "volume": "5"},
        ],
    })

    assert market.winning_outcome == "YES"


class _StubScanner:
    def __init__(self, winner_bcid: str):
        self._winner_bcid = winner_bcid

    def get_market(self, condition_id: str):
        if condition_id == self._winner_bcid:
            return MarketInfo(
                condition_id=condition_id,
                question="Winner bracket",
                end_date=None,
                tokens=[TokenInfo(token_id="tok", outcome="YES", price=Decimal("1"), volume=Decimal("0"))],
                volume=Decimal("0"),
                liquidity=Decimal("0"),
                active=False,
                closed=True,
                resolved=True,
                winning_outcome="YES",
            )
        return None


class _StubScannerTokenFallback:
    def __init__(self):
        self._market = MarketInfo(
            condition_id="real_cond",
            question="Token mapped winner",
            end_date=None,
            tokens=[TokenInfo(token_id="tok_win", outcome="YES", price=Decimal("1"), volume=Decimal("0"))],
            volume=Decimal("0"),
            liquidity=Decimal("0"),
            active=False,
            closed=True,
            resolved=True,
            winning_outcome="YES",
        )

    def get_market(self, condition_id: str):
        # Simulate unresolved/unavailable condition-id lookup path.
        return None

    def get_market_by_token(self, token_id: str):
        if token_id == "tok_win":
            return self._market
        return None


def test_resolution_monitor_marks_winning_bracket_redeemable_when_outcome_unknown(tmp_path) -> None:
    pm = PositionManager(storage_path=str(tmp_path / "positions.json"))

    winner = pm.open_position(
        condition_id="0xgroup",
        token_id="tok_w",
        outcome="UNKNOWN",
        strategy="multi_outcome_arb",
        entry_price=Decimal("0.20"),
        quantity=Decimal("10"),
        metadata={"bracket_condition_id": "0xwinner"},
    )
    loser = pm.open_position(
        condition_id="0xgroup",
        token_id="tok_l",
        outcome="UNKNOWN",
        strategy="multi_outcome_arb",
        entry_price=Decimal("0.30"),
        quantity=Decimal("10"),
        metadata={"bracket_condition_id": "0xloser"},
    )

    monitor = ResolutionMonitor(position_manager=pm, scanner=_StubScanner("0xwinner"), check_interval=0.0)  # type: ignore[arg-type]

    events = monitor.check_resolutions()

    assert len(events) == 1
    winner_pos = pm.get_position(winner.position_id)
    loser_pos = pm.get_position(loser.position_id)
    assert winner_pos is not None and winner_pos.is_redeemable
    assert loser_pos is not None and loser_pos.is_closed


def test_resolution_monitor_uses_token_lookup_fallback_for_brackets(tmp_path) -> None:
    pm = PositionManager(storage_path=str(tmp_path / "positions.json"))

    winner = pm.open_position(
        condition_id="0xgroup",
        token_id="tok_win",
        outcome="UNKNOWN",
        strategy="conditional_arb",
        entry_price=Decimal("0.25"),
        quantity=Decimal("8"),
        metadata={"bracket_condition_id": "0xnot_lookupable"},
    )
    loser = pm.open_position(
        condition_id="0xgroup",
        token_id="tok_lose",
        outcome="UNKNOWN",
        strategy="conditional_arb",
        entry_price=Decimal("0.35"),
        quantity=Decimal("8"),
        metadata={"bracket_condition_id": "0xother"},
    )

    monitor = ResolutionMonitor(position_manager=pm, scanner=_StubScannerTokenFallback(), check_interval=0.0)  # type: ignore[arg-type]
    events = monitor.check_resolutions()

    assert len(events) == 1
    winner_pos = pm.get_position(winner.position_id)
    loser_pos = pm.get_position(loser.position_id)
    assert winner_pos is not None and winner_pos.is_redeemable
    assert loser_pos is not None and loser_pos.is_closed


def test_dashboard_uses_daily_pnl_and_expected_labels() -> None:
    stats = {
        "uptime_seconds": 60,
        "executor": {
            "total_executions": 1,
            "successful": 1,
            "failed": 0,
            "paper_total_profit": 1.5,
            "paper_total_cost": 10.0,
            "paper_roi": 15.0,
            "portfolio": {
                "open_positions": 1,
                "total_realized_pnl": 0.0,
                "total_unrealized_pnl": 0.2,
                "total_pnl": 0.2,
            },
            "wallet": {},
            "paper_trades_by_strategy": {},
            "circuit_breaker": {
                "state": "ARMED",
                "daily_pnl": -1.23,
                "drawdown_pct": 0.0,
                "consecutive_losses": 1,
            },
        },
        "orchestrator": {
            "total_signals_seen": 1,
            "total_signals_executed": 1,
            "enabled_strategies": 1,
        },
    }

    html = _render_html(stats)

    assert "Expected Profit (Signals)" in html
    assert "Expected Cost (Signals)" in html
    assert "Expected ROI (Signals)" in html
    assert "Daily P&amp;L" in html or "Daily P&L" in html
    assert "$-1.23" in html
