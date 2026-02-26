"""Tests that paper-mode clean restart properly resets all state."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from polymarket_bot.position_manager import PositionManager


def _open_dummy(pm: PositionManager, cid: str = "c1") -> None:
    pm.open_position(
        condition_id=cid,
        token_id=f"t_{cid}",
        outcome="YES",
        strategy="conditional_arb",
        entry_price=Decimal("0.40"),
        quantity=Decimal("10"),
        entry_order_id="o1",
        metadata={},
    )


def test_reset_clears_in_memory_positions(tmp_path: Path) -> None:
    pm = PositionManager(storage_path=str(tmp_path / "positions.json"))
    _open_dummy(pm)
    assert len(pm.positions) == 1

    pm.reset_all_positions()

    assert len(pm.positions) == 0
    assert pm._next_position_id == 1


def test_reset_writes_empty_file(tmp_path: Path) -> None:
    fp = tmp_path / "positions.json"
    pm = PositionManager(storage_path=str(fp))
    _open_dummy(pm)
    # File should have 1 position now
    data_before = json.loads(fp.read_text())
    assert len(data_before["positions"]) == 1

    pm.reset_all_positions()

    data_after = json.loads(fp.read_text())
    assert data_after["positions"] == []
    assert data_after["next_position_id"] == 1


def test_reset_survives_reload(tmp_path: Path) -> None:
    """After reset + reload from disk, state must be empty."""
    fp = tmp_path / "positions.json"
    pm1 = PositionManager(storage_path=str(fp))
    _open_dummy(pm1)
    _open_dummy(pm1, cid="c2")
    assert len(pm1.positions) == 2

    pm1.reset_all_positions()

    # Simulate restart: new manager reads same file
    pm2 = PositionManager(storage_path=str(fp))
    assert len(pm2.positions) == 0
    assert len(pm2.get_open_positions()) == 0


def test_reset_deletes_old_file_first(tmp_path: Path) -> None:
    """The reset deletes the old file before writing, so a partial write
    cannot leave stale data behind."""
    fp = tmp_path / "positions.json"
    pm = PositionManager(storage_path=str(fp))
    _open_dummy(pm)
    assert fp.stat().st_size > 50  # non-trivial file

    pm.reset_all_positions()

    # File should exist and be clean
    assert fp.exists()
    data = json.loads(fp.read_text())
    assert data["positions"] == []


def test_reset_logs_stale_count(tmp_path: Path, caplog) -> None:
    pm = PositionManager(storage_path=str(fp := tmp_path / "pos.json"))
    _open_dummy(pm)
    _open_dummy(pm, cid="c2")
    _open_dummy(pm, cid="c3")

    with caplog.at_level("INFO"):
        pm.reset_all_positions()

    assert "3 stale position" in caplog.text
