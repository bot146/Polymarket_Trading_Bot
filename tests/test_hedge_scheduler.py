import time

from polymarket_bot.hedge_scheduler import HedgeScheduler


def test_hedge_scheduler_marks_due_after_timeout(monkeypatch) -> None:
    now = 1000.0

    def fake_time() -> float:
        return now

    monkeypatch.setattr(time, "time", fake_time)

    s = HedgeScheduler(hedge_timeout_ms=1000)
    s.note_imbalance("c1")
    assert not s.due("c1")

    now = 1001.1
    assert s.due("c1")
