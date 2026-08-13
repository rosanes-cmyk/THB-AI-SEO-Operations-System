"""Append-only audit journal — Rule 6.

The journal must be able to answer, for any change the system was involved in:
who or what changed it, when, why, from what value, to what value, and what
happened afterwards.

Append-only JSONL, one entry per line, opened in append mode and flushed +
fsynced on every write. A truncated final line loses at most the last entry and
never corrupts the ones before it — the property that matters for an audit log.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from core.models import iso, utcnow

logger = logging.getLogger(__name__)


@dataclass
class ChangeRecord:
    """One prepared or executed change.

    `old_value` is captured BEFORE any write is attempted. A change with no
    captured old value can never be executed — that is what makes rollback
    possible (Rule 6).
    """

    action: str
    target_url: str
    field_name: str
    old_value: Any
    new_value: Any
    reason: str
    expected_result: str = ""
    verification_plan: str = ""
    approved_by: str = ""
    risk_tier: str = ""
    incident_id: str = ""
    finding_id: str = ""
    dry_run: bool = True
    executed: bool = False
    verified: bool | None = None
    rolled_back: bool = False
    error: str = ""
    screenshot_before: str = ""
    screenshot_after: str = ""
    record_id: str = field(default_factory=lambda: f"chg_{uuid.uuid4().hex[:12]}")
    created_at: str = field(default_factory=lambda: iso(utcnow()))

    @property
    def has_rollback_data(self) -> bool:
        """True when we captured enough to put the old value back."""
        return self.old_value is not None

    def rollback_payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "target_url": self.target_url,
            "field_name": self.field_name,
            "restore_value": self.old_value,
            "from_record": self.record_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "created_at": self.created_at,
            "action": self.action,
            "target_url": self.target_url,
            "field_name": self.field_name,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "reason": self.reason,
            "expected_result": self.expected_result,
            "verification_plan": self.verification_plan,
            "approved_by": self.approved_by,
            "risk_tier": self.risk_tier,
            "incident_id": self.incident_id,
            "finding_id": self.finding_id,
            "dry_run": self.dry_run,
            "executed": self.executed,
            "verified": self.verified,
            "rolled_back": self.rolled_back,
            "error": self.error,
            "screenshot_before": self.screenshot_before,
            "screenshot_after": self.screenshot_after,
            "has_rollback_data": self.has_rollback_data,
        }


class Journal:
    """Append-only JSONL writer/reader."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _append(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, default=str, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def record(self, change: ChangeRecord, event: str = "prepared") -> ChangeRecord:
        """Write one journal entry. Never raises into the caller."""
        try:
            self._append({"event": event, **change.to_dict()})
        except OSError as exc:
            # A journal we cannot write to is a serious problem, but crashing
            # the monitoring loop over it makes the outage worse.
            logger.error("could not write journal entry: %s", exc)
        return change

    def note(self, event: str, **fields: Any) -> None:
        """Free-form audit event not tied to a change record."""
        try:
            self._append({"event": event, "at": iso(utcnow()), **fields})
        except OSError as exc:
            logger.error("could not write journal note: %s", exc)

    def entries(self) -> Iterator[dict[str, Any]]:
        """Read every entry, skipping any line that is not valid JSON."""
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError as exc:
            logger.error("could not read journal: %s", exc)

    def executed_changes(self) -> list[dict[str, Any]]:
        return [e for e in self.entries() if e.get("executed")]

    def pending_approvals(self) -> list[dict[str, Any]]:
        return [
            e
            for e in self.entries()
            if not e.get("executed") and not e.get("approved_by") and e.get("dry_run")
        ]
