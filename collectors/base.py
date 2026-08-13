"""Collector contract.

Rule 7 is enforced structurally: a collector that could not measure something
returns UNAVAILABLE or ERROR with an explanation. There is no code path that
lets it return a plausible-looking number it did not observe.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from core.models import CollectorStatus, Finding, iso, utcnow

logger = logging.getLogger(__name__)


@dataclass
class CollectorResult:
    """What one collector run produced."""

    agent: str
    status: CollectorStatus = CollectorStatus.OK
    findings: list[Finding] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    reason: str = ""
    started_at: str = field(default_factory=lambda: iso(utcnow()))

    @property
    def ok(self) -> bool:
        return self.status is CollectorStatus.OK

    @classmethod
    def unavailable(cls, agent: str, reason: str, **data: Any) -> "CollectorResult":
        """The check could not run — a missing credential, absent dependency,
        or unconfigured property. Not a failure; an honest 'unknown'."""
        logger.info("collector %s unavailable: %s", agent, reason)
        return cls(
            agent=agent, status=CollectorStatus.UNAVAILABLE, reason=reason, data=dict(data)
        )

    @classmethod
    def failed(cls, agent: str, error: str, **data: Any) -> "CollectorResult":
        """The check ran and failed."""
        logger.warning("collector %s error: %s", agent, error)
        return cls(agent=agent, status=CollectorStatus.ERROR, error=error, data=dict(data))

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "status": self.status.value,
            "error": self.error,
            "reason": self.reason,
            "started_at": self.started_at,
            "findings": [f.to_dict() for f in self.findings],
            "data": self.data,
        }


def collector_guard(agent: str) -> Callable[[Callable[..., CollectorResult]], Callable[..., CollectorResult]]:
    """Decorator: convert an unexpected exception into an ERROR result.

    This is the collector-level half of Rule 8. The scheduler catches whatever
    escapes anyway, but converting here means one collector's crash still
    produces a structured record the digest can report on, rather than a hole.
    """

    def decorate(func: Callable[..., CollectorResult]) -> Callable[..., CollectorResult]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> CollectorResult:
            try:
                return func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - isolation boundary
                logger.exception("collector %s raised", agent)
                return CollectorResult.failed(agent, f"{type(exc).__name__}: {exc}")

        return wrapper

    return decorate
