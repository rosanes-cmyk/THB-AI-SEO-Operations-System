"""Google Chat delivery.

Two guarantees:

  * A Chat outage cannot kill the service. Every send is wrapped; failures
    return False and are logged, never raised.
  * A Chat outage cannot lose an incident. Undelivered messages are appended
    to an on-disk outbox so the next digest can report what was missed.

Note on scope: this is an *incoming webhook*, which is one-way. It cannot
render buttons that call back into this system, so it is not used to collect
approvals. Stage 9 needs a real interactive control plane with authentication;
faking approval flows on top of a one-way webhook would be security theatre.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

import requests

from core.models import Alert, AlertKind, iso, utcnow

logger = logging.getLogger(__name__)

Poster = Callable[[str, dict[str, Any], int], int]

_ICON = {
    AlertKind.NEW: "\U0001f6a8",  # rotating light
    AlertKind.PERSISTENT: "⏰",  # alarm clock
    AlertKind.RECOVERED: "✅",  # check mark
}


def _default_poster(url: str, payload: dict[str, Any], timeout: int) -> int:
    response = requests.post(url, json=payload, timeout=timeout)
    return response.status_code


def format_alert(alert: Alert) -> str:
    """Plain-text Chat message. Readable on a phone at 3 AM."""
    finding = alert.incident.finding
    icon = _ICON[alert.kind]
    lines = [f"{icon} *{alert.title()}*"]

    if finding.entity or finding.url:
        lines.append(f"*Page:* {finding.entity or finding.url}")
    if finding.url and finding.entity:
        lines.append(f"*URL:* {finding.url}")
    if finding.viewport:
        lines.append(f"*Viewport:* {finding.viewport}")

    if alert.kind is AlertKind.RECOVERED:
        lines.append(
            f"Resolved after {alert.incident.persistence_count} consecutive detections."
        )
    else:
        if finding.business_impact:
            lines.append(f"*Business impact:* {finding.business_impact}")
        lines.append(
            f"*Severity:* {finding.severity.value} • "
            f"*Revenue weight:* {finding.revenue_weight}/100 • "
            f"*Confidence:* {finding.confidence:.0%}"
        )
        if finding.conversion_blocking:
            lines.append("*CONVERSION BLOCKING* — sellers cannot convert right now.")
        if finding.recommended_action:
            lines.append(f"*Recommended:* {finding.recommended_action}")
        if finding.screenshot_path:
            lines.append(f"*Screenshot:* {finding.screenshot_path}")
        if alert.kind is AlertKind.PERSISTENT:
            lines.append(
                f"Seen {alert.incident.persistence_count} times since "
                f"{iso(alert.incident.first_seen)}."
            )

    lines.append(f"_Incident {alert.incident.incident_id} • detected by {finding.source_agent}_")
    return "\n".join(lines)


class GoogleChatNotifier:
    """Incoming-webhook client with an on-disk outbox for failures."""

    def __init__(
        self,
        webhook_url: str,
        outbox_path: Path,
        *,
        timeout: int = 15,
        poster: Poster | None = None,
    ) -> None:
        self._url = webhook_url or ""
        self._outbox = outbox_path
        self._timeout = timeout
        self._post = poster or _default_poster

    @property
    def configured(self) -> bool:
        return bool(self._url)

    def _spool(self, kind: str, text: str, error: str) -> None:
        """Persist an undelivered message. Never raises."""
        try:
            self._outbox.parent.mkdir(parents=True, exist_ok=True)
            with self._outbox.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "at": iso(utcnow()),
                            "kind": kind,
                            "error": error[:300],
                            "text": text,
                        }
                    )
                    + "\n"
                )
        except OSError as exc:
            logger.error("could not spool undelivered message: %s", exc)

    def send_text(self, text: str, *, kind: str = "message") -> bool:
        """Post a message. Returns delivery success; never raises."""
        if not self.configured:
            self._spool(kind, text, "GOOGLE_CHAT_WEBHOOK_URL is not set")
            logger.info("Chat not configured; message spooled to %s", self._outbox)
            return False

        try:
            status = self._post(self._url, {"text": text}, self._timeout)
        except Exception as exc:  # noqa: BLE001 - delivery must never be fatal
            # The URL itself is a secret; log the failure, not the endpoint.
            logger.warning("Chat delivery failed: %s", type(exc).__name__)
            self._spool(kind, text, f"{type(exc).__name__}: {exc}")
            return False

        if 200 <= status < 300:
            return True

        logger.warning("Chat delivery returned HTTP %s", status)
        self._spool(kind, text, f"HTTP {status}")
        return False

    def send_alert(self, alert: Alert) -> bool:
        return self.send_text(format_alert(alert), kind=f"alert:{alert.kind.value}")

    def send_alerts(self, alerts: list[Alert]) -> dict[str, int]:
        delivered = failed = 0
        for alert in alerts:
            if self.send_alert(alert):
                delivered += 1
            else:
                failed += 1
        return {"delivered": delivered, "failed": failed}

    def outbox_size(self) -> int:
        """Number of messages that were never delivered."""
        if not self._outbox.exists():
            return 0
        try:
            with self._outbox.open("r", encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            return 0
