from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from polymarket_bot.paper_wallet import PaperWalletController


def test_paper_wallet_creates_default_file_and_uses_default_tiers(tmp_path: Path):
    cfg = tmp_path / "paper_wallet.json"
    wallet = PaperWalletController(
        file_path=cfg,
        default_starting_balance=Decimal("100"),
        default_tier_spec="100:1.00,1000:1.10,5000:1.20,10000:1.30",
        refresh_seconds=0,
    )
    wallet.ensure_file()

    snap = wallet.snapshot(
        portfolio_stats={
            "total_realized_pnl": 0,
            "total_unrealized_pnl": 0,
        }
    )
    assert snap.equity == Decimal("100")
    assert snap.multiplier == Decimal("1.00")


def test_paper_wallet_equity_crosses_tiers(tmp_path: Path):
    cfg = tmp_path / "paper_wallet.json"
    wallet = PaperWalletController(
        file_path=cfg,
        default_starting_balance=Decimal("100"),
        default_tier_spec="100:1.00,1000:1.10,5000:1.20,10000:1.30",
        refresh_seconds=0,
    )
    wallet.ensure_file()

    snap = wallet.snapshot(
        portfolio_stats={
            "total_realized_pnl": 950,
            "total_unrealized_pnl": 0,
        }
    )
    assert snap.equity == Decimal("1050")
    assert snap.multiplier == Decimal("1.10")

    snap2 = wallet.snapshot(
        portfolio_stats={
            "total_realized_pnl": 9000,
            "total_unrealized_pnl": 1000,
        }
    )
    assert snap2.equity == Decimal("10100")
    assert snap2.multiplier == Decimal("1.30")


def test_manual_adjustment_applies_without_restart(tmp_path: Path):
    cfg = tmp_path / "paper_wallet.json"
    wallet = PaperWalletController(
        file_path=cfg,
        default_starting_balance=Decimal("100"),
        default_tier_spec="100:1.00,1000:1.10,5000:1.20,10000:1.30",
        refresh_seconds=0,
    )
    wallet.ensure_file()

    # Edit file at runtime
    cfg.write_text(
        """
{
  "starting_balance": "100",
  "manual_adjustment": "900",
  "tiers": [
    {"equity": "100", "multiplier": "1.00"},
    {"equity": "1000", "multiplier": "1.10"}
  ]
}
""".strip(),
        encoding="utf-8",
    )
    wallet.refresh(force=True)

    snap = wallet.snapshot(
        portfolio_stats={
            "total_realized_pnl": 0,
            "total_unrealized_pnl": 0,
        }
    )
    assert snap.equity == Decimal("1000")
    assert snap.multiplier == Decimal("1.10")
