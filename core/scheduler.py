"""Task scheduling with failure isolation and backoff.

Rule 8 lives here. Every task runs inside its own try/except; a task that
raises is logged, counted, and rescheduled with backoff, and the loop moves on
to the next task. One dead collector cannot stop the others, and a persistently
failing expensive task backs off instead of hammering a broken API every cycle.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.state import StateStore

logger = logging.getLogger(__name__)

TaskFunc = Callable[[], Any]


def _tz(name: str) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning("unknown timezone %r; falling back to UTC", name)
        return timezone.utc


@dataclass
class Task:
    """A scheduled unit of work.

    Exactly one of `interval_seconds` or `daily_at` must be set.

    `max_backoff_seconds` bounds how far a failing task is pushed out. Cheap,
    critical tasks (the heartbeat) get a short cap so a transient failure never
    blinds us for long. Expensive tasks (vision, PageSpeed) get a long cap so a
    broken API is not hammered every cycle.
    """

    name: str
    func: TaskFunc
    interval_seconds: float | None = None
    daily_at: str | None = None
    timezone_name: str = "UTC"
    max_backoff_seconds: float = 3600.0
    run_on_start: bool = True
    next_run_at: datetime | None = field(default=None)

    def __post_init__(self) -> None:
        if (self.interval_seconds is None) == (self.daily_at is None):
            raise ValueError(
                f"task {self.name}: set exactly one of interval_seconds or daily_at"
            )
        if self.interval_seconds is not None and self.interval_seconds <= 0:
            raise ValueError(f"task {self.name}: interval_seconds must be positive")
        if self.daily_at is not None:
            self._parse_daily(self.daily_at)  # fail fast on a bad "HH:MM"

    @staticmethod
    def _parse_daily(value: str) -> tuple[int, int]:
        try:
            hour_s, minute_s = value.strip().split(":", 1)
            hour, minute = int(hour_s), int(minute_s)
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"daily_at must look like 'HH:MM', got {value!r}") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"daily_at out of range: {value!r}")
        return hour, minute

    def next_daily_run(self, after: datetime) -> datetime:
        """Next occurrence of `daily_at` in the configured timezone.

        Computed in local wall-clock time so the 7:00 AM digest stays at 7:00
        AM across a DST transition.
        """
        hour, minute = self._parse_daily(self.daily_at or "07:00")
        tz = _tz(self.timezone_name)
        local = after.astimezone(tz)
        candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local:
            candidate = candidate + timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    def schedule_first_run(self, now: datetime) -> None:
        if self.daily_at is not None:
            self.next_run_at = self.next_daily_run(now)
        else:
            self.next_run_at = now if self.run_on_start else now + timedelta(
                seconds=float(self.interval_seconds or 0)
            )

    def schedule_after_success(self, now: datetime) -> None:
        if self.daily_at is not None:
            self.next_run_at = self.next_daily_run(now)
        else:
            self.next_run_at = now + timedelta(seconds=float(self.interval_seconds or 0))

    def schedule_after_failure(self, now: datetime, consecutive_failures: int) -> None:
        """Exponential backoff, bounded by `max_backoff_seconds`.

        A daily task that fails retries on the backoff schedule rather than
        waiting a full day — a failed 7 AM digest should not mean no digest.
        """
        base = float(self.interval_seconds or 300.0)
        exponent = max(0, min(consecutive_failures - 1, 10))
        delay = min(base * (2**exponent), self.max_backoff_seconds)
        self.next_run_at = now + timedelta(seconds=delay)


@dataclass
class TaskOutcome:
    name: str
    ok: bool
    result: Any = None
    error: str = ""
    duration_seconds: float = 0.0


class Scheduler:
    """Runs due tasks, isolating failures and recording health in state."""

    def __init__(self, state: StateStore) -> None:
        self._state = state
        self._tasks: list[Task] = []

    def add(self, task: Task) -> Task:
        if any(t.name == task.name for t in self._tasks):
            raise ValueError(f"duplicate task name: {task.name}")
        self._tasks.append(task)
        return task

    @property
    def tasks(self) -> list[Task]:
        return list(self._tasks)

    def prime(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        for task in self._tasks:
            if task.next_run_at is None:
                task.schedule_first_run(now)

    def due(self, now: datetime | None = None) -> list[Task]:
        now = now or datetime.now(timezone.utc)
        return [
            t for t in self._tasks if t.next_run_at is not None and t.next_run_at <= now
        ]

    def seconds_until_next(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        upcoming = [t.next_run_at for t in self._tasks if t.next_run_at is not None]
        if not upcoming:
            return 60.0
        return max(0.0, (min(upcoming) - now).total_seconds())

    def run_due(self, now: datetime | None = None) -> list[TaskOutcome]:
        """Run every due task. Never raises — that is the whole point."""
        now = now or datetime.now(timezone.utc)
        outcomes: list[TaskOutcome] = []

        for task in self.due(now):
            self._state.record_attempt(task.name)
            started = time.monotonic()
            try:
                result = task.func()
            except Exception as exc:  # noqa: BLE001 - isolation boundary
                elapsed = time.monotonic() - started
                failures = self._state.record_failure(
                    task.name, f"{type(exc).__name__}: {exc}"
                )
                task.schedule_after_failure(now, failures)
                logger.exception(
                    "task %s failed (consecutive=%d, next attempt %s)",
                    task.name,
                    failures,
                    task.next_run_at,
                )
                outcomes.append(
                    TaskOutcome(
                        name=task.name,
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                        duration_seconds=elapsed,
                    )
                )
            else:
                elapsed = time.monotonic() - started
                self._state.record_success(task.name)
                task.schedule_after_success(now)
                logger.info("task %s ok in %.2fs", task.name, elapsed)
                outcomes.append(
                    TaskOutcome(
                        name=task.name, ok=True, result=result, duration_seconds=elapsed
                    )
                )

        return outcomes
