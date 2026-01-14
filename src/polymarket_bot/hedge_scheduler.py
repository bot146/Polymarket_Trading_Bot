"""Hedge scheduler.

In aggressive maker mode we want an "opportunistic" window:
- If we get a partial/one-sided fill, give the complementary side a short window
  to fill naturally (often it will in a tight market).
- If it doesn't, force a hedge to neutralize exposure.

This module is intentionally small and deterministic so it can be unit tested.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class PendingHedge:
    condition_id: str
    created_at: float


class HedgeScheduler:
    def __init__(self, *, hedge_timeout_ms: int) -> None:
        self.hedge_timeout_ms = hedge_timeout_ms
        self._pending: dict[str, PendingHedge] = {}

    def note_imbalance(self, condition_id: str) -> None:
        # If already pending, keep the original timestamp.
        self._pending.setdefault(condition_id, PendingHedge(condition_id=condition_id, created_at=time.time()))

    def clear(self, condition_id: str) -> None:
        self._pending.pop(condition_id, None)

    def due(self, condition_id: str) -> bool:
        pending = self._pending.get(condition_id)
        if not pending:
            return False
        age_ms = (time.time() - pending.created_at) * 1000
        return age_ms >= self.hedge_timeout_ms

    def due_conditions(self) -> list[str]:
        return [cid for cid in list(self._pending.keys()) if self.due(cid)]
