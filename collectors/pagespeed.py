"""PageSpeed Insights collector.

Without an API key this collector reports UNAVAILABLE rather than guessing
(Rule 7). It never estimates a Core Web Vitals number it did not receive.

Findings are weighted by revenue: a poor LCP on the seller form matters; the
same LCP on the blog index does not get to page the on-call.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import requests

from collectors.base import CollectorResult, collector_guard
from core.config import PageConfig, Settings
from core.models import Finding, RiskTier, Severity

logger = logging.getLogger(__name__)

AGENT = "pagespeed"

API_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# Google's own "needs improvement" / "poor" thresholds.
THRESHOLDS = {
    "largest_contentful_paint_ms": (2500, 4000),
    "cumulative_layout_shift": (0.1, 0.25),
    "interaction_to_next_paint_ms": (200, 500),
    "total_blocking_time_ms": (200, 600),
}

Requester = Callable[[str, dict[str, Any], int], dict[str, Any]]


def _default_requester(url: str, params: dict[str, Any], timeout: int) -> dict[str, Any]:
    response = requests.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _audit_ms(audits: dict[str, Any], key: str) -> float | None:
    node = audits.get(key)
    if not isinstance(node, dict):
        return None
    value = node.get("numericValue")
    return float(value) if isinstance(value, (int, float)) else None


def _extract(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the metrics we care about. Anything absent stays absent."""
    lighthouse = payload.get("lighthouseResult") or {}
    audits = lighthouse.get("audits") or {}
    categories = lighthouse.get("categories") or {}
    perf = categories.get("performance") or {}
    score = perf.get("score")

    metrics: dict[str, Any] = {
        "performance_score": round(score * 100) if isinstance(score, (int, float)) else None,
        "largest_contentful_paint_ms": _audit_ms(audits, "largest-contentful-paint"),
        "cumulative_layout_shift": _audit_ms(audits, "cumulative-layout-shift"),
        "total_blocking_time_ms": _audit_ms(audits, "total-blocking-time"),
        "interaction_to_next_paint_ms": _audit_ms(audits, "interaction-to-next-paint"),
        "first_contentful_paint_ms": _audit_ms(audits, "first-contentful-paint"),
        "speed_index_ms": _audit_ms(audits, "speed-index"),
    }
    return {k: v for k, v in metrics.items() if v is not None}


def _grade(metric: str, value: float) -> str:
    good, poor = THRESHOLDS[metric]
    if value <= good:
        return "good"
    if value <= poor:
        return "needs_improvement"
    return "poor"


def _findings_for(page: PageConfig, strategy: str, metrics: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []

    for metric, value in metrics.items():
        if metric not in THRESHOLDS:
            continue
        grade = _grade(metric, float(value))
        if grade == "good":
            continue
        # On a low-value page, "needs improvement" is not worth a finding.
        if grade == "needs_improvement" and page.revenue_weight < 50:
            continue

        severity = Severity.MEDIUM if grade == "poor" else Severity.LOW
        if grade == "poor" and page.money_page:
            severity = Severity.HIGH

        findings.append(
            Finding(
                source_agent=AGENT,
                problem=f"{metric.replace('_', ' ')} is {grade} on {strategy} ({value:g})",
                url=page.url,
                entity=page.name,
                viewport=strategy,
                severity=severity,
                confidence=0.9,
                revenue_weight=page.revenue_weight,
                conversion_blocking=False,
                business_impact=(
                    "Slow or unstable loading on a revenue page measurably reduces "
                    "form starts and completions."
                    if page.money_page
                    else "Performance is below Google's threshold on a low-value page."
                ),
                evidence={"strategy": strategy, "metric": metric, "value": value, **metrics},
                recommended_action=(
                    "Review render-blocking resources, image sizing, and third-party "
                    "scripts on this template."
                ),
                risk_tier=RiskTier.HUMAN_ONLY,
                verification_plan="Re-run PageSpeed for this URL and strategy.",
            )
        )

    return findings


@collector_guard(AGENT)
def run(
    settings: Settings,
    *,
    pages: tuple[PageConfig, ...] | None = None,
    strategies: tuple[str, ...] = ("mobile", "desktop"),
    requester: Requester | None = None,
) -> CollectorResult:
    key = settings.secrets.pagespeed_api_key
    if not key:
        return CollectorResult.unavailable(
            AGENT,
            "PAGESPEED_API_KEY is not set; no PageSpeed data collected",
        )

    targets = pages if pages is not None else settings.money_pages
    targets = targets[: settings.limits.pagespeed_max_urls]
    if not targets:
        return CollectorResult.unavailable(AGENT, "no pages configured for PageSpeed")

    request = requester or _default_requester
    timeout = settings.limits.pagespeed_timeout_seconds

    findings: list[Finding] = []
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for page in targets:
        for strategy in strategies:
            try:
                payload = request(
                    API_URL,
                    {
                        "url": page.url,
                        "strategy": strategy,
                        "category": "performance",
                        "key": key,
                    },
                    timeout,
                )
            except Exception as exc:  # noqa: BLE001 - per-URL isolation
                # Note the URL but never the key.
                errors.append(
                    {
                        "url": page.url,
                        "strategy": strategy,
                        "error": f"{type(exc).__name__}: {exc}"[:200],
                    }
                )
                continue

            metrics = _extract(payload)
            if not metrics:
                errors.append(
                    {
                        "url": page.url,
                        "strategy": strategy,
                        "error": "response contained no usable metrics",
                    }
                )
                continue

            results.append({"url": page.url, "strategy": strategy, **metrics})
            findings.extend(_findings_for(page, strategy, metrics))

    if not results and errors:
        return CollectorResult.failed(
            AGENT, "every PageSpeed request failed", errors=errors
        )

    return CollectorResult(
        agent=AGENT,
        findings=findings,
        data={"measurements": results, "errors": errors},
    )
