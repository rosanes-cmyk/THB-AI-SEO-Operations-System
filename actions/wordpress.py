"""WordPress remediation adapter.

PRODUCTION WRITES ARE OFF BY DEFAULT AND MUST STAY OFF UNTIL STAGE 8.

Four independent gates must all agree before a single byte reaches production:

  1. `THB_WORDPRESS_WRITES_ENABLED=true`   the adapter's kill switch
  2. `actions.policy.evaluate(...).allowed` the risk tier permits it
  3. an explicit `approved_by`              a human authorized this change
  4. captured `old_value`                   rollback is possible (Rule 6)

Any one of them failing produces a fully prepared, journaled proposal that was
not executed. That is the intended steady state until each action type has been
proven safe individually.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from actions.journal import ChangeRecord, Journal
from actions.policy import ActionType, PolicyDecision, evaluate, risk_tier
from core.config import Settings

logger = logging.getLogger(__name__)

# WordPress REST field per action.
FIELD_FOR_ACTION = {
    ActionType.UPDATE_TITLE: "title",
    ActionType.UPDATE_META_DESCRIPTION: "excerpt",
}

Transport = Callable[[str, str, dict[str, Any] | None, tuple[str, str], int], dict[str, Any]]


def _default_transport(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    auth: tuple[str, str],
    timeout: int,
) -> dict[str, Any]:
    response = requests.request(
        method,
        url,
        json=payload,
        auth=auth,
        timeout=timeout,
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    return response.json()


@dataclass
class ActionResult:
    change: ChangeRecord
    decision: PolicyDecision
    executed: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "executed": self.executed,
            "message": self.message,
            "decision": self.decision.to_dict(),
            "change": self.change.to_dict(),
        }


class WordPressAdapter:
    """Prepares and (only when fully gated) applies WordPress content changes."""

    def __init__(
        self,
        settings: Settings,
        journal: Journal,
        *,
        transport: Transport | None = None,
    ) -> None:
        self._settings = settings
        self._journal = journal
        self._transport = transport or _default_transport
        self._base = (settings.secrets.wordpress_base_url or "").rstrip("/")
        self._user = settings.secrets.wordpress_username
        self._password = settings.secrets.wordpress_app_password

    # -- capability -------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self._base and self._user and self._password)

    @property
    def writes_enabled(self) -> bool:
        """The adapter kill switch. Off by default; on is still not enough."""
        return self._settings.wordpress_writes_enabled and self.configured

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "writes_enabled": self.writes_enabled,
            "base_url": self._base or "(unset)",
        }

    # -- reads ------------------------------------------------------------

    def _api(self, path: str) -> str:
        return f"{self._base}/wp-json/wp/v2/{path.lstrip('/')}"

    def fetch_post(self, post_id: int, post_type: str = "pages") -> dict[str, Any] | None:
        """Read a post. Returns None on any failure — reads never raise."""
        if not self.configured:
            return None
        try:
            return self._transport(
                "GET",
                self._api(f"{post_type}/{post_id}"),
                None,
                (self._user, self._password),
                self._settings.limits.http_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("WordPress read failed for %s/%s: %s", post_type, post_id, exc)
            return None

    @staticmethod
    def _rendered(node: Any) -> str:
        if isinstance(node, dict):
            return str(node.get("rendered") or node.get("raw") or "")
        return str(node or "")

    # -- proposals --------------------------------------------------------

    def propose(
        self,
        action: ActionType,
        *,
        post_id: int,
        target_url: str,
        new_value: str,
        reason: str,
        post_type: str = "pages",
        incident_id: str = "",
        finding_id: str = "",
        expected_result: str = "",
    ) -> ChangeRecord:
        """Prepare a change completely, capturing rollback data. Never writes.

        Generating a proposal is an AUTO-tier action; it is safe to do
        continuously. Executing it is not.
        """
        field_name = FIELD_FOR_ACTION.get(action, "")
        old_value: Any = None

        if field_name:
            current = self.fetch_post(post_id, post_type)
            if current is not None:
                old_value = self._rendered(current.get(field_name))

        change = ChangeRecord(
            action=action.value,
            target_url=target_url,
            field_name=field_name or action.value,
            old_value=old_value,
            new_value=new_value,
            reason=reason,
            expected_result=expected_result
            or f"{field_name or action.value} on {target_url} reflects the new value",
            verification_plan=(
                "Re-fetch the post via the WordPress REST API and confirm the field "
                "matches the new value, then re-render the public URL and confirm no "
                "new console errors or visual regressions."
            ),
            risk_tier=risk_tier(action).value,
            incident_id=incident_id,
            finding_id=finding_id,
            dry_run=True,
            executed=False,
        )
        return self._journal.record(change, event="prepared")

    # -- execution --------------------------------------------------------

    def apply(
        self,
        change: ChangeRecord,
        *,
        post_id: int,
        approved_by: str = "",
        post_type: str = "pages",
    ) -> ActionResult:
        """Execute a prepared change, only if every gate passes."""
        try:
            action = ActionType(change.action)
        except ValueError:
            decision = evaluate(ActionType.DATABASE_WRITE)  # fail closed
            change.error = f"unknown action type: {change.action}"
            self._journal.record(change, event="rejected")
            return ActionResult(change, decision, False, change.error)

        decision = evaluate(
            action, approved_by=approved_by, adapter_enabled=self.writes_enabled
        )

        if not decision.allowed:
            change.approved_by = approved_by
            change.error = decision.reason
            self._journal.record(change, event="blocked")
            logger.info("WordPress write blocked: %s", decision.reason)
            return ActionResult(change, decision, False, decision.reason)

        # Gate 4: rollback data. Checked after policy so the journal records
        # the more specific reason when policy already said no.
        if not change.has_rollback_data:
            message = (
                "refusing to write without captured rollback data; "
                "re-run propose() while the post is readable"
            )
            change.error = message
            self._journal.record(change, event="blocked")
            return ActionResult(change, decision, False, message)

        field_name = FIELD_FOR_ACTION.get(action)
        if not field_name:
            message = f"no WordPress field mapping implemented for {action.value}"
            change.error = message
            self._journal.record(change, event="blocked")
            return ActionResult(change, decision, False, message)

        change.approved_by = approved_by
        change.dry_run = False

        try:
            self._transport(
                "POST",
                self._api(f"{post_type}/{post_id}"),
                {field_name: change.new_value},
                (self._user, self._password),
                self._settings.limits.http_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            change.error = f"{type(exc).__name__}: {exc}"[:300]
            self._journal.record(change, event="failed")
            return ActionResult(change, decision, False, change.error)

        change.executed = True
        self._journal.record(change, event="executed")

        verified = self._verify(change, post_id, post_type, field_name)
        change.verified = verified
        self._journal.record(change, event="verified" if verified else "verification_failed")

        if verified:
            return ActionResult(change, decision, True, "change applied and verified")

        rolled_back = self.rollback(change, post_id=post_id, post_type=post_type)
        message = (
            "verification failed; change rolled back"
            if rolled_back
            else "verification failed AND rollback failed — manual intervention required"
        )
        return ActionResult(change, decision, True, message)

    def _verify(
        self, change: ChangeRecord, post_id: int, post_type: str, field_name: str
    ) -> bool:
        current = self.fetch_post(post_id, post_type)
        if current is None:
            return False
        return self._rendered(current.get(field_name)).strip() == str(
            change.new_value
        ).strip()

    def rollback(
        self, change: ChangeRecord, *, post_id: int, post_type: str = "pages"
    ) -> bool:
        """Restore the captured old value. Journaled either way."""
        if not change.has_rollback_data:
            self._journal.note(
                "rollback_impossible",
                record_id=change.record_id,
                reason="no old value captured",
            )
            return False

        field_name = FIELD_FOR_ACTION.get(ActionType(change.action), "")
        if not field_name:
            return False

        try:
            self._transport(
                "POST",
                self._api(f"{post_type}/{post_id}"),
                {field_name: change.old_value},
                (self._user, self._password),
                self._settings.limits.http_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            self._journal.note(
                "rollback_failed",
                record_id=change.record_id,
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
            return False

        change.rolled_back = True
        self._journal.record(change, event="rolled_back")
        return True


def build_adapter(settings: Settings) -> WordPressAdapter:
    journal = Journal(Path(settings.data_dir) / "audit_journal.jsonl")
    return WordPressAdapter(settings, journal)
