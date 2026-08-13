"""Rule 9: deduplication, escalation, and recovery."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from core.config import IncidentConfig, Settings
from core.incidents import IncidentStore
from core.models import AlertKind, Finding, Severity, utcnow
from core.state import StateStore


def make_finding(
    problem: str = "Expected lead form is absent from the page markup",
    *,
    agent: str = "site_guardian",
    url: str = "https://example.test/",
    confidence: float = 0.99,
    blocking: bool = False,
    weight: int = 95,
) -> Finding:
    return Finding(
        source_agent=agent,
        problem=problem,
        url=url,
        severity=Severity.CRITICAL,
        confidence=confidence,
        revenue_weight=weight,
        conversion_blocking=blocking,
    )


def build_store(tmp_path: Path, settings: Settings) -> tuple[IncidentStore, StateStore]:
    state = StateStore(path=tmp_path / "state.json")
    return IncidentStore(state, settings.incidents), state


def test_same_finding_alerts_once(tmp_path: Path, settings: Settings) -> None:
    store, _ = build_store(tmp_path, settings)
    finding = make_finding()

    # min_persistence_to_alert is 2, so the first sighting is silent.
    first = store.reconcile([finding], scope={"site_guardian"})
    assert first == []

    second = store.reconcile([make_finding()], scope={"site_guardian"})
    assert [a.kind for a in second] == [AlertKind.NEW]

    # Every subsequent cycle inside the escalation window is silent.
    for _ in range(5):
        assert store.reconcile([make_finding()], scope={"site_guardian"}) == []


def test_conversion_blocking_alerts_immediately(
    tmp_path: Path, settings: Settings
) -> None:
    store, _ = build_store(tmp_path, settings)
    alerts = store.reconcile([make_finding(blocking=True)], scope={"site_guardian"})
    assert [a.kind for a in alerts] == [AlertKind.NEW]


def test_low_confidence_never_alerts(tmp_path: Path, settings: Settings) -> None:
    store, _ = build_store(tmp_path, settings)
    for _ in range(5):
        alerts = store.reconcile(
            [make_finding(confidence=0.4)], scope={"site_guardian"}
        )
        assert alerts == []


def test_escalation_after_window(tmp_path: Path, settings: Settings) -> None:
    store, state = build_store(tmp_path, settings)

    store.reconcile([make_finding(blocking=True)], scope={"site_guardian"})
    fingerprint = make_finding(blocking=True).fingerprint()

    # Pretend the last alert was sent two hours ago.
    payload = state.incidents[fingerprint]
    payload["last_alert_at"] = (utcnow() - timedelta(hours=2)).isoformat()

    alerts = store.reconcile([make_finding(blocking=True)], scope={"site_guardian"})
    assert [a.kind for a in alerts] == [AlertKind.PERSISTENT]
    assert alerts[0].incident.persistence_count == 2


def test_recovery_alert_only_after_a_real_alert(
    tmp_path: Path, settings: Settings
) -> None:
    store, _ = build_store(tmp_path, settings)

    store.reconcile([make_finding(blocking=True)], scope={"site_guardian"})
    alerts = store.reconcile([], scope={"site_guardian"})

    assert [a.kind for a in alerts] == [AlertKind.RECOVERED]
    assert store.open_incidents() == []


def test_never_announced_incident_recovers_silently(
    tmp_path: Path, settings: Settings
) -> None:
    store, _ = build_store(tmp_path, settings)

    # One sighting only — below the persistence bar, so never announced.
    store.reconcile([make_finding()], scope={"site_guardian"})
    alerts = store.reconcile([], scope={"site_guardian"})

    assert alerts == []
    assert store.open_incidents() == []


def test_recovery_is_scoped_to_the_agents_that_ran(
    tmp_path: Path, settings: Settings
) -> None:
    """A vision run finding nothing must not 'recover' a heartbeat incident."""
    store, _ = build_store(tmp_path, settings)

    store.reconcile([make_finding(blocking=True)], scope={"site_guardian"})
    assert len(store.open_incidents()) == 1

    alerts = store.reconcile([], scope={"vision"})

    assert alerts == []
    assert len(store.open_incidents()) == 1, "out-of-scope incident must stay open"


def test_regression_after_recovery_alerts_again(
    tmp_path: Path, settings: Settings
) -> None:
    store, _ = build_store(tmp_path, settings)

    store.reconcile([make_finding(blocking=True)], scope={"site_guardian"})
    store.reconcile([], scope={"site_guardian"})
    alerts = store.reconcile([make_finding(blocking=True)], scope={"site_guardian"})

    assert [a.kind for a in alerts] == [AlertKind.NEW]
    assert alerts[0].incident.persistence_count == 1


def test_distinct_problems_are_distinct_incidents(
    tmp_path: Path, settings: Settings
) -> None:
    store, _ = build_store(tmp_path, settings)
    store.reconcile(
        [
            make_finding("Expected lead form is absent", blocking=True),
            make_finding("No tappable phone link rendered", blocking=True),
        ],
        scope={"site_guardian"},
    )
    assert len(store.open_incidents()) == 2


def test_fingerprint_is_stable_across_instances() -> None:
    a = make_finding()
    b = make_finding()
    assert a.finding_id != b.finding_id, "ids differ"
    assert a.fingerprint() == b.fingerprint(), "fingerprints must match"


def test_incident_state_survives_restart(tmp_path: Path, settings: Settings) -> None:
    state = StateStore(path=tmp_path / "state.json")
    store = IncidentStore(state, settings.incidents)
    store.reconcile([make_finding(blocking=True)], scope={"site_guardian"})
    state.save()

    reloaded_state = StateStore.load(tmp_path / "state.json")
    reloaded = IncidentStore(reloaded_state, settings.incidents)
    open_incidents = reloaded.open_incidents()

    assert len(open_incidents) == 1
    assert open_incidents[0].alert_count == 1

    # A restart must not re-announce an incident it already announced.
    assert reloaded.reconcile([make_finding(blocking=True)], scope={"site_guardian"}) == []


def test_priority_puts_revenue_impact_first() -> None:
    """The Primary Business Principle, as an assertion."""
    broken_form_on_money_page = Finding(
        source_agent="vision",
        problem="Lead form not visible",
        severity=Severity.CRITICAL,
        revenue_weight=95,
        confidence=0.95,
        conversion_blocking=True,
    )
    duplicate_title_on_old_blog = Finding(
        source_agent="technical_seo",
        problem="Duplicate title",
        severity=Severity.CRITICAL,  # deliberately over-severe
        revenue_weight=2,
        confidence=1.0,
    )
    assert (
        broken_form_on_money_page.priority_score()
        > duplicate_title_on_old_blog.priority_score()
    )


def test_malformed_incident_record_does_not_blind_the_store(
    tmp_path: Path, settings: Settings
) -> None:
    store, state = build_store(tmp_path, settings)
    store.reconcile([make_finding(blocking=True)], scope={"site_guardian"})
    state.incidents["garbage"] = {"not": "an incident"}

    assert len(store.open_incidents()) == 1


def test_history_is_bounded(tmp_path: Path, settings: Settings) -> None:
    config = IncidentConfig(min_persistence_to_alert=1, min_confidence_to_alert=0.0)
    state = StateStore(path=tmp_path / "state.json")
    store = IncidentStore(state, config)
    for _ in range(80):
        store.reconcile([make_finding()], scope={"site_guardian"})
    assert len(store.open_incidents()[0].history) <= 50
