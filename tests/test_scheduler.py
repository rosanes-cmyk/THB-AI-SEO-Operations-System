"""Rule 8: failure isolation, backoff, and cadence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core.scheduler import Scheduler, Task
from core.state import StateStore


def new_scheduler(tmp_path: Path) -> tuple[Scheduler, StateStore]:
    state = StateStore(path=tmp_path / "state.json")
    return Scheduler(state), state


def test_failing_task_does_not_stop_healthy_tasks(tmp_path: Path) -> None:
    scheduler, state = new_scheduler(tmp_path)
    ran: list[str] = []

    def boom() -> None:
        raise RuntimeError("injected")

    scheduler.add(Task("exploding", boom, interval_seconds=60))
    scheduler.add(Task("healthy", lambda: ran.append("healthy"), interval_seconds=60))
    scheduler.prime()

    outcomes = scheduler.run_due()

    assert ran == ["healthy"], "the healthy task still ran"
    assert {o.name: o.ok for o in outcomes} == {"exploding": False, "healthy": True}
    assert state.task("exploding").consecutive_failures == 1
    assert state.task("healthy").consecutive_failures == 0


def test_run_due_never_raises(tmp_path: Path) -> None:
    scheduler, _ = new_scheduler(tmp_path)

    def nasty() -> None:
        raise BaseExceptionGroup("grouped", [ValueError("a"), KeyError("b")])

    scheduler.add(Task("nasty", nasty, interval_seconds=60))
    scheduler.prime()
    outcomes = scheduler.run_due()  # must not propagate
    assert outcomes[0].ok is False


def test_backoff_grows_and_is_capped(tmp_path: Path) -> None:
    scheduler, _ = new_scheduler(tmp_path)
    task = Task(
        "expensive",
        lambda: (_ for _ in ()).throw(RuntimeError("x")),
        interval_seconds=60,
        max_backoff_seconds=600,
    )
    scheduler.add(task)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    task.schedule_after_failure(now, 1)
    first = (task.next_run_at - now).total_seconds()
    task.schedule_after_failure(now, 2)
    second = (task.next_run_at - now).total_seconds()
    task.schedule_after_failure(now, 20)
    capped = (task.next_run_at - now).total_seconds()

    assert first == 60
    assert second == 120
    assert capped == 600, "backoff must be bounded"


def test_success_resets_cadence(tmp_path: Path) -> None:
    scheduler, _ = new_scheduler(tmp_path)
    task = Task("t", lambda: "ok", interval_seconds=300)
    scheduler.add(task)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    task.schedule_after_failure(now, 5)
    task.schedule_after_success(now)
    assert (task.next_run_at - now).total_seconds() == 300


def test_task_runs_only_when_due(tmp_path: Path) -> None:
    scheduler, _ = new_scheduler(tmp_path)
    calls: list[int] = []
    scheduler.add(Task("t", lambda: calls.append(1), interval_seconds=300))

    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    scheduler.prime(now)

    scheduler.run_due(now)
    assert len(calls) == 1

    scheduler.run_due(now + timedelta(seconds=60))
    assert len(calls) == 1, "not due yet"

    scheduler.run_due(now + timedelta(seconds=301))
    assert len(calls) == 2


def test_run_on_start_false_defers_first_run(tmp_path: Path) -> None:
    scheduler, _ = new_scheduler(tmp_path)
    calls: list[int] = []
    scheduler.add(
        Task("vision", lambda: calls.append(1), interval_seconds=3600, run_on_start=False)
    )
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    scheduler.prime(now)
    scheduler.run_due(now)
    assert calls == [], "expensive task must not fire immediately on boot"


def test_daily_task_uses_configured_timezone() -> None:
    task = Task(
        "digest",
        lambda: None,
        daily_at="07:00",
        timezone_name="America/Los_Angeles",
    )
    # 06:00 Los Angeles on 2026-06-15 -> next 07:00 is the same day.
    after = datetime(2026, 6, 15, 6, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    nxt = task.next_daily_run(after).astimezone(ZoneInfo("America/Los_Angeles"))
    assert (nxt.hour, nxt.minute, nxt.day) == (7, 0, 15)

    # 08:00 -> rolls to tomorrow.
    after = datetime(2026, 6, 15, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    nxt = task.next_daily_run(after).astimezone(ZoneInfo("America/Los_Angeles"))
    assert (nxt.hour, nxt.day) == (7, 16)


def test_daily_task_holds_wall_clock_across_dst() -> None:
    task = Task("digest", lambda: None, daily_at="07:00", timezone_name="America/Los_Angeles")
    tz = ZoneInfo("America/Los_Angeles")
    # Day before US spring-forward 2026 (2026-03-08).
    after = datetime(2026, 3, 7, 8, 0, tzinfo=tz)
    nxt = task.next_daily_run(after).astimezone(tz)
    assert (nxt.hour, nxt.minute) == (7, 0), "digest stays at 7:00 local"


def test_unknown_timezone_falls_back_to_utc() -> None:
    task = Task("digest", lambda: None, daily_at="07:00", timezone_name="Mars/Olympus")
    assert task.next_daily_run(datetime(2026, 1, 1, tzinfo=timezone.utc)) is not None


def test_invalid_task_configuration_fails_fast() -> None:
    with pytest.raises(ValueError):
        Task("bad", lambda: None)  # neither interval nor daily
    with pytest.raises(ValueError):
        Task("bad", lambda: None, interval_seconds=60, daily_at="07:00")
    with pytest.raises(ValueError):
        Task("bad", lambda: None, daily_at="25:00")
    with pytest.raises(ValueError):
        Task("bad", lambda: None, interval_seconds=0)


def test_duplicate_task_name_rejected(tmp_path: Path) -> None:
    scheduler, _ = new_scheduler(tmp_path)
    scheduler.add(Task("t", lambda: None, interval_seconds=60))
    with pytest.raises(ValueError):
        scheduler.add(Task("t", lambda: None, interval_seconds=60))


def test_seconds_until_next_is_never_negative(tmp_path: Path) -> None:
    scheduler, _ = new_scheduler(tmp_path)
    scheduler.add(Task("t", lambda: None, interval_seconds=60))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    scheduler.prime(now)
    assert scheduler.seconds_until_next(now + timedelta(hours=1)) == 0.0
