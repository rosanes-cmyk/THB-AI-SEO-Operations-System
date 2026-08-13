"""Canonical schemas.

Every agent — heartbeat, vision, crawl, PageSpeed, and later Search Console,
GA4, revenue attribution, local — normalizes what it observes into a single
`Finding`. The reasoning engine consumes only `Finding`; it never sees a
collector's raw shape. That is what keeps Stage 7 from becoming a pile of
special cases.

`Incident` is the durable, stateful wrapper around a recurring Finding: it
carries the evidence, the persistence count, and the alert lifecycle
(NEW -> PERSISTENT -> RECOVERED) required by Rule 4 and Rule 9.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class Severity(str, Enum):
    """Technical severity — how broken the thing is, in isolation."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {
            Severity.CRITICAL: 4,
            Severity.HIGH: 3,
            Severity.MEDIUM: 2,
            Severity.LOW: 1,
            Severity.INFO: 0,
        }[self]


class RiskTier(str, Enum):
    """Rule 5 — tiered autonomy.

    AUTO             low-risk, reversible, no production content change
    APPROVAL         prepared fully, executed only after explicit approval
    HUMAN_ONLY       never executed by the system, under any circumstance
    """

    AUTO = "auto"
    APPROVAL = "approval"
    HUMAN_ONLY = "human_only"


class IncidentStatus(str, Enum):
    OPEN = "open"
    RECOVERED = "recovered"
    SUPPRESSED = "suppressed"


class AlertKind(str, Enum):
    """Rule 9 — the only three things we are allowed to say."""

    NEW = "new"
    PERSISTENT = "persistent"
    RECOVERED = "recovered"


class CollectorStatus(str, Enum):
    """Rule 7 — a collector says what it knows, including that it knows nothing.

    OK           the check ran and produced trustworthy data
    UNAVAILABLE  the check could not run (no credential, dependency missing)
    ERROR        the check ran and failed (timeout, 5xx, malformed response)

    UNAVAILABLE and ERROR never carry invented values.
    """

    OK = "ok"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass
class Finding:
    """One normalized observation from any agent.

    Fields marked (Rule 4) are the mandatory evidence contract.
    """

    source_agent: str
    problem: str
    url: str = ""
    entity: str = ""
    severity: Severity = Severity.MEDIUM
    confidence: float = 0.5
    revenue_weight: int = 0
    conversion_blocking: bool = False
    business_impact: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    screenshot_path: str = ""
    viewport: str = ""
    recommended_action: str = ""
    risk_tier: RiskTier = RiskTier.HUMAN_ONLY
    verification_plan: str = ""
    detected_at: datetime = field(default_factory=utcnow)
    finding_id: str = field(default_factory=lambda: f"fnd_{uuid.uuid4().hex[:12]}")

    def __post_init__(self) -> None:
        # Clamp rather than raise: a malformed AI confidence must never take
        # down the collector that produced it (Rule 8).
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        self.revenue_weight = max(0, min(100, int(self.revenue_weight)))
        if isinstance(self.severity, str):
            self.severity = Severity(self.severity)
        if isinstance(self.risk_tier, str):
            self.risk_tier = RiskTier(self.risk_tier)

    def fingerprint(self) -> str:
        """Stable identity across runs, used for incident deduplication.

        Deliberately excludes timestamps, confidence, and screenshot paths —
        the same defect on the same page in the same viewport must produce the
        same fingerprint every cycle, or Rule 9 (no alert spam) fails.
        """
        raw = "|".join(
            (self.source_agent, self.url, self.viewport, self.problem.strip().lower())
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def priority_score(self) -> float:
        """Business-impact-first ordering (the Primary Business Principle).

        Revenue weight dominates. Technical severity is a tiebreaker, not the
        headline. A conversion-blocking defect gets a hard floor so it can
        never be sorted below a cosmetic issue on a higher-weighted page.
        """
        score = (self.revenue_weight * 0.6) + (self.severity.rank * 6.0)
        score *= 0.5 + (self.confidence / 2.0)
        if self.conversion_blocking:
            score += 60.0
        return round(score, 2)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        data["risk_tier"] = self.risk_tier.value
        data["detected_at"] = iso(self.detected_at)
        data["fingerprint"] = self.fingerprint()
        data["priority_score"] = self.priority_score()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Finding":
        payload = dict(data)
        payload.pop("fingerprint", None)
        payload.pop("priority_score", None)
        detected = payload.get("detected_at")
        if isinstance(detected, str):
            payload["detected_at"] = datetime.fromisoformat(detected)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})


@dataclass
class Incident:
    """A finding that persists across cycles, with its alert lifecycle."""

    fingerprint: str
    finding: Finding
    incident_id: str = field(default_factory=lambda: f"inc_{uuid.uuid4().hex[:12]}")
    status: IncidentStatus = IncidentStatus.OPEN
    first_seen: datetime = field(default_factory=utcnow)
    last_seen: datetime = field(default_factory=utcnow)
    persistence_count: int = 1
    alert_count: int = 0
    last_alert_at: datetime | None = None
    recovered_at: datetime | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "fingerprint": self.fingerprint,
            "status": self.status.value,
            "first_seen": iso(self.first_seen),
            "last_seen": iso(self.last_seen),
            "persistence_count": self.persistence_count,
            "alert_count": self.alert_count,
            "last_alert_at": iso(self.last_alert_at) if self.last_alert_at else None,
            "recovered_at": iso(self.recovered_at) if self.recovered_at else None,
            "finding": self.finding.to_dict(),
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Incident":
        def dt(key: str) -> datetime | None:
            value = data.get(key)
            return datetime.fromisoformat(value) if value else None

        return cls(
            fingerprint=data["fingerprint"],
            finding=Finding.from_dict(data["finding"]),
            incident_id=data.get("incident_id") or f"inc_{uuid.uuid4().hex[:12]}",
            status=IncidentStatus(data.get("status", "open")),
            first_seen=dt("first_seen") or utcnow(),
            last_seen=dt("last_seen") or utcnow(),
            persistence_count=int(data.get("persistence_count", 1)),
            alert_count=int(data.get("alert_count", 0)),
            last_alert_at=dt("last_alert_at"),
            recovered_at=dt("recovered_at"),
            history=list(data.get("history") or []),
        )


@dataclass
class Alert:
    """One outbound notification. Produced by the incident store, consumed by
    notifications/. Kept separate so a Chat outage cannot lose an incident."""

    kind: AlertKind
    incident: Incident
    created_at: datetime = field(default_factory=utcnow)

    def title(self) -> str:
        f = self.incident.finding
        prefix = {
            AlertKind.NEW: "NEW INCIDENT",
            AlertKind.PERSISTENT: "PERSISTENT / ESCALATED",
            AlertKind.RECOVERED: "RECOVERED",
        }[self.kind]
        return f"{prefix}: {f.problem}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "created_at": iso(self.created_at),
            "incident": self.incident.to_dict(),
        }
