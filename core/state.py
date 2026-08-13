"""Durable state.

Requirements this file exists to satisfy:

  * atomic writes — a crash mid-write must never leave a truncated file
  * corruption recovery — a corrupt state file degrades to a fresh state plus
    a loud warning, it does not crash the service (Rule 10)
  * restart survival — last attempt / last success / consecutive failures per
    task are what make escalation and the dead-man switch meaningful
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from core.models import iso, utcnow

STATE_VERSION = 1


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON so the file is either the old content or the new content.

    Writes to a temp file in the same directory, fsyncs it, then `os.replace`
    (atomic on POSIX). The directory itself is fsynced so the rename is
    durable across power loss.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@dataclass
class TaskHealth:
    """Observability contract every scheduled task must expose."""

    name: str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str = ""
    consecutive_failures: int = 0
    total_runs: int = 0
    total_failures: int = 0

    @property
    def healthy(self) -> bool:
        return self.consecutive_failures == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "last_attempt_at": iso(self.last_attempt_at) if self.last_attempt_at else None,
            "last_success_at": iso(self.last_success_at) if self.last_success_at else None,
            "last_failure_at": iso(self.last_failure_at) if self.last_failure_at else None,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "total_runs": self.total_runs,
            "total_failures": self.total_failures,
            "healthy": self.healthy,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskHealth":
        def dt(key: str) -> datetime | None:
            value = data.get(key)
            return datetime.fromisoformat(value) if value else None

        return cls(
            name=data["name"],
            last_attempt_at=dt("last_attempt_at"),
            last_success_at=dt("last_success_at"),
            last_failure_at=dt("last_failure_at"),
            last_error=str(data.get("last_error") or ""),
            consecutive_failures=int(data.get("consecutive_failures", 0)),
            total_runs=int(data.get("total_runs", 0)),
            total_failures=int(data.get("total_failures", 0)),
        )


@dataclass
class StateStore:
    """JSON-backed state with atomic writes and corruption recovery."""

    path: Path
    version: int = STATE_VERSION
    started_at: datetime = field(default_factory=utcnow)
    tasks: dict[str, TaskHealth] = field(default_factory=dict)
    incidents: dict[str, dict[str, Any]] = field(default_factory=dict)
    counters: dict[str, Any] = field(default_factory=dict)
    recovered_from_corruption: bool = False

    # -- lifecycle --------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "StateStore":
        """Load state, recovering to a clean store if the file is unusable.

        A corrupt file is preserved as `<name>.corrupt-<timestamp>` so the
        failure can be investigated instead of silently vanishing.
        """
        store = cls(path=path)
        if not path.exists():
            return store

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("state root is not an object")
        except (json.JSONDecodeError, ValueError, OSError, UnicodeDecodeError):
            store._quarantine()
            store.recovered_from_corruption = True
            return store

        try:
            store.version = int(raw.get("version", STATE_VERSION))
            started = raw.get("started_at")
            store.started_at = (
                datetime.fromisoformat(started) if started else utcnow()
            )
            store.tasks = {
                name: TaskHealth.from_dict(payload)
                for name, payload in (raw.get("tasks") or {}).items()
            }
            store.incidents = dict(raw.get("incidents") or {})
            store.counters = dict(raw.get("counters") or {})
        except Exception:  # noqa: BLE001 - any shape error means unusable state
            # Structurally valid JSON, semantically wrong shape (a list where a
            # mapping belongs, a non-ISO timestamp, a missing task name). The
            # set of ways a hand-edited or partially-written state file can be
            # wrong is open-ended, so this catch is deliberately broad: the
            # recovery path is identical for all of them, and a monitoring
            # service must not fail to start over a malformed state file.
            store._quarantine()
            return cls(path=path, recovered_from_corruption=True)

        return store

    def _quarantine(self) -> None:
        if not self.path.exists():
            return
        stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
        target = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            shutil.move(str(self.path), str(target))
        except OSError:
            # If we cannot even move it, the next atomic write overwrites it.
            pass

    def save(self) -> None:
        atomic_write_json(
            self.path,
            {
                "version": self.version,
                "started_at": iso(self.started_at),
                "saved_at": iso(utcnow()),
                "tasks": {n: t.to_dict() for n, t in self.tasks.items()},
                "incidents": self.incidents,
                "counters": self.counters,
            },
        )

    # -- task health ------------------------------------------------------

    def task(self, name: str) -> TaskHealth:
        if name not in self.tasks:
            self.tasks[name] = TaskHealth(name=name)
        return self.tasks[name]

    def record_attempt(self, name: str) -> None:
        task = self.task(name)
        task.last_attempt_at = utcnow()
        task.total_runs += 1

    def record_success(self, name: str) -> None:
        task = self.task(name)
        task.last_success_at = utcnow()
        task.consecutive_failures = 0
        task.last_error = ""

    def record_failure(self, name: str, error: str) -> int:
        task = self.task(name)
        task.last_failure_at = utcnow()
        task.consecutive_failures += 1
        task.total_failures += 1
        # Bound the stored message so a huge traceback cannot bloat state.
        task.last_error = error[:500]
        return task.consecutive_failures

    # -- counters ---------------------------------------------------------

    def bump(self, key: str, amount: int = 1) -> int:
        value = int(self.counters.get(key, 0)) + amount
        self.counters[key] = value
        return value

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "started_at": iso(self.started_at),
            "recovered_from_corruption": self.recovered_from_corruption,
            "tasks": {n: t.to_dict() for n, t in self.tasks.items()},
            "open_incidents": sum(
                1
                for inc in self.incidents.values()
                if inc.get("status") == "open"
            ),
        }


def write_heartbeat(path: Path, payload: dict[str, Any]) -> None:
    """Dead-man file. External supervision reads its mtime.

    Deliberately separate from the state file: state can legitimately go a
    while without changing, but the heartbeat must tick every loop.
    """
    atomic_write_json(path, {**payload, "written_at": iso(utcnow())})
