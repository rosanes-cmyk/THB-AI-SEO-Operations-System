"""Incident lifecycle: deduplication, escalation, recovery.

Rule 9 in code. Three things may be said about an incident and nothing else:

    NEW INCIDENT            first time it clears the alerting bar
    PERSISTENT / ESCALATED  still broken, and the escalation window elapsed
    RECOVERED               it went away, and we had previously announced it

Everything is keyed on `Finding.fingerprint()`, which is stable across cycles.
The same defect re-detected every five minutes produces exactly one NEW alert
and then at most one PERSISTENT alert per escalation window.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Iterable

from core.config import IncidentConfig
from core.models import (
    Alert,
    AlertKind,
    Finding,
    Incident,
    IncidentStatus,
    iso,
    utcnow,
)
from core.state import StateStore

# Recovered incidents are kept this long for reporting, then pruned so state
# cannot grow without bound.
RECOVERED_RETENTION = timedelta(days=14)


class IncidentStore:
    """Reconciles a run's findings against durable incident state."""

    def __init__(self, state: StateStore, config: IncidentConfig) -> None:
        self._state = state
        self._config = config

    # -- access -----------------------------------------------------------

    def all_incidents(self) -> list[Incident]:
        out: list[Incident] = []
        for payload in self._state.incidents.values():
            try:
                out.append(Incident.from_dict(payload))
            except (KeyError, TypeError, ValueError):
                # One malformed record must not blind the whole store.
                continue
        return out

    def open_incidents(self) -> list[Incident]:
        return [i for i in self.all_incidents() if i.status is IncidentStatus.OPEN]

    def _put(self, incident: Incident) -> None:
        self._state.incidents[incident.fingerprint] = incident.to_dict()

    def _get(self, fingerprint: str) -> Incident | None:
        payload = self._state.incidents.get(fingerprint)
        if not payload:
            return None
        try:
            return Incident.from_dict(payload)
        except (KeyError, TypeError, ValueError):
            return None

    # -- alerting policy --------------------------------------------------

    def _clears_alert_bar(self, incident: Incident) -> bool:
        """Should this open incident be announced at all?

        A conversion-blocking finding bypasses both the confidence floor and
        the persistence requirement: a broken seller form on a money page is
        an emergency the first time we see it, not the second.
        """
        finding = incident.finding
        if finding.conversion_blocking:
            return True
        if finding.confidence < self._config.min_confidence_to_alert:
            return False
        return incident.persistence_count >= self._config.min_persistence_to_alert

    def _escalation_due(self, incident: Incident) -> bool:
        if incident.last_alert_at is None:
            return False
        elapsed = (utcnow() - incident.last_alert_at).total_seconds()
        return elapsed >= self._config.escalation_after_seconds

    # -- reconciliation ---------------------------------------------------

    def reconcile(
        self, findings: Iterable[Finding], scope: Iterable[str]
    ) -> list[Alert]:
        """Fold this run's findings into incident state and return alerts.

        `scope` names the source agents this run actually covered. Only open
        incidents inside that scope are eligible for recovery — otherwise a
        vision run that found nothing would "recover" a heartbeat incident it
        never even looked at.
        """
        scope_set = {s for s in scope}
        alerts: list[Alert] = []
        seen: set[str] = set()
        now = utcnow()

        for finding in findings:
            fingerprint = finding.fingerprint()
            seen.add(fingerprint)
            incident = self._get(fingerprint)

            if incident is None or incident.status is not IncidentStatus.OPEN:
                # Brand new, or a previously recovered incident that regressed.
                incident = Incident(
                    fingerprint=fingerprint,
                    finding=finding,
                    first_seen=now,
                    last_seen=now,
                    persistence_count=1,
                )
            else:
                incident.persistence_count += 1
                incident.last_seen = now
                # Always carry the freshest evidence (newest screenshot, newest
                # confidence) so an alert never shows a stale artifact.
                incident.finding = finding

            incident.status = IncidentStatus.OPEN
            incident.history.append(
                {
                    "at": iso(now),
                    "event": "observed",
                    "confidence": finding.confidence,
                    "severity": finding.severity.value,
                }
            )
            incident.history = incident.history[-50:]

            if self._clears_alert_bar(incident):
                if incident.alert_count == 0:
                    incident.alert_count += 1
                    incident.last_alert_at = now
                    alerts.append(Alert(kind=AlertKind.NEW, incident=incident))
                elif self._escalation_due(incident):
                    incident.alert_count += 1
                    incident.last_alert_at = now
                    alerts.append(Alert(kind=AlertKind.PERSISTENT, incident=incident))

            self._put(incident)

        # Recovery pass, restricted to the scope this run actually covered.
        for incident in self.open_incidents():
            if incident.fingerprint in seen:
                continue
            if incident.finding.source_agent not in scope_set:
                continue
            incident.status = IncidentStatus.RECOVERED
            incident.recovered_at = now
            incident.history.append({"at": iso(now), "event": "recovered"})
            incident.history = incident.history[-50:]
            self._put(incident)
            # Never announce the recovery of something we never announced.
            if incident.alert_count > 0:
                alerts.append(Alert(kind=AlertKind.RECOVERED, incident=incident))

        self.prune()
        return alerts

    def prune(self) -> int:
        """Drop long-recovered incidents. Returns the number removed."""
        cutoff = utcnow() - RECOVERED_RETENTION
        removed = 0
        for fingerprint, payload in list(self._state.incidents.items()):
            if payload.get("status") != "recovered":
                continue
            stamp = payload.get("recovered_at")
            if not stamp:
                continue
            try:
                from datetime import datetime

                if datetime.fromisoformat(stamp) < cutoff:
                    del self._state.incidents[fingerprint]
                    removed += 1
            except ValueError:
                continue
        return removed
