"""Paper wallet equity + dynamic sizing controller.

This module tracks a virtual paper balance and computes a sizing multiplier
based on equity tiers. It is designed to be editable at runtime via a JSON
file so users can adjust balances/tier rules without restarting the bot.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaperWalletSnapshot:
    equity: Decimal
    starting_balance: Decimal
    manual_adjustment: Decimal
    multiplier: Decimal
    tier_floor: Decimal


class PaperWalletController:
    """Paper wallet runtime controller.

    Runtime file format (editable while bot is running):
        {
          "starting_balance": "100",
          "manual_adjustment": "0",
          "tiers": [
            {"equity": "100", "multiplier": "1.00"},
            {"equity": "1000", "multiplier": "1.10"},
            {"equity": "5000", "multiplier": "1.20"},
            {"equity": "10000", "multiplier": "1.30"}
          ]
        }

    Equity model:
        equity = starting_balance + manual_adjustment + realized_pnl + unrealized_pnl
    """

    def __init__(
        self,
        *,
        file_path: Path,
        default_starting_balance: Decimal,
        default_tier_spec: str,
        refresh_seconds: float = 5.0,
    ) -> None:
        self.file_path = file_path
        self.default_starting_balance = default_starting_balance
        self.default_tier_spec = default_tier_spec
        self.refresh_seconds = refresh_seconds

        self._last_refresh_ts = 0.0
        self._last_mtime: float | None = None

        self.starting_balance: Decimal = default_starting_balance
        self.manual_adjustment: Decimal = Decimal("0")
        self.tiers: list[tuple[Decimal, Decimal]] = self._parse_tier_spec(default_tier_spec)

        self._last_logged_multiplier: Decimal | None = None

    # ------------------------------------------------------------------
    # Persistence / runtime reloading
    # ------------------------------------------------------------------

    def ensure_file(self) -> None:
        """Create runtime wallet config if it does not exist."""
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if self.file_path.exists():
            self.refresh(force=True)
            return

        data = {
            "starting_balance": str(self.default_starting_balance),
            "manual_adjustment": "0",
            "tiers": [
                {"equity": str(eq), "multiplier": str(mult)}
                for eq, mult in self.tiers
            ],
        }
        self.file_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.refresh(force=True)

    def refresh(self, *, force: bool = False) -> None:
        """Reload runtime wallet settings from disk if changed."""
        now = time.time()
        if not force and (now - self._last_refresh_ts) < self.refresh_seconds:
            return
        self._last_refresh_ts = now

        if not self.file_path.exists():
            return

        try:
            stat = self.file_path.stat()
            mtime = stat.st_mtime
            if not force and self._last_mtime is not None and mtime <= self._last_mtime:
                return

            raw = json.loads(self.file_path.read_text(encoding="utf-8"))
            self._last_mtime = mtime

            self.starting_balance = Decimal(str(raw.get("starting_balance", self.default_starting_balance)))
            self.manual_adjustment = Decimal(str(raw.get("manual_adjustment", "0")))

            tiers_raw = raw.get("tiers")
            if isinstance(tiers_raw, list) and tiers_raw:
                parsed: list[tuple[Decimal, Decimal]] = []
                for t in tiers_raw:
                    if not isinstance(t, dict):
                        continue
                    eq = Decimal(str(t.get("equity")))
                    mult = Decimal(str(t.get("multiplier")))
                    if eq < 0 or mult <= 0:
                        continue
                    parsed.append((eq, mult))
                if parsed:
                    self.tiers = sorted(parsed, key=lambda x: x[0])

        except Exception as e:
            log.warning("Failed to refresh paper wallet config %s: %s", self.file_path, e)

    # ------------------------------------------------------------------
    # Equity / sizing
    # ------------------------------------------------------------------

    def snapshot(self, *, portfolio_stats: dict[str, Any]) -> PaperWalletSnapshot:
        """Compute current paper equity and sizing multiplier."""
        realized = Decimal(str(portfolio_stats.get("total_realized_pnl", 0)))
        unrealized = Decimal(str(portfolio_stats.get("total_unrealized_pnl", 0)))

        equity = self.starting_balance + self.manual_adjustment + realized + unrealized
        multiplier = Decimal("1")
        floor = Decimal("0")

        for eq_floor, eq_mult in self.tiers:
            if equity >= eq_floor:
                floor = eq_floor
                multiplier = eq_mult
            else:
                break

        return PaperWalletSnapshot(
            equity=equity,
            starting_balance=self.starting_balance,
            manual_adjustment=self.manual_adjustment,
            multiplier=multiplier,
            tier_floor=floor,
        )

    def maybe_log_tier_change(self, snap: PaperWalletSnapshot) -> None:
        if self._last_logged_multiplier is None or snap.multiplier != self._last_logged_multiplier:
            self._last_logged_multiplier = snap.multiplier
            log.info(
                "💼 PAPER WALLET: equity=$%.2f (start=$%.2f adj=$%.2f) -> sizing x%.2f @ tier >= $%.0f",
                float(snap.equity),
                float(snap.starting_balance),
                float(snap.manual_adjustment),
                float(snap.multiplier),
                float(snap.tier_floor),
            )

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_tier_spec(spec: str) -> list[tuple[Decimal, Decimal]]:
        """Parse tier spec string like "100:1.00,1000:1.10,5000:1.20"."""
        tiers: list[tuple[Decimal, Decimal]] = []
        for raw in (spec or "").split(","):
            item = raw.strip()
            if not item:
                continue
            if ":" not in item:
                continue
            left, right = item.split(":", 1)
            try:
                eq = Decimal(left.strip())
                mult = Decimal(right.strip())
            except Exception:
                continue
            if eq < 0 or mult <= 0:
                continue
            tiers.append((eq, mult))

        if not tiers:
            tiers = [
                (Decimal("100"), Decimal("1.00")),
                (Decimal("1000"), Decimal("1.10")),
                (Decimal("5000"), Decimal("1.20")),
                (Decimal("10000"), Decimal("1.30")),
            ]

        tiers.sort(key=lambda x: x[0])
        return tiers
