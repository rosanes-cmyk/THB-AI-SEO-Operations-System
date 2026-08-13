"""Stage 7 — the unified reasoning and priority engine.

Seven agents now produce findings: heartbeat, vision, technical SEO,
PageSpeed, Search Console, GA4, and revenue. Every one of them normalizes
into the same `Finding`, which is what makes this file possible: it reasons
across all of them without knowing how any of them work.

It does three things a per-agent report cannot.

**It correlates.** When the vision agent sees a broken form on the seller
page and GA4 sees conversions collapse on that same page, those are not two
findings. They are one finding with a cause and a consequence, and reporting
them separately buries the fact that we already know why. Corroborated items
outrank isolated ones because two independent agents agreeing is stronger
evidence than either alone.

**It buckets deterministically.** CRITICAL NOW / FIX NEXT / GROWTH
OPPORTUNITIES / MONITOR / NO ACTION are decided in plain Python from
revenue weight, severity, confidence, corroboration, and persistence. Claude
narrates the result; it does not choose it. That ordering matters — if the
model decided the buckets, a bad response would silently reorder the
business's priorities.

**It reports what it could not see.** A board that says NO ACTION while
three collectors were unavailable is the most dangerous output this system
could produce. Coverage is computed from the collector results themselves,
and an incomplete board says so in its headline, before anything else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Sequence

from collectors.base import CollectorResult
from core.models import (
    VERDICT_WITHHELD,
    CollectorStatus,
    Finding,
    Severity,
    iso,
    utcnow,
)
from core.urls import normalize_url

logger = logging.getLogger(__name__)

# Score floors. Deliberately explicit rather than tuned: someone reading this
# file should be able to see exactly why an item landed where it did.
CRITICAL_WEIGHT_FLOOR = 80      # revenue weight that makes a HIGH an emergency
FIX_NEXT_SCORE_FLOOR = 45.0
MONITOR_CONFIDENCE_FLOOR = 0.6


class Bucket(str, Enum):
    """The only five things the board is allowed to say."""

    CRITICAL_NOW = "critical_now"
    FIX_NEXT = "fix_next"
    GROWTH = "growth_opportunities"
    MONITOR = "monitor"
    NO_ACTION = "no_action"

    @property
    def label(self) -> str:
        return {
            Bucket.CRITICAL_NOW: "CRITICAL NOW",
            Bucket.FIX_NEXT: "FIX NEXT",
            Bucket.GROWTH: "GROWTH OPPORTUNITIES",
            Bucket.MONITOR: "MONITOR",
            Bucket.NO_ACTION: "NO ACTION",
        }[self]


# Agent pairs whose agreement means something specific. The label is the
# explanation a human actually needs — "these two are the same problem".
@dataclass(frozen=True)
class Correlation:
    agents: frozenset[str]
    label: str


CORRELATIONS: tuple[Correlation, ...] = (
    Correlation(
        frozenset({"vision_ai", "ga4"}),
        "A visible defect on this page and a measured conversion drop on the "
        "same page. The drop has a cause we can see.",
    ),
    Correlation(
        frozenset({"site_guardian", "ga4"}),
        "This page failed a health check and its conversions moved. Treat the "
        "health failure as the cause until proven otherwise.",
    ),
    Correlation(
        frozenset({"technical_seo", "search_console"}),
        "A technical defect on this page and a search decline on the same "
        "page. The traffic loss has a technical cause.",
    ),
    Correlation(
        frozenset({"pagespeed", "ga4"}),
        "Slow performance and weaker conversion on the same page. Speed is "
        "costing conversions here, not just scoring badly.",
    ),
    Correlation(
        frozenset({"search_console", "ga4"}),
        "Search and behavior agree on the direction of travel for this page.",
    ),
    Correlation(
        frozenset({"vision_ai", "site_guardian"}),
        "Both the rendered page and the raw health check flag this page.",
    ),
)


@dataclass
class Coverage:
    """What ran, what could not, and what broke — computed, never assumed."""

    ran: list[str] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return bool(self.ran) and not self.unavailable and not self.failed

    @property
    def blind_spots(self) -> list[str]:
        spots = [f"{agent}: {reason}" for agent, reason in sorted(self.unavailable.items())]
        spots += [f"{agent}: failed — {error}" for agent, error in sorted(self.failed.items())]
        return spots

    def caveat(self) -> str:
        """The sentence that must precede any verdict when coverage is partial."""
        if self.complete:
            return ""
        missing = sorted(set(self.unavailable) | set(self.failed))
        if not missing:
            # Nothing ran and nothing failed: this is a cold start, not a
            # partial picture. Saying "0 of 0 agents did not report" would be
            # both nonsense and falsely reassuring.
            return (
                "Nothing has been measured yet — no agent has reported in this "
                "installation."
            )
        return (
            f"Incomplete picture: {len(missing)} of {len(missing) + len(self.ran)} "
            f"agents did not report ({', '.join(missing)}). Anything absent from "
            "this board may simply not have been measured."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran": sorted(self.ran),
            "unavailable": self.unavailable,
            "failed": self.failed,
            "complete": self.complete,
            "blind_spots": self.blind_spots,
            "caveat": self.caveat(),
        }

    @classmethod
    def from_results(cls, results: Iterable[CollectorResult]) -> "Coverage":
        coverage = cls()
        for result in results:
            if result.status is CollectorStatus.OK:
                coverage.ran.append(result.agent)
            elif result.status is CollectorStatus.UNAVAILABLE:
                coverage.unavailable[result.agent] = result.reason or "no reason given"
            else:
                coverage.failed[result.agent] = result.error or "no error given"
        return coverage


@dataclass
class PriorityItem:
    """One ranked item on the board, with everything that argues for it."""

    finding: Finding
    bucket: Bucket
    score: float
    reasons: list[str] = field(default_factory=list)
    supporting: list[Finding] = field(default_factory=list)
    correlation_notes: list[str] = field(default_factory=list)
    persistence: int = 1

    @property
    def corroborating_agents(self) -> list[str]:
        return sorted({f.source_agent for f in self.supporting})

    @property
    def is_corroborated(self) -> bool:
        return bool(self.corroborating_agents)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket.value,
            "score": round(self.score, 2),
            "problem": self.finding.problem,
            "url": self.finding.url,
            "entity": self.finding.entity,
            "agent": self.finding.source_agent,
            "severity": self.finding.severity.value,
            "confidence": round(self.finding.confidence, 2),
            "revenue_weight": self.finding.revenue_weight,
            "conversion_blocking": self.finding.conversion_blocking,
            "business_impact": self.finding.business_impact,
            "recommended_action": self.finding.recommended_action,
            "risk_tier": self.finding.risk_tier.value,
            "fingerprint": self.finding.fingerprint(),
            "persistence": self.persistence,
            "reasons": self.reasons,
            "corroborated_by": self.corroborating_agents,
            "correlation_notes": self.correlation_notes,
            "supporting": [
                {"agent": f.source_agent, "problem": f.problem, "severity": f.severity.value}
                for f in self.supporting
            ],
        }


@dataclass
class PriorityBoard:
    """The deterministic answer to 'what do I do first'."""

    items: list[PriorityItem] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    generated_at: str = field(default_factory=lambda: iso(utcnow()))
    narrative: dict[str, Any] = field(default_factory=dict)

    def bucket(self, bucket: Bucket) -> list[PriorityItem]:
        return [i for i in self.items if i.bucket is bucket]

    @property
    def critical_now(self) -> list[PriorityItem]:
        return self.bucket(Bucket.CRITICAL_NOW)

    @property
    def top_action(self) -> PriorityItem | None:
        actionable = [
            i
            for i in self.items
            if i.bucket in (Bucket.CRITICAL_NOW, Bucket.FIX_NEXT, Bucket.GROWTH)
        ]
        return actionable[0] if actionable else None

    def headline(self) -> str:
        """One sentence. Coverage caveat comes first when coverage is partial."""
        caveat = self.coverage.caveat()
        critical = len(self.critical_now)
        if critical:
            core = (
                f"{critical} issue(s) need attention now, starting with "
                f"{self.critical_now[0].finding.problem}"
            )
        elif self.top_action:
            core = f"Nothing critical. Highest priority: {self.top_action.finding.problem}"
        elif not self.coverage.ran:
            return caveat or "No agent reported. Nothing was measured."
        elif caveat:
            # The important distinction: "found nothing" is not "all clear"
            # when we could not look everywhere.
            core = "Nothing actionable found in what could be checked"
        else:
            core = "All monitored pages and channels are healthy"
        return f"{caveat} {core}." if caveat else f"{core}."

    def counts(self) -> dict[str, int]:
        return {b.value: len(self.bucket(b)) for b in Bucket}

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "headline": self.headline(),
            "counts": self.counts(),
            "coverage": self.coverage.to_dict(),
            "buckets": {
                b.value: [i.to_dict() for i in self.bucket(b)] for b in Bucket
            },
            "top_action": self.top_action.to_dict() if self.top_action else None,
            "narrative": self.narrative,
        }


# --------------------------------------------------------------------------
# Correlation
# --------------------------------------------------------------------------


def _group_key(finding: Finding) -> str:
    """What counts as "the same thing" across agents.

    URL when there is one, because that is the only identifier every agent
    shares. Otherwise the entity, which is how channel-level revenue findings
    group. Findings with neither never merge, which is the safe default.
    """
    url = normalize_url(finding.url) if finding.url else ""
    if url:
        return f"url:{url}"
    if finding.entity:
        return f"entity:{finding.entity.strip().lower()}"
    return f"solo:{finding.fingerprint()}"


def _correlation_notes(agents: set[str]) -> list[str]:
    return [c.label for c in CORRELATIONS if c.agents <= agents]


def correlate(findings: Sequence[Finding]) -> list[PriorityItem]:
    """Merge findings that describe the same page or entity across agents.

    The highest-scoring finding leads; the rest become supporting evidence.
    Findings from the *same* agent about the same URL stay separate — one
    agent reporting twice is two problems, not corroboration.
    """
    groups: dict[str, list[Finding]] = {}
    for finding in findings:
        groups.setdefault(_group_key(finding), []).append(finding)

    items: list[PriorityItem] = []
    for key, group in groups.items():
        group.sort(key=lambda f: f.priority_score(), reverse=True)
        by_agent: dict[str, list[Finding]] = {}
        for finding in group:
            by_agent.setdefault(finding.source_agent, []).append(finding)

        if len(by_agent) < 2 or key.startswith("solo:"):
            # No cross-agent agreement here; every finding stands alone.
            items.extend(PriorityItem(finding=f, bucket=Bucket.MONITOR, score=0.0)
                         for f in group)
            continue

        primary = group[0]
        supporting = [
            findings_for[0]
            for agent, findings_for in by_agent.items()
            if agent != primary.source_agent
        ]
        notes = _correlation_notes(set(by_agent))
        items.append(
            PriorityItem(
                finding=primary,
                bucket=Bucket.MONITOR,
                score=0.0,
                supporting=supporting,
                correlation_notes=notes,
            )
        )
        # Anything the same agent said twice about this page is still its own
        # item — merging those would hide a second defect.
        extras = [f for f in by_agent[primary.source_agent][1:]]
        items.extend(
            PriorityItem(finding=f, bucket=Bucket.MONITOR, score=0.0) for f in extras
        )
    return items


# --------------------------------------------------------------------------
# Scoring and bucketing
# --------------------------------------------------------------------------


def score(item: PriorityItem) -> tuple[float, list[str]]:
    """Deterministic score with the reasoning attached.

    Starts from the finding's own business-impact score and adjusts for the
    things only a cross-agent view can know: corroboration and persistence.
    """
    base = item.finding.priority_score()
    reasons = [f"base business-impact score {base:.0f}"]
    total = base

    if item.is_corroborated:
        bonus = 15.0 * len(item.corroborating_agents)
        total += bonus
        reasons.append(
            f"+{bonus:.0f} corroborated by {', '.join(item.corroborating_agents)}"
        )
    if item.correlation_notes:
        total += 10.0
        reasons.append("+10 a known cause-and-effect pattern")
    if item.persistence >= 3:
        total += 10.0
        reasons.append(f"+10 unresolved across {item.persistence} cycles")
    elif item.persistence == 1:
        # Rule 9's logic applied to ranking: one sighting is not yet a trend.
        total -= 5.0
        reasons.append("-5 seen once, not yet confirmed")
    return round(total, 2), reasons


def bucket_for(item: PriorityItem) -> tuple[Bucket, list[str]]:
    """Which of the five buckets, and why. Checked in strict order."""
    f = item.finding
    reasons: list[str] = []

    # A collector that measured something but says the sample is too thin
    # goes to MONITOR whatever it scores. Acting on it is acting on noise.
    if f.evidence.get(VERDICT_WITHHELD):
        return Bucket.MONITOR, ["the collector withheld a verdict on sample size"]

    if f.conversion_blocking:
        return Bucket.CRITICAL_NOW, ["blocks a visitor from converting"]
    if f.severity is Severity.CRITICAL:
        return Bucket.CRITICAL_NOW, ["critical severity"]
    if (
        f.severity is Severity.HIGH
        and f.revenue_weight >= CRITICAL_WEIGHT_FLOOR
        and f.confidence >= 0.7
    ):
        subject = "page" if f.url else "channel"
        return Bucket.CRITICAL_NOW, [
            f"high severity on a revenue-weight-{f.revenue_weight} {subject}, "
            f"confidence {f.confidence:.0%}"
        ]

    if f.confidence < MONITOR_CONFIDENCE_FLOOR:
        return Bucket.MONITOR, [f"confidence {f.confidence:.0%} is too low to act on"]

    # INFO is the severity every collector reserves for upside: striking
    # distance, unmet query demand, a channel earning above target.
    if f.severity is Severity.INFO:
        if f.revenue_weight > 0 or f.source_agent in {"search_console", "revenue"}:
            return Bucket.GROWTH, ["an opportunity rather than a defect"]
        return Bucket.NO_ACTION, ["informational, with no revenue attached"]

    if item.score >= FIX_NEXT_SCORE_FLOOR and f.severity.rank >= Severity.MEDIUM.rank:
        reasons.append(f"score {item.score:.0f} at {f.severity.value} severity")
        if item.persistence == 1 and f.severity is Severity.MEDIUM:
            return Bucket.MONITOR, reasons + ["seen once; confirm before acting"]
        return Bucket.FIX_NEXT, reasons

    if f.revenue_weight == 0 and f.severity.rank <= Severity.LOW.rank:
        return Bucket.NO_ACTION, ["low severity on a page with no revenue weight"]

    return Bucket.MONITOR, [f"below the action floor at score {item.score:.0f}"]


def build_board(
    findings: Sequence[Finding],
    coverage: Coverage,
    *,
    persistence: dict[str, int] | None = None,
) -> PriorityBoard:
    """The whole deterministic pipeline: correlate, score, bucket, rank."""
    persistence = persistence or {}
    items = correlate(findings)

    for item in items:
        item.persistence = max(1, int(persistence.get(item.finding.fingerprint(), 1)))
        item.score, reasons = score(item)
        item.bucket, why = bucket_for(item)
        item.reasons = reasons + why

    order = {b: i for i, b in enumerate(Bucket)}
    items.sort(key=lambda i: (order[i.bucket], -i.score))
    return PriorityBoard(items=items, coverage=coverage)


def board_from_results(
    results: Sequence[CollectorResult],
    *,
    persistence: dict[str, int] | None = None,
) -> PriorityBoard:
    """Build the board straight from a cycle's collector results.

    Findings from ERROR and UNAVAILABLE collectors are not used — those
    results carry no findings by construction — but their agents are recorded
    as blind spots so the board can say what it did not see.
    """
    findings = [f for r in results if r.status is CollectorStatus.OK for f in r.findings]
    return build_board(findings, Coverage.from_results(results), persistence=persistence)


# --------------------------------------------------------------------------
# Optional narration
# --------------------------------------------------------------------------


def narrate(board: PriorityBoard, analyzer: Any, context: dict[str, Any] | None = None) -> None:
    """Let Claude write the human summary — of the board we already decided.

    Anything the model returns that does not correspond to a real finding is
    discarded. It is narrating a decision, not making one, so a bad response
    degrades the prose and cannot reorder the business's priorities.
    """
    if analyzer is None or not getattr(analyzer, "available", False):
        board.narrative = {
            "available": False,
            "reason": getattr(analyzer, "unavailable_reason", "no analyzer configured"),
        }
        return

    findings = [i.finding for i in board.items]
    if not findings:
        board.narrative = {"available": False, "reason": "no findings to narrate"}
        return

    payload = dict(context or {})
    payload["deterministic_board"] = board.counts()
    payload["coverage"] = board.coverage.to_dict()

    try:
        report = analyzer.prioritize(findings, payload)
    except Exception as exc:  # noqa: BLE001 - narration is never load-bearing
        logger.warning("narration failed: %s", exc)
        board.narrative = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
        return

    known = {i.finding.problem.strip().lower() for i in board.items}

    def keep(lines: list[str]) -> list[str]:
        """Drop any line that does not reference a finding we actually have."""
        out = []
        for line in lines:
            lowered = line.strip().lower()
            if any(problem[:60] in lowered or lowered[:60] in problem for problem in known):
                out.append(line)
        return out

    board.narrative = {
        "available": bool(report.available),
        "headline": report.headline,
        "single_highest_priority_action": report.single_highest_priority_action,
        "critical_now": keep(report.critical_now),
        "fix_next": keep(report.fix_next),
        "growth_opportunities": keep(report.growth_opportunities),
        "monitor": keep(report.monitor),
        "discarded_unmatched": (
            len(report.critical_now) + len(report.fix_next) + len(report.growth_opportunities)
            - len(keep(report.critical_now))
            - len(keep(report.fix_next))
            - len(keep(report.growth_opportunities))
        ),
        "note": (
            "Buckets above are the deterministic engine's. This narration "
            "explains them and cannot change them."
        ),
    }
