"""Risk policy — Rule 5, as a machine-readable table.

The policy is data, not scattered conditionals, so "what is this system allowed
to do on its own?" has exactly one answer and that answer is auditable.

Three tiers:

  AUTO        Extremely low-risk and reversible. Retesting, capturing more
              evidence, verifying recovery, recording incidents, sending
              alerts, generating a *proposal*, validating, updating internal
              state. Nothing here touches production content.

  APPROVAL    Prepared completely by the system, executed only after an
              explicit, unexpired, matching approval. Title tags, meta
              descriptions, internal links, image alt text, schema.

  HUMAN_ONLY  Never executed by this system under any circumstance. Deleting
              pages, changing important redirects, rewriting ranking content,
              robots.txt, canonical architecture, Elementor structure, Google
              Business Profile identity, destructive database operations.

Widening this table is a deliberate act. Nothing widens it at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.models import RiskTier


class ActionType(str, Enum):
    # --- AUTO ---------------------------------------------------------
    RECHECK = "recheck"
    CAPTURE_EVIDENCE = "capture_evidence"
    VERIFY_RECOVERY = "verify_recovery"
    RECORD_INCIDENT = "record_incident"
    SEND_ALERT = "send_alert"
    GENERATE_PROPOSAL = "generate_proposal"
    UPDATE_INTERNAL_STATE = "update_internal_state"

    # --- APPROVAL REQUIRED --------------------------------------------
    UPDATE_TITLE = "update_title"
    UPDATE_META_DESCRIPTION = "update_meta_description"
    UPDATE_IMAGE_ALT = "update_image_alt"
    ADD_INTERNAL_LINK = "add_internal_link"
    UPDATE_SCHEMA = "update_schema"

    # --- HUMAN ONLY ---------------------------------------------------
    DELETE_PAGE = "delete_page"
    CHANGE_REDIRECT = "change_redirect"
    REWRITE_RANKING_CONTENT = "rewrite_ranking_content"
    EDIT_ROBOTS_TXT = "edit_robots_txt"
    CHANGE_CANONICAL = "change_canonical"
    EDIT_ELEMENTOR_STRUCTURE = "edit_elementor_structure"
    CHANGE_GBP_IDENTITY = "change_gbp_identity"
    DATABASE_WRITE = "database_write"


RISK_POLICY: dict[ActionType, RiskTier] = {
    ActionType.RECHECK: RiskTier.AUTO,
    ActionType.CAPTURE_EVIDENCE: RiskTier.AUTO,
    ActionType.VERIFY_RECOVERY: RiskTier.AUTO,
    ActionType.RECORD_INCIDENT: RiskTier.AUTO,
    ActionType.SEND_ALERT: RiskTier.AUTO,
    ActionType.GENERATE_PROPOSAL: RiskTier.AUTO,
    ActionType.UPDATE_INTERNAL_STATE: RiskTier.AUTO,
    ActionType.UPDATE_TITLE: RiskTier.APPROVAL,
    ActionType.UPDATE_META_DESCRIPTION: RiskTier.APPROVAL,
    ActionType.UPDATE_IMAGE_ALT: RiskTier.APPROVAL,
    ActionType.ADD_INTERNAL_LINK: RiskTier.APPROVAL,
    ActionType.UPDATE_SCHEMA: RiskTier.APPROVAL,
    ActionType.DELETE_PAGE: RiskTier.HUMAN_ONLY,
    ActionType.CHANGE_REDIRECT: RiskTier.HUMAN_ONLY,
    ActionType.REWRITE_RANKING_CONTENT: RiskTier.HUMAN_ONLY,
    ActionType.EDIT_ROBOTS_TXT: RiskTier.HUMAN_ONLY,
    ActionType.CHANGE_CANONICAL: RiskTier.HUMAN_ONLY,
    ActionType.EDIT_ELEMENTOR_STRUCTURE: RiskTier.HUMAN_ONLY,
    ActionType.CHANGE_GBP_IDENTITY: RiskTier.HUMAN_ONLY,
    ActionType.DATABASE_WRITE: RiskTier.HUMAN_ONLY,
}

# Actions that write to production. Every one of these must also pass the
# adapter's own enablement flag and carry rollback data.
PRODUCTION_WRITE_ACTIONS = frozenset(
    {
        ActionType.UPDATE_TITLE,
        ActionType.UPDATE_META_DESCRIPTION,
        ActionType.UPDATE_IMAGE_ALT,
        ActionType.ADD_INTERNAL_LINK,
        ActionType.UPDATE_SCHEMA,
        ActionType.DELETE_PAGE,
        ActionType.CHANGE_REDIRECT,
        ActionType.REWRITE_RANKING_CONTENT,
        ActionType.EDIT_ROBOTS_TXT,
        ActionType.CHANGE_CANONICAL,
        ActionType.EDIT_ELEMENTOR_STRUCTURE,
        ActionType.CHANGE_GBP_IDENTITY,
        ActionType.DATABASE_WRITE,
    }
)


class PolicyError(RuntimeError):
    """Raised when an action is attempted that policy forbids."""


@dataclass(frozen=True)
class PolicyDecision:
    action: ActionType
    tier: RiskTier
    allowed: bool
    requires_approval: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "tier": self.tier.value,
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "reason": self.reason,
        }


def risk_tier(action: ActionType) -> RiskTier:
    """Unknown actions are HUMAN_ONLY. Fail closed, always."""
    return RISK_POLICY.get(action, RiskTier.HUMAN_ONLY)


def is_production_write(action: ActionType) -> bool:
    return action in PRODUCTION_WRITE_ACTIONS


def evaluate(
    action: ActionType,
    *,
    approved_by: str = "",
    adapter_enabled: bool = False,
) -> PolicyDecision:
    """Decide whether `action` may execute right now.

    `adapter_enabled` is the adapter's own kill switch (e.g. the WordPress
    writes flag). It is deliberately a separate gate from approval: turning the
    adapter on does not approve anything, and approving something does not turn
    the adapter on.
    """
    tier = risk_tier(action)

    if tier is RiskTier.HUMAN_ONLY:
        return PolicyDecision(
            action=action,
            tier=tier,
            allowed=False,
            requires_approval=False,
            reason=(
                "HUMAN_ONLY: this action is never executed by the system. A person "
                "must perform it directly."
            ),
        )

    if tier is RiskTier.AUTO:
        return PolicyDecision(
            action=action,
            tier=tier,
            allowed=True,
            requires_approval=False,
            reason="AUTO: low-risk, reversible, no production content change.",
        )

    # APPROVAL tier.
    if is_production_write(action) and not adapter_enabled:
        return PolicyDecision(
            action=action,
            tier=tier,
            allowed=False,
            requires_approval=True,
            reason=(
                "Production writes are disabled for this adapter. The proposal was "
                "prepared but not executed."
            ),
        )

    if not approved_by:
        return PolicyDecision(
            action=action,
            tier=tier,
            allowed=False,
            requires_approval=True,
            reason="APPROVAL required: no approver recorded for this action.",
        )

    return PolicyDecision(
        action=action,
        tier=tier,
        allowed=True,
        requires_approval=True,
        reason=f"APPROVAL granted by {approved_by}.",
    )


def policy_table() -> list[dict[str, Any]]:
    """The full policy, for documentation and the operator dashboard."""
    return [
        {
            "action": action.value,
            "tier": tier.value,
            "production_write": is_production_write(action),
        }
        for action, tier in sorted(RISK_POLICY.items(), key=lambda kv: kv[0].value)
    ]
