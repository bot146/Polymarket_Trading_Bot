"""Tests for OrderBookDepthChecker."""

from decimal import Decimal
from unittest.mock import patch, MagicMock

from polymarket_bot.order_book_depth import OrderBookDepthChecker, DepthCheck


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_checker(**kwargs) -> OrderBookDepthChecker:
    defaults = dict(min_depth_usdc=Decimal("10"), timeout=1.0)
    defaults.update(kwargs)
    return OrderBookDepthChecker(**defaults)


SAMPLE_BOOK = {
    "asks": [
        {"price": "0.45", "size": "100"},
        {"price": "0.46", "size": "50"},
        {"price": "0.50", "size": "200"},
    ],
    "bids": [
        {"price": "0.44", "size": "80"},
        {"price": "0.43", "size": "60"},
        {"price": "0.40", "size": "150"},
    ],
}


# ---------------------------------------------------------------------------
# check_depth — BUY side
# ---------------------------------------------------------------------------

def test_buy_sufficient_depth():
    checker = _make_checker(min_depth_usdc=Decimal("10"))
    checker._cache["tok1"] = SAMPLE_BOOK

    result = checker.check_depth("tok1", "BUY", Decimal("0.50"), Decimal("10"))

    assert result.sufficient is True
    # All three ask levels are <= 0.50
    assert result.levels_checked == 3
    assert result.available_size == Decimal("350")  # 100 + 50 + 200


def test_buy_insufficient_depth():
    checker = _make_checker(min_depth_usdc=Decimal("99999"))
    checker._cache["tok1"] = SAMPLE_BOOK

    result = checker.check_depth("tok1", "BUY", Decimal("0.50"), Decimal("10"))

    assert result.sufficient is False


def test_buy_filters_by_limit_price():
    """Only ask levels <= limit_price should be included."""
    checker = _make_checker(min_depth_usdc=Decimal("1"))
    checker._cache["tok1"] = SAMPLE_BOOK

    result = checker.check_depth("tok1", "BUY", Decimal("0.45"), Decimal("10"))

    # Only the 0.45 level qualifies
    assert result.levels_checked == 1
    assert result.available_size == Decimal("100")


# ---------------------------------------------------------------------------
# check_depth — SELL side
# ---------------------------------------------------------------------------

def test_sell_sufficient_depth():
    checker = _make_checker(min_depth_usdc=Decimal("10"))
    checker._cache["tok1"] = SAMPLE_BOOK

    result = checker.check_depth("tok1", "SELL", Decimal("0.40"), Decimal("10"))

    assert result.sufficient is True
    assert result.levels_checked == 3  # all bids >= 0.40


def test_sell_filters_by_limit_price():
    """Only bid levels >= limit_price should be included."""
    checker = _make_checker(min_depth_usdc=Decimal("1"))
    checker._cache["tok1"] = SAMPLE_BOOK

    result = checker.check_depth("tok1", "SELL", Decimal("0.44"), Decimal("10"))

    assert result.levels_checked == 1
    assert result.available_size == Decimal("80")


# ---------------------------------------------------------------------------
# List-format levels (e.g. [[price, size], ...])
# ---------------------------------------------------------------------------

def test_list_format_levels():
    book = {
        "asks": [[0.30, 200], [0.31, 100]],
        "bids": [],
    }
    checker = _make_checker(min_depth_usdc=Decimal("1"))
    checker._cache["tok1"] = book

    result = checker.check_depth("tok1", "BUY", Decimal("0.31"), Decimal("10"))

    assert result.levels_checked == 2
    assert result.sufficient is True


# ---------------------------------------------------------------------------
# Fetch failure → conservative False
# ---------------------------------------------------------------------------

@patch("polymarket_bot.order_book_depth.requests.get")
def test_fetch_failure_returns_insufficient(mock_get):
    mock_get.side_effect = Exception("network error")
    checker = _make_checker()

    result = checker.check_depth("tok1", "BUY", Decimal("0.50"), Decimal("10"))

    assert result.sufficient is False
    assert result.available_size == Decimal("0")
    assert result.levels_checked == 0


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------

@patch("polymarket_bot.order_book_depth.requests.get")
def test_cache_avoids_duplicate_fetch(mock_get):
    resp = MagicMock()
    resp.json.return_value = SAMPLE_BOOK
    resp.raise_for_status = MagicMock()
    mock_get.return_value = resp

    checker = _make_checker()

    checker.check_depth("tok1", "BUY", Decimal("0.50"), Decimal("10"))
    checker.check_depth("tok1", "BUY", Decimal("0.50"), Decimal("10"))

    assert mock_get.call_count == 1  # second call served from cache


def test_clear_cache():
    checker = _make_checker()
    checker._cache["tok1"] = SAMPLE_BOOK
    checker.clear_cache()
    assert len(checker._cache) == 0


# ---------------------------------------------------------------------------
# check_trades batch helper
# ---------------------------------------------------------------------------

def test_check_trades_all_sufficient():
    checker = _make_checker(min_depth_usdc=Decimal("1"))
    checker._cache["tok1"] = SAMPLE_BOOK
    checker._cache["tok2"] = SAMPLE_BOOK

    all_ok, checks = checker.check_trades([
        {"token_id": "tok1", "side": "BUY", "price": "0.50", "size": "10"},
        {"token_id": "tok2", "side": "SELL", "price": "0.40", "size": "10"},
    ])

    assert all_ok is True
    assert len(checks) == 2


def test_check_trades_one_insufficient():
    checker = _make_checker(min_depth_usdc=Decimal("99999"))
    checker._cache["tok1"] = SAMPLE_BOOK

    all_ok, checks = checker.check_trades([
        {"token_id": "tok1", "side": "BUY", "price": "0.50", "size": "10"},
    ])

    assert all_ok is False


# ---------------------------------------------------------------------------
# Notional calculation
# ---------------------------------------------------------------------------

def test_notional_calculation():
    """available_notional = sum(price * size) across qualifying levels."""
    checker = _make_checker(min_depth_usdc=Decimal("1"))
    checker._cache["tok1"] = SAMPLE_BOOK

    result = checker.check_depth("tok1", "BUY", Decimal("0.46"), Decimal("10"))

    # 0.45*100 + 0.46*50 = 45 + 23 = 68
    expected = Decimal("0.45") * Decimal("100") + Decimal("0.46") * Decimal("50")
    assert result.available_notional == expected
