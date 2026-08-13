"""Rule 5 (tiered autonomy) and Rule 6 (rollback evidence).

These are the tests that must never be relaxed. Every one of them asserts that
the system cannot change production content without every gate agreeing.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from actions.journal import ChangeRecord, Journal
from actions.policy import (
    ActionType,
    RISK_POLICY,
    evaluate,
    is_production_write,
    risk_tier,
)
from actions.wordpress import WordPressAdapter
from core.config import Secrets, Settings
from core.models import RiskTier


# -- the policy table ------------------------------------------------------


def test_destructive_actions_are_human_only() -> None:
    for action in (
        ActionType.DELETE_PAGE,
        ActionType.CHANGE_REDIRECT,
        ActionType.REWRITE_RANKING_CONTENT,
        ActionType.EDIT_ROBOTS_TXT,
        ActionType.CHANGE_CANONICAL,
        ActionType.EDIT_ELEMENTOR_STRUCTURE,
        ActionType.CHANGE_GBP_IDENTITY,
        ActionType.DATABASE_WRITE,
    ):
        assert risk_tier(action) is RiskTier.HUMAN_ONLY, action
        assert evaluate(action, approved_by="ceo", adapter_enabled=True).allowed is False


def test_no_production_write_is_auto_tier() -> None:
    """The core safety invariant: nothing that writes to production is AUTO."""
    for action, tier in RISK_POLICY.items():
        if is_production_write(action):
            assert tier is not RiskTier.AUTO, f"{action.value} must not be AUTO"


def test_unknown_action_fails_closed() -> None:
    class Rogue(str):
        value = "rogue"

    assert risk_tier(Rogue("rogue")) is RiskTier.HUMAN_ONLY  # type: ignore[arg-type]


def test_auto_actions_need_no_approval() -> None:
    for action in (
        ActionType.RECHECK,
        ActionType.CAPTURE_EVIDENCE,
        ActionType.SEND_ALERT,
        ActionType.GENERATE_PROPOSAL,
    ):
        decision = evaluate(action)
        assert decision.allowed is True and decision.requires_approval is False


def test_approval_tier_blocked_without_approver() -> None:
    decision = evaluate(ActionType.UPDATE_TITLE, adapter_enabled=True)
    assert decision.allowed is False
    assert decision.requires_approval is True


def test_approval_tier_blocked_when_adapter_disabled() -> None:
    decision = evaluate(ActionType.UPDATE_TITLE, approved_by="ops", adapter_enabled=False)
    assert decision.allowed is False
    assert "disabled" in decision.reason


def test_approval_tier_allowed_only_with_both_gates() -> None:
    decision = evaluate(ActionType.UPDATE_TITLE, approved_by="ops", adapter_enabled=True)
    assert decision.allowed is True


# -- the WordPress adapter -------------------------------------------------


class RecordingTransport:
    def __init__(self, get_payload: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.get_payload = get_payload or {"title": {"rendered": "Old Title"}}

    def __call__(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None,
        auth: tuple[str, str],
        timeout: int,
    ) -> dict[str, Any]:
        self.calls.append((method, url, payload))
        if method == "GET":
            return dict(self.get_payload)
        # Echo the write back so verification succeeds.
        self.get_payload = {"title": {"rendered": (payload or {}).get("title", "")}}
        return dict(self.get_payload)

    @property
    def writes(self) -> list[tuple[str, str, dict[str, Any] | None]]:
        return [c for c in self.calls if c[0] == "POST"]


def configured(settings: Settings, *, writes_enabled: bool) -> Settings:
    return replace(
        settings,
        secrets=Secrets(
            wordpress_base_url="https://wp.example.test",
            wordpress_username="svc",
            wordpress_app_password="app-password",
        ),
        wordpress_writes_enabled=writes_enabled,
    )


def build(settings: Settings, tmp_path: Path, transport: RecordingTransport):
    journal = Journal(tmp_path / "journal.jsonl")
    return WordPressAdapter(settings, journal, transport=transport), journal


def test_writes_disabled_by_default(settings: Settings, tmp_path: Path) -> None:
    adapter, _ = build(configured(settings, writes_enabled=False), tmp_path, RecordingTransport())
    assert adapter.writes_enabled is False


def test_propose_never_writes(settings: Settings, tmp_path: Path) -> None:
    transport = RecordingTransport()
    adapter, journal = build(configured(settings, writes_enabled=True), tmp_path, transport)

    change = adapter.propose(
        ActionType.UPDATE_TITLE,
        post_id=12,
        target_url="https://example.test/",
        new_value="New Title",
        reason="title is missing the primary intent",
    )

    assert transport.writes == [], "proposing must not write"
    assert change.dry_run is True and change.executed is False
    assert change.old_value == "Old Title", "rollback data captured before any write"
    assert list(journal.entries())[0]["event"] == "prepared"


def test_apply_blocked_when_adapter_disabled(settings: Settings, tmp_path: Path) -> None:
    transport = RecordingTransport()
    adapter, journal = build(configured(settings, writes_enabled=False), tmp_path, transport)

    change = adapter.propose(
        ActionType.UPDATE_TITLE,
        post_id=12,
        target_url="https://example.test/",
        new_value="New Title",
        reason="r",
    )
    result = adapter.apply(change, post_id=12, approved_by="ops")

    assert result.executed is False
    assert transport.writes == []
    assert any(e["event"] == "blocked" for e in journal.entries())


def test_apply_blocked_without_approval(settings: Settings, tmp_path: Path) -> None:
    transport = RecordingTransport()
    adapter, _ = build(configured(settings, writes_enabled=True), tmp_path, transport)

    change = adapter.propose(
        ActionType.UPDATE_TITLE,
        post_id=12,
        target_url="https://example.test/",
        new_value="New Title",
        reason="r",
    )
    result = adapter.apply(change, post_id=12)  # no approver

    assert result.executed is False
    assert transport.writes == []


def test_apply_blocked_without_rollback_data(settings: Settings, tmp_path: Path) -> None:
    """Rule 6: no rollback data, no write. Even fully approved and enabled."""
    transport = RecordingTransport()
    adapter, _ = build(configured(settings, writes_enabled=True), tmp_path, transport)

    change = ChangeRecord(
        action=ActionType.UPDATE_TITLE.value,
        target_url="https://example.test/",
        field_name="title",
        old_value=None,  # never captured
        new_value="New Title",
        reason="r",
    )
    result = adapter.apply(change, post_id=12, approved_by="ops")

    assert result.executed is False
    assert "rollback" in result.message
    assert transport.writes == []


def test_human_only_action_is_never_executed(settings: Settings, tmp_path: Path) -> None:
    transport = RecordingTransport()
    adapter, _ = build(configured(settings, writes_enabled=True), tmp_path, transport)

    change = ChangeRecord(
        action=ActionType.DELETE_PAGE.value,
        target_url="https://example.test/",
        field_name="status",
        old_value="publish",
        new_value="trash",
        reason="cleanup",
    )
    result = adapter.apply(change, post_id=12, approved_by="ceo")

    assert result.executed is False
    assert "HUMAN_ONLY" in result.message
    assert transport.writes == []


def test_fully_gated_write_executes_and_verifies(settings: Settings, tmp_path: Path) -> None:
    transport = RecordingTransport()
    adapter, journal = build(configured(settings, writes_enabled=True), tmp_path, transport)

    change = adapter.propose(
        ActionType.UPDATE_TITLE,
        post_id=12,
        target_url="https://example.test/",
        new_value="New Title",
        reason="r",
    )
    result = adapter.apply(change, post_id=12, approved_by="ops@twinhomebuyer.com")

    assert result.executed is True
    assert change.verified is True
    assert change.approved_by == "ops@twinhomebuyer.com"
    assert len(transport.writes) == 1
    events = [e["event"] for e in journal.entries()]
    assert events == ["prepared", "executed", "verified"]


def test_failed_verification_triggers_rollback(settings: Settings, tmp_path: Path) -> None:
    class NonPersistingTransport(RecordingTransport):
        """Simulates a write that appears to succeed but does not stick."""

        def __call__(self, method, url, payload, auth, timeout):  # type: ignore[override]
            self.calls.append((method, url, payload))
            return {"title": {"rendered": "Old Title"}}

    transport = NonPersistingTransport()
    adapter, journal = build(configured(settings, writes_enabled=True), tmp_path, transport)

    change = adapter.propose(
        ActionType.UPDATE_TITLE,
        post_id=12,
        target_url="https://example.test/",
        new_value="New Title",
        reason="r",
    )
    result = adapter.apply(change, post_id=12, approved_by="ops")

    assert change.verified is False
    assert change.rolled_back is True
    assert "rolled back" in result.message
    events = [e["event"] for e in journal.entries()]
    assert "verification_failed" in events and "rolled_back" in events


def test_transport_failure_is_journaled_not_raised(settings: Settings, tmp_path: Path) -> None:
    class ExplodingTransport(RecordingTransport):
        def __call__(self, method, url, payload, auth, timeout):  # type: ignore[override]
            if method == "GET":
                return {"title": {"rendered": "Old Title"}}
            raise ConnectionError("injected WP outage")

    adapter, journal = build(
        configured(settings, writes_enabled=True), tmp_path, ExplodingTransport()
    )
    change = adapter.propose(
        ActionType.UPDATE_TITLE,
        post_id=12,
        target_url="https://example.test/",
        new_value="New Title",
        reason="r",
    )
    result = adapter.apply(change, post_id=12, approved_by="ops")

    assert result.executed is False
    assert "ConnectionError" in result.change.error
    assert any(e["event"] == "failed" for e in journal.entries())


# -- the journal -----------------------------------------------------------


def test_journal_is_append_only(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "j.jsonl")
    for i in range(3):
        journal.record(
            ChangeRecord(
                action="update_title",
                target_url=f"https://example.test/{i}",
                field_name="title",
                old_value="a",
                new_value="b",
                reason="r",
            )
        )
    assert len(list(journal.entries())) == 3


def test_journal_survives_a_corrupt_line(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    journal = Journal(path)
    journal.record(
        ChangeRecord(
            action="update_title",
            target_url="https://example.test/",
            field_name="title",
            old_value="a",
            new_value="b",
            reason="r",
        )
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{truncated line\n")
    journal.note("later_event", detail="still readable")

    events = [e.get("event") for e in journal.entries()]
    assert events == ["prepared", "later_event"]


def test_rollback_payload_restores_old_value() -> None:
    change = ChangeRecord(
        action="update_title",
        target_url="https://example.test/",
        field_name="title",
        old_value="Original",
        new_value="Changed",
        reason="r",
    )
    assert change.has_rollback_data is True
    assert change.rollback_payload()["restore_value"] == "Original"


@pytest.mark.parametrize("value", [None])
def test_missing_old_value_has_no_rollback(value: Any) -> None:
    change = ChangeRecord(
        action="update_title",
        target_url="u",
        field_name="title",
        old_value=value,
        new_value="x",
        reason="r",
    )
    assert change.has_rollback_data is False
