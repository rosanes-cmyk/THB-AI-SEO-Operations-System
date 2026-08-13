"""Claude reasoning and vision.

Two jobs:

  inspect_screenshot()  look at a rendered money page the way a seller would,
                        and say whether conversion is blocked.
  prioritize()          take every agent's normalized findings and rank them by
                        business impact, not textbook SEO severity.

Three rules shape the implementation:

  Rule 7  Claude cannot silently guess. Every response is schema-validated
          before it is trusted. A malformed response, a refusal, an API error,
          or a missing key yields an `unknown` verdict — never a fabricated
          finding.
  Rule 8  A Claude outage must not stop collection. Nothing here raises into
          the caller; failures are returned as data.
  Cost    Vision is expensive relative to an HTTP GET, so calls are rate
          limited per hour and images are sent at the screenshot's own size.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config import PageConfig, Settings
from core.models import Finding, RiskTier, Severity

logger = logging.getLogger(__name__)

AGENT = "vision_ai"

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # Claude's per-image limit

VISION_SYSTEM_PROMPT = """\
You are the visual quality inspector for a real-estate company that buys \
houses for cash. The screenshots you review are of pages whose only job is to \
get a motivated home seller to submit a form or call a phone number.

Judge the page the way a first-time visitor on this exact device would see it. \
Your priority order is fixed:

1. Can the visitor find and use the lead form or phone CTA?
2. Is anything covering, clipping, or hiding the primary call to action?
3. Is the layout broken in a way that destroys trust or readability?

Report only defects you can actually see in the image, and describe the visual \
evidence for each one. Unusual or dated design is NOT a defect - do not \
critique taste, color choices, or style. If the page looks fine, say so.

If the screenshot is too ambiguous to judge, return verdict "unknown" with a \
low confidence rather than guessing.\
"""

PRIORITY_SYSTEM_PROMPT = """\
You are the operations lead for a company that buys houses for cash. You are \
given normalized findings from several monitoring agents. Rank them by real \
business and revenue impact, not by textbook SEO severity.

Ground rules:
- A broken seller form on a money page is an emergency.
- A hidden phone CTA on mobile is important.
- A ranking or traffic decline on a page that produces contracts is important.
- A duplicate title on an irrelevant old blog post is not automatically important.
- Never invent findings. Rank only what you were given, using the evidence given.
- Be concise. A human reads this at 7 AM and needs to know what to do first.\
"""

VISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["healthy", "warning", "critical", "unknown"],
        },
        "confidence": {"type": "number"},
        "conversion_blocking": {"type": "boolean"},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low", "info"],
                    },
                    "description": {"type": "string"},
                    "business_impact": {"type": "string"},
                    "visual_evidence": {"type": "string"},
                    "recommended_verification": {"type": "string"},
                },
                "required": [
                    "type",
                    "severity",
                    "description",
                    "business_impact",
                    "visual_evidence",
                    "recommended_verification",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "confidence", "conversion_blocking", "summary", "findings"],
    "additionalProperties": False,
}

PRIORITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "critical_now": {"type": "array", "items": {"type": "string"}},
        "fix_next": {"type": "array", "items": {"type": "string"}},
        "growth_opportunities": {"type": "array", "items": {"type": "string"}},
        "monitor": {"type": "array", "items": {"type": "string"}},
        "no_action": {"type": "array", "items": {"type": "string"}},
        "single_highest_priority_action": {"type": "string"},
    },
    "required": [
        "headline",
        "critical_now",
        "fix_next",
        "growth_opportunities",
        "monitor",
        "no_action",
        "single_highest_priority_action",
    ],
    "additionalProperties": False,
}


@dataclass
class VisionVerdict:
    """Validated AI judgment about one screenshot."""

    url: str
    viewport: str
    verdict: str = "unknown"
    confidence: float = 0.0
    conversion_blocking: bool = False
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def usable(self) -> bool:
        return self.verdict != "unknown" and not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "viewport": self.viewport,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "conversion_blocking": self.conversion_blocking,
            "summary": self.summary,
            "finding_count": len(self.findings),
            "error": self.error,
        }


@dataclass
class PriorityReport:
    headline: str = ""
    critical_now: list[str] = field(default_factory=list)
    fix_next: list[str] = field(default_factory=list)
    growth_opportunities: list[str] = field(default_factory=list)
    monitor: list[str] = field(default_factory=list)
    no_action: list[str] = field(default_factory=list)
    single_highest_priority_action: str = ""
    available: bool = True
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "critical_now": self.critical_now,
            "fix_next": self.fix_next,
            "growth_opportunities": self.growth_opportunities,
            "monitor": self.monitor,
            "no_action": self.no_action,
            "single_highest_priority_action": self.single_highest_priority_action,
            "available": self.available,
            "error": self.error,
        }


class RateLimiter:
    """Sliding one-hour window. Prevents a retry storm from becoming a bill."""

    def __init__(self, max_per_hour: int) -> None:
        self._max = max(1, int(max_per_hour))
        self._calls: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self._calls and now - self._calls[0] > 3600:
            self._calls.popleft()
        if len(self._calls) >= self._max:
            return False
        self._calls.append(now)
        return True

    @property
    def used(self) -> int:
        return len(self._calls)


def _severity(value: str) -> Severity:
    try:
        return Severity(str(value).lower())
    except ValueError:
        return Severity.MEDIUM


def _validate_vision_payload(payload: Any) -> tuple[dict[str, Any] | None, str]:
    """Structural validation of Claude's response.

    Structured outputs make a malformed response unlikely, not impossible, and
    a monitoring system that crashes on unexpected AI output is not a
    monitoring system.
    """
    if not isinstance(payload, dict):
        return None, "response was not a JSON object"

    verdict = payload.get("verdict")
    if verdict not in {"healthy", "warning", "critical", "unknown"}:
        return None, f"invalid verdict: {verdict!r}"

    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None, "confidence was not a number"
    if not 0.0 <= confidence <= 1.0:
        confidence = max(0.0, min(1.0, confidence))

    findings = payload.get("findings")
    if not isinstance(findings, list):
        return None, "findings was not a list"

    clean: list[dict[str, Any]] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or "").strip()
        if not description:
            continue
        clean.append(
            {
                "type": str(item.get("type") or "unspecified")[:80],
                "severity": str(item.get("severity") or "medium"),
                "description": description[:400],
                "business_impact": str(item.get("business_impact") or "")[:400],
                "visual_evidence": str(item.get("visual_evidence") or "")[:400],
                "recommended_verification": str(
                    item.get("recommended_verification") or ""
                )[:300],
            }
        )

    return (
        {
            "verdict": verdict,
            "confidence": confidence,
            "conversion_blocking": bool(payload.get("conversion_blocking", False)),
            "summary": str(payload.get("summary") or "")[:600],
            "findings": clean,
        },
        "",
    )


class ClaudeAnalyzer:
    """Claude client wrapper. Degrades to `unknown` instead of failing."""

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self._settings = settings
        self._model = settings.claude_model
        self._effort = settings.claude_effort
        self._limiter = RateLimiter(settings.limits.claude_max_calls_per_hour)
        self._client = client
        self._client_error = ""

        if client is None:
            if not settings.secrets.anthropic_api_key:
                self._client_error = "ANTHROPIC_API_KEY is not set"
            else:
                try:
                    import anthropic  # noqa: PLC0415

                    self._client = anthropic.Anthropic(
                        api_key=settings.secrets.anthropic_api_key
                    )
                except Exception as exc:  # noqa: BLE001
                    self._client_error = f"{type(exc).__name__}: {exc}"

    @property
    def available(self) -> bool:
        return self._client is not None and not self._client_error

    @property
    def unavailable_reason(self) -> str:
        return self._client_error or ""

    # -- transport --------------------------------------------------------

    def _call_json(
        self,
        *,
        system: str,
        content: list[dict[str, Any]],
        schema: dict[str, Any],
        max_tokens: int = 8000,
    ) -> tuple[dict[str, Any] | None, str]:
        """One structured-output call. Returns (payload, error)."""
        if not self.available:
            return None, self._client_error or "Claude client unavailable"
        if not self._limiter.allow():
            return None, "Claude hourly call budget exhausted; skipping this call"

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_config={
                    "effort": self._effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except Exception as exc:  # noqa: BLE001 - never propagate an API failure
            return None, f"{type(exc).__name__}: {exc}"[:300]

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            return None, "Claude declined this request"
        if stop_reason == "max_tokens":
            return None, "response hit max_tokens before completing"

        text = ""
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text += getattr(block, "text", "") or ""
        if not text.strip():
            return None, "Claude returned no text content"

        try:
            return json.loads(text), ""
        except json.JSONDecodeError as exc:
            return None, f"response was not valid JSON: {exc}"[:200]

    # -- vision -----------------------------------------------------------

    def inspect_screenshot(self, page: PageConfig, evidence: Any) -> VisionVerdict | None:
        """Judge one screenshot. Returns None only when there is nothing to judge."""
        shot = Path(getattr(evidence, "screenshot_path", "") or "")
        viewport = str(getattr(evidence, "viewport", "") or "")

        if not shot.exists():
            return None

        try:
            raw_bytes = shot.read_bytes()
        except OSError as exc:
            return VisionVerdict(
                url=page.url, viewport=viewport, error=f"could not read screenshot: {exc}"
            )
        if len(raw_bytes) > MAX_IMAGE_BYTES:
            return VisionVerdict(
                url=page.url,
                viewport=viewport,
                error=f"screenshot too large to send ({len(raw_bytes)} bytes)",
            )

        expectations = []
        if page.expect.form:
            expectations.append("a visible lead form")
        if page.expect.phone_cta:
            expectations.append("a visible phone call-to-action")
        if page.expect.min_visible_buttons:
            expectations.append(
                f"at least {page.expect.min_visible_buttons} visible call-to-action button(s)"
            )

        prompt = (
            f"Page: {page.name} ({page.url})\n"
            f"Viewport: {viewport} "
            f"{getattr(evidence, 'viewport_width', '?')}x{getattr(evidence, 'viewport_height', '?')}\n"
            f"This page is {'a REVENUE page' if page.money_page else 'not a revenue page'}.\n"
            f"Expected to contain: {', '.join(expectations) or 'no specific elements configured'}.\n\n"
            "Browser evidence collected alongside this screenshot "
            "(use it to corroborate, but judge primarily from the image):\n"
            f"{json.dumps(evidence.to_dict() if hasattr(evidence, 'to_dict') else {}, indent=2)[:2000]}\n\n"
            "Inspect the screenshot and report conversion-blocking, layout, and "
            "trust defects."
        )

        payload, error = self._call_json(
            system=VISION_SYSTEM_PROMPT,
            content=[
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.standard_b64encode(raw_bytes).decode("ascii"),
                    },
                },
                {"type": "text", "text": prompt},
            ],
            schema=VISION_SCHEMA,
        )

        if error or payload is None:
            logger.info("vision AI unavailable for %s: %s", page.url, error)
            return VisionVerdict(url=page.url, viewport=viewport, error=error)

        clean, validation_error = _validate_vision_payload(payload)
        if clean is None:
            logger.warning("vision AI response rejected for %s: %s", page.url, validation_error)
            return VisionVerdict(
                url=page.url, viewport=viewport, error=validation_error, raw=payload
            )

        verdict = VisionVerdict(
            url=page.url,
            viewport=viewport,
            verdict=clean["verdict"],
            confidence=clean["confidence"],
            conversion_blocking=clean["conversion_blocking"],
            summary=clean["summary"],
            raw=clean,
        )

        if clean["verdict"] in {"healthy", "unknown"}:
            return verdict

        for item in clean["findings"]:
            verdict.findings.append(
                Finding(
                    source_agent=AGENT,
                    problem=item["description"],
                    url=page.url,
                    entity=page.name,
                    viewport=viewport,
                    severity=_severity(item["severity"]),
                    confidence=clean["confidence"],
                    revenue_weight=page.revenue_weight,
                    conversion_blocking=bool(clean["conversion_blocking"]),
                    business_impact=item["business_impact"],
                    evidence={
                        "ai_type": item["type"],
                        "visual_evidence": item["visual_evidence"],
                        "ai_summary": clean["summary"],
                        "browser": (
                            evidence.to_dict() if hasattr(evidence, "to_dict") else {}
                        ),
                    },
                    screenshot_path=str(shot),
                    recommended_action=item["recommended_verification"],
                    risk_tier=RiskTier.HUMAN_ONLY,
                    verification_plan=item["recommended_verification"]
                    or "Re-render the page and re-inspect the screenshot.",
                )
            )

        return verdict

    # -- prioritization ---------------------------------------------------

    def prioritize(
        self, findings: list[Finding], context: dict[str, Any] | None = None
    ) -> PriorityReport:
        """Rank findings by business impact for the daily digest.

        Deterministic scoring runs first and is passed in as evidence, so
        Claude is reasoning across pre-computed signals rather than inventing
        priority from nothing.
        """
        if not findings:
            return PriorityReport(headline="No open findings.", available=True)

        ranked = sorted(findings, key=lambda f: f.priority_score(), reverse=True)[:40]
        payload = [
            {
                "problem": f.problem,
                "url": f.url,
                "page": f.entity,
                "agent": f.source_agent,
                "viewport": f.viewport,
                "severity": f.severity.value,
                "confidence": f.confidence,
                "revenue_weight": f.revenue_weight,
                "conversion_blocking": f.conversion_blocking,
                "business_impact": f.business_impact,
                "deterministic_priority_score": f.priority_score(),
            }
            for f in ranked
        ]

        prompt = (
            "Findings (already scored deterministically; higher score = more "
            "business impact):\n"
            f"{json.dumps(payload, indent=2)[:12000]}\n\n"
            f"Additional context:\n{json.dumps(context or {}, indent=2)[:2000]}\n\n"
            "Group these into CRITICAL NOW, FIX NEXT, GROWTH OPPORTUNITIES, "
            "MONITOR, and NO ACTION. Keep each group short — at most 5 items. "
            "Then name the single highest-priority action."
        )

        response, error = self._call_json(
            system=PRIORITY_SYSTEM_PROMPT,
            content=[{"type": "text", "text": prompt}],
            schema=PRIORITY_SCHEMA,
            max_tokens=4000,
        )

        if error or not isinstance(response, dict):
            logger.info("priority engine unavailable: %s", error)
            return PriorityReport(available=False, error=error or "invalid response")

        def strings(key: str) -> list[str]:
            value = response.get(key)
            if not isinstance(value, list):
                return []
            return [str(v)[:300] for v in value if isinstance(v, (str, int, float))][:5]

        return PriorityReport(
            headline=str(response.get("headline") or "")[:400],
            critical_now=strings("critical_now"),
            fix_next=strings("fix_next"),
            growth_opportunities=strings("growth_opportunities"),
            monitor=strings("monitor"),
            no_action=strings("no_action"),
            single_highest_priority_action=str(
                response.get("single_highest_priority_action") or ""
            )[:400],
            available=True,
        )
