"""Tests for PositionCloser."""

from decimal import Decimal
from unittest.mock import MagicMock, patch
import time

from polymarket_bot.position_closer import PositionCloser, CloseResult
from polymarket_bot.position_manager import Position, PositionManager
from polymarket_bot.resolution_monitor import ResolutionMonitor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(**overrides):
    """Return a minimal Settings-like mock."""
    defaults = {
        "trading_mode": "paper",
        "profit_target_pct": Decimal("10"),
        "stop_loss_pct": Decimal("20"),
        "max_position_age_hours": 48.0,
        "poly_private_key": "",
    }
    defaults.update(overrides)
    s = MagicMock()
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


def _make_position(
    position_id: str = "pos1",
    token_id: str = "tok1",
    condition_id: str = "cond1",
    entry_price: Decimal = Decimal("0.50"),
    quantity: Decimal = Decimal("10"),
    cost_basis: Decimal | None = None,
    strategy: str = "arbitrage",
    entry_time: float | None = None,
    is_redeemable: bool = False,
) -> Position:
    p = MagicMock(spec=Position)
    p.position_id = position_id
    p.token_id = token_id
    p.condition_id = condition_id
    p.entry_price = entry_price
    p.quantity = quantity
    p.cost_basis = cost_basis if cost_basis is not None else entry_price * quantity
    p.strategy = strategy
    p.entry_time = entry_time if entry_time is not None else time.time()
    p.is_redeemable = is_redeemable
    p.unrealized_pnl = Decimal("0")

    def _update_pnl(price):
        p.unrealized_pnl = (price - entry_price) * quantity

    p.update_unrealized_pnl = _update_pnl
    return p


def _make_closer(**settings_overrides) -> tuple[PositionCloser, MagicMock, MagicMock]:
    settings = _make_settings(**settings_overrides)
    pm = MagicMock(spec=PositionManager)
    pm.get_open_positions.return_value = []
    pm.get_redeemable_positions.return_value = []
    pm.close_position.return_value = Decimal("0.50")
    pm.update_unrealized_pnl = MagicMock()

    rm = MagicMock(spec=ResolutionMonitor)
    rm.check_resolutions.return_value = []

    closer = PositionCloser(
        client=None,
        settings=settings,
        position_manager=pm,
        resolution_monitor=rm,
    )
    return closer, pm, rm


# ---------------------------------------------------------------------------
# _should_close_position
# ---------------------------------------------------------------------------

def test_profit_target_triggers_close():
    closer, pm, _ = _make_closer(profit_target_pct=Decimal("10"))
    pos = _make_position(entry_price=Decimal("0.50"))

    # Current price = 0.60 → +20% return → exceeds 10% target
    price_data = {"tok1": Decimal("0.60")}
    assert closer._should_close_position(pos, price_data) is True
    assert closer.profit_target_closes == 1


def test_stop_loss_triggers_close():
    closer, pm, _ = _make_closer(stop_loss_pct=Decimal("20"))
    pos = _make_position(entry_price=Decimal("0.50"))

    # Current price = 0.35 → -30% return → exceeds -20% stop
    price_data = {"tok1": Decimal("0.35")}
    assert closer._should_close_position(pos, price_data) is True
    assert closer.stop_loss_closes == 1


def test_no_close_within_bounds():
    closer, pm, _ = _make_closer(
        profit_target_pct=Decimal("50"),
        stop_loss_pct=Decimal("50"),
    )
    pos = _make_position(entry_price=Decimal("0.50"))

    # +4% return — neither target nor stop
    price_data = {"tok1": Decimal("0.52")}
    assert closer._should_close_position(pos, price_data) is False


def test_time_based_exit():
    closer, pm, _ = _make_closer(max_position_age_hours=1.0)
    # Position entered 2 hours ago
    pos = _make_position(entry_time=time.time() - 7200)

    price_data = {"tok1": Decimal("0.50")}
    assert closer._should_close_position(pos, price_data) is True
    assert closer.time_based_closes == 1


def test_time_exit_without_price_data():
    """Time-based exit should work even without price data."""
    closer, pm, _ = _make_closer(max_position_age_hours=1.0)
    pos = _make_position(entry_time=time.time() - 7200)

    # No price for this token
    price_data = {}
    assert closer._should_close_position(pos, price_data) is True


def test_no_close_without_price_when_young():
    """Young position without price data should not close."""
    closer, pm, _ = _make_closer(max_position_age_hours=48.0)
    pos = _make_position(entry_time=time.time() - 60)

    price_data = {}
    assert closer._should_close_position(pos, price_data) is False


# ---------------------------------------------------------------------------
# close_position — paper mode
# ---------------------------------------------------------------------------

def test_paper_close_records_pnl():
    closer, pm, _ = _make_closer()
    pm.close_position.return_value = Decimal("1.25")
    pos = _make_position()

    result = closer.close_position(pos, {"tok1": Decimal("0.60")})

    assert result.success is True
    assert result.reason == "paper_closed"
    assert result.realized_pnl == Decimal("1.25")
    assert closer.close_count == 1
    assert closer.total_realized_pnl == Decimal("1.25")


# ---------------------------------------------------------------------------
# redeem_position — paper mode
# ---------------------------------------------------------------------------

def test_paper_redeem_success():
    closer, pm, _ = _make_closer()
    pm.close_position.return_value = Decimal("4.00")
    pos = _make_position(is_redeemable=True, entry_price=Decimal("0.60"))

    result = closer.redeem_position(pos)

    assert result.success is True
    assert result.reason == "paper_redeemed"
    assert result.realized_pnl == Decimal("4.00")
    assert closer.redemption_count == 1
    # close_position should be called with exit_price=1.0
    pm.close_position.assert_called_once_with(pos.position_id, exit_price=Decimal("1.0"))


def test_redeem_not_redeemable():
    closer, pm, _ = _make_closer()
    pos = _make_position(is_redeemable=False)

    result = closer.redeem_position(pos)

    assert result.success is False
    assert result.reason == "not_redeemable"


# ---------------------------------------------------------------------------
# check_and_close_positions integration
# ---------------------------------------------------------------------------

def test_check_and_close_full_flow():
    """End-to-end: one redeemable + one profit-target position."""
    closer, pm, _ = _make_closer(profit_target_pct=Decimal("10"))

    redeemable = _make_position(
        position_id="redeem1", is_redeemable=True, entry_price=Decimal("0.80"),
    )
    profitable = _make_position(
        position_id="profit1", token_id="tok2", entry_price=Decimal("0.50"),
    )

    pm.get_redeemable_positions.return_value = [redeemable]
    pm.get_open_positions.return_value = [profitable]

    price_data = {"tok2": Decimal("0.60")}  # +20% return
    results = closer.check_and_close_positions(price_data)

    assert len(results) == 2
    reasons = {r.reason for r in results}
    assert "paper_redeemed" in reasons
    assert "paper_closed" in reasons


def test_arb_positions_skip_normal_exit():
    """Multi-outcome arb positions should not exit via profit/stop rules."""
    closer, pm, _ = _make_closer(profit_target_pct=Decimal("1"))

    arb_pos = _make_position(
        strategy="multi_outcome_arb",
        entry_price=Decimal("0.50"),
    )
    pm.get_open_positions.return_value = [arb_pos]
    pm.get_redeemable_positions.return_value = []

    # +100% return — but should be skipped for arb
    price_data = {"tok1": Decimal("1.00")}
    results = closer.check_and_close_positions(price_data)

    # No closes — arb positions are exempted
    assert len(results) == 0


# ---------------------------------------------------------------------------
# Arb group age-based exit
# ---------------------------------------------------------------------------

def test_arb_group_age_exit():
    """Old arb groups should be force-closed."""
    closer, pm, _ = _make_closer(max_position_age_hours=1.0)
    pm.close_position.return_value = Decimal("-0.10")

    old_arb = _make_position(
        strategy="multi_outcome_arb",
        entry_time=time.time() - 7200,  # 2h old
        position_id="arb1",
    )
    pm.get_open_positions.return_value = [old_arb]
    pm.get_redeemable_positions.return_value = []

    price_data = {"tok1": Decimal("0.40")}
    results = closer.check_and_close_positions(price_data)

    assert len(results) == 1
    assert results[0].success is True


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------

def test_get_stats_initial():
    closer, _, _ = _make_closer()
    stats = closer.get_stats()

    assert stats["total_closes"] == 0
    assert stats["total_redemptions"] == 0
    assert stats["total_realized_pnl"] == 0.0


def test_get_stats_after_activity():
    closer, pm, _ = _make_closer()
    pm.close_position.return_value = Decimal("2.50")

    pos = _make_position()
    closer.close_position(pos, {"tok1": Decimal("0.60")})

    stats = closer.get_stats()
    assert stats["total_closes"] == 1
    assert stats["total_realized_pnl"] == 2.50


# ---------------------------------------------------------------------------
# Live close — no client
# ---------------------------------------------------------------------------

def test_live_close_fails_without_client():
    closer, pm, _ = _make_closer(trading_mode="live")
    pos = _make_position()

    result = closer._live_close(pos, Decimal("0.60"))

    assert result.success is False
    assert result.reason == "no_client"
