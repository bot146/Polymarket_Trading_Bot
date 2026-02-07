"""Circuit breaker for automated risk management.

Monitors portfolio performance and automatically halts trading when
safety thresholds are breached. Inspired by exchange-level circuit
breakers and hedge fund risk management systems.

Thresholds:
- Max daily loss: total realized P&L today falls below -$X
- Max drawdown: portfolio drops X% from its peak value
- Consecutive losses: N trades in a row are losers
- Cooldown: after tripping, wait N minutes before re-enabling

All thresholds are configurable via Settings.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class BreakerState(str, Enum):
    ARMED = "armed"        # Normal operation
    TRIPPED = "tripped"    # Trading halted
    COOLDOWN = "cooldown"  # Waiting to re-arm


@dataclass
class CircuitBreakerStats:
    """Running statistics tracked by the circuit breaker."""
    daily_pnl: Decimal = Decimal("0")
    peak_portfolio_value: Decimal = Decimal("0")
    current_portfolio_value: Decimal = Decimal("0")
    consecutive_losses: int = 0
    total_trips: int = 0
    last_trip_reason: str = ""
    last_trip_time: float = 0.0


class CircuitBreaker:
    """Monitors P&L and halts trading when thresholds are breached.

    Usage::

        breaker = CircuitBreaker(settings)

        # Before each trade:
        if not breaker.allow_trading():
            log.warning("Circuit breaker is active \u2014 skipping trade")
            continue

        # After each trade completes (fill confirmed):
        breaker.record_trade_result(pnl=Decimal("-0.50"))

        # Periodically (e.g., every stats print):
        breaker.update_portfolio_value(current_value)
    """

    def __init__(
        self,
        *,
        max_daily_loss_usdc: Decimal = Decimal("50"),
        max_drawdown_pct: Decimal = Decimal("10"),
        max_consecutive_losses: int = 5,
        cooldown_minutes: int = 30,
    ) -> None:
        self.max_daily_loss_usdc = max_daily_loss_usdc
        self.max_drawdown_pct = max_drawdown_pct / Decimal("100")
        self.max_consecutive_losses = max_consecutive_losses
        self.cooldown_seconds = cooldown_minutes * 60

        self._state = BreakerState.ARMED
        self._stats = CircuitBreakerStats()
        self._day_start_ts = _start_of_day()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def allow_trading(self) -> bool:
        """Return True if trading is allowed, False if circuit breaker is active."""
        self._maybe_reset_daily()
        self._maybe_exit_cooldown()
        return self._state == BreakerState.ARMED

    def record_trade_result(self, pnl: Decimal) -> None:
        """Record the P&L of a completed trade and check thresholds."""
        self._maybe_reset_daily()

        self._stats.daily_pnl += pnl

        if pnl < 0:
            self._stats.consecutive_losses += 1
        else:
            self._stats.consecutive_losses = 0

        self._check_thresholds()

    def update_portfolio_value(self, value: Decimal) -> None:
        """Update current portfolio value for drawdown tracking."""
        if value > self._stats.peak_portfolio_value:
            self._stats.peak_portfolio_value = value
        self._stats.current_portfolio_value = value
        self._check_thresholds()

    def force_trip(self, reason: str) -> None:
        """Manually trip the breaker (e.g., external kill signal)."""
        self._trip(reason)

    def reset(self) -> None:
        """Manually re-arm the breaker (use with caution)."""
        self._state = BreakerState.ARMED
        self._stats.consecutive_losses = 0
        log.info("Circuit breaker manually re-armed")

    @property
    def state(self) -> BreakerState:
        return self._state

    def get_stats(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "daily_pnl": float(self._stats.daily_pnl),
            "peak_portfolio_value": float(self._stats.peak_portfolio_value),
            "current_portfolio_value": float(self._stats.current_portfolio_value),
            "consecutive_losses": self._stats.consecutive_losses,
            "total_trips": self._stats.total_trips,
            "last_trip_reason": self._stats.last_trip_reason,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_thresholds(self) -> None:
        if self._state != BreakerState.ARMED:
            return

        # 1. Daily loss limit
        if self._stats.daily_pnl <= -self.max_daily_loss_usdc:
            self._trip(
                f"daily_loss_limit (daily_pnl=${self._stats.daily_pnl:.2f} <= "
                f"-${self.max_daily_loss_usdc:.2f})"
            )
            return

        # 2. Max drawdown from peak
        if self._stats.peak_portfolio_value > 0:
            drawdown = (
                (self._stats.peak_portfolio_value - self._stats.current_portfolio_value)
                / self._stats.peak_portfolio_value
            )
            if drawdown >= self.max_drawdown_pct:
                self._trip(
                    f"max_drawdown ({drawdown * 100:.1f}% >= "
                    f"{self.max_drawdown_pct * 100:.1f}%)"
                )
                return

        # 3. Consecutive losses
        if self._stats.consecutive_losses >= self.max_consecutive_losses:
            self._trip(
                f"consecutive_losses ({self._stats.consecutive_losses} >= "
                f"{self.max_consecutive_losses})"
            )
            return

    def _trip(self, reason: str) -> None:
        self._state = BreakerState.TRIPPED
        self._stats.total_trips += 1
        self._stats.last_trip_reason = reason
        self._stats.last_trip_time = time.time()
        log.error(
            "🚨 CIRCUIT BREAKER TRIPPED: %s — trading halted for %d minutes",
            reason,
            self.cooldown_seconds // 60,
        )

    def _maybe_exit_cooldown(self) -> None:
        if self._state == BreakerState.TRIPPED:
            self._state = BreakerState.COOLDOWN

        if self._state != BreakerState.COOLDOWN:
            return

        elapsed = time.time() - self._stats.last_trip_time
        if elapsed >= self.cooldown_seconds:
            self._state = BreakerState.ARMED
            self._stats.consecutive_losses = 0
            log.info(
                "✅ Circuit breaker cooldown expired — trading re-enabled "
                "(was tripped for: %s)",
                self._stats.last_trip_reason,
            )

    def _maybe_reset_daily(self) -> None:
        """Reset daily counters at midnight."""
        now_day = _start_of_day()
        if now_day > self._day_start_ts:
            self._stats.daily_pnl = Decimal("0")
            self._day_start_ts = now_day
            log.info("Circuit breaker daily counters reset")


def _start_of_day() -> float:
    """Return epoch timestamp of the start of the current UTC day."""
    import calendar
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return calendar.timegm(midnight.timetuple())
