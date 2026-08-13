"""Revenue-page heartbeat — the 5-minute check.

This is the cheapest, most important collector in the system. It answers one
question about every money page: *can a seller still reach the form right now?*

It is deliberately HTML-only and dependency-light so it keeps running when
vision, PageSpeed, and Claude are all down. HTML presence is a weak signal —
a form can exist in the markup and still be invisible to a human — which is
why Stage 3's vision engine exists. The heartbeat's job is to catch the hard
failures (page down, 5xx, form gone from the markup) within five minutes.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

import requests

from collectors.base import CollectorResult, collector_guard
from core.config import PageConfig, Settings
from core.models import Finding, RiskTier, Severity

logger = logging.getLogger(__name__)

AGENT = "site_guardian"

USER_AGENT = "THB-AI-SEO-Ops/1.0 (+monitoring; contact: ops@twinhomebuyer.com)"

_FORM_RE = re.compile(r"<form\b", re.IGNORECASE)
_TEL_RE = re.compile(r"href\s*=\s*[\"']tel:", re.IGNORECASE)
_PHONE_RE = re.compile(r"\(?\b\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")
_BUTTON_RE = re.compile(
    r"(<button\b|<input\b[^>]*type\s*=\s*[\"']?(submit|button)|role\s*=\s*[\"']button[\"'])",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# A fetcher takes (url, timeout) and returns (status_code, text, final_url).
Fetcher = Callable[[str, int], tuple[int, str, str]]


def _default_fetcher(url: str, timeout: int) -> tuple[int, str, str]:
    response = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
        allow_redirects=True,
    )
    return response.status_code, response.text or "", response.url


def _visible_button_count(html: str) -> int:
    return len(_BUTTON_RE.findall(html))


def _check_page(
    page: PageConfig, html: str, status_code: int, final_url: str
) -> list[Finding]:
    """Compare an observed render against this page's configured expectations.

    Only configured expectations are checked. An expectation that was not set
    is not a defect — the system never invents a requirement.
    """
    findings: list[Finding] = []
    expect = page.expect
    evidence_base: dict[str, Any] = {
        "http_status": status_code,
        "final_url": final_url,
        "html_bytes": len(html),
        "title": (_TITLE_RE.search(html).group(1).strip()[:200] if _TITLE_RE.search(html) else ""),
    }

    def add(
        problem: str,
        *,
        severity: Severity,
        blocking: bool,
        impact: str,
        action: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        findings.append(
            Finding(
                source_agent=AGENT,
                problem=problem,
                url=page.url,
                entity=page.name,
                severity=severity,
                confidence=0.99,  # deterministic HTTP/HTML check, not an inference
                revenue_weight=page.revenue_weight,
                conversion_blocking=blocking,
                business_impact=impact,
                evidence={**evidence_base, **(extra or {})},
                recommended_action=action,
                risk_tier=RiskTier.HUMAN_ONLY,
                verification_plan="Re-request the page and confirm the expectation is met.",
            )
        )

    if status_code != expect.status:
        blocking = page.money_page and (status_code >= 500 or status_code == 0 or status_code == 404)
        add(
            f"HTTP {status_code} (expected {expect.status})",
            severity=Severity.CRITICAL if blocking else Severity.HIGH,
            blocking=blocking,
            impact=(
                f"{page.name} is not serving correctly; sellers reaching this page "
                "cannot convert."
                if page.money_page
                else f"{page.name} is not serving the expected status."
            ),
            action="Check origin/host status and recent deploys for this URL.",
        )
        # A non-200 body is not worth further structural assertions.
        return findings

    if expect.form and not _FORM_RE.search(html):
        add(
            "Expected lead form is absent from the page markup",
            severity=Severity.CRITICAL,
            blocking=True,
            impact=(
                f"No seller can submit a lead from {page.name}. Every visitor to "
                "this page is currently a lost opportunity."
            ),
            action=(
                "Check the form plugin/embed on this page and the most recent "
                "content or plugin change."
            ),
        )

    if expect.phone_cta and not (_TEL_RE.search(html) or _PHONE_RE.search(html)):
        add(
            "Expected phone CTA is absent from the page markup",
            severity=Severity.HIGH,
            blocking=True,
            impact=(
                f"Callers cannot find a number on {page.name}; phone leads from "
                "this page are lost."
            ),
            action="Confirm the phone CTA block still renders on this template.",
        )

    missing_text = [
        marker for marker in expect.text_contains if marker.lower() not in html.lower()
    ]
    if missing_text:
        add(
            f"Expected content missing: {', '.join(missing_text)}",
            severity=Severity.HIGH if page.money_page else Severity.MEDIUM,
            blocking=False,
            impact=(
                "Key messaging is missing, which usually means a template or "
                "content block failed to render."
            ),
            action="Compare the live page against the expected template content.",
            extra={"missing_markers": missing_text},
        )

    if expect.min_visible_buttons:
        count = _visible_button_count(html)
        if count < expect.min_visible_buttons:
            add(
                f"Only {count} button-like elements found "
                f"(expected at least {expect.min_visible_buttons})",
                severity=Severity.HIGH if page.money_page else Severity.LOW,
                blocking=page.money_page,
                impact="The primary call to action may not be rendering.",
                action="Verify the CTA component renders for anonymous visitors.",
                extra={"button_count": count},
            )

    return findings


@collector_guard(AGENT)
def run(
    settings: Settings,
    *,
    pages: tuple[PageConfig, ...] | None = None,
    fetcher: Fetcher | None = None,
) -> CollectorResult:
    """Check every money page (or an explicit page list).

    A single page's network failure is captured as a finding for that page; the
    remaining pages are still checked.
    """
    targets = pages if pages is not None else settings.money_pages
    if not targets:
        return CollectorResult.unavailable(
            AGENT, "no money pages configured; nothing to heartbeat"
        )

    fetch = fetcher or _default_fetcher
    timeout = settings.limits.http_timeout_seconds
    findings: list[Finding] = []
    checked: list[dict[str, Any]] = []

    for page in targets:
        try:
            status_code, html, final_url = fetch(page.url, timeout)
        except Exception as exc:  # noqa: BLE001 - per-page isolation
            logger.warning("heartbeat fetch failed for %s: %s", page.url, exc)
            findings.append(
                Finding(
                    source_agent=AGENT,
                    problem=f"Page unreachable ({type(exc).__name__})",
                    url=page.url,
                    entity=page.name,
                    severity=Severity.CRITICAL,
                    confidence=0.95,
                    revenue_weight=page.revenue_weight,
                    conversion_blocking=page.money_page,
                    business_impact=(
                        f"{page.name} did not respond. If this is real, the page is "
                        "producing zero leads right now."
                    ),
                    evidence={"exception": f"{type(exc).__name__}: {exc}"[:300]},
                    recommended_action="Check DNS, TLS, origin health, and WAF rules.",
                    risk_tier=RiskTier.HUMAN_ONLY,
                    verification_plan="Re-request the URL; confirm a 200 response.",
                )
            )
            checked.append({"url": page.url, "status": "unreachable"})
            continue

        findings.extend(_check_page(page, html, status_code, final_url))
        checked.append(
            {
                "url": page.url,
                "name": page.name,
                "http_status": status_code,
                "final_url": final_url,
                "money_page": page.money_page,
            }
        )

    return CollectorResult(
        agent=AGENT,
        findings=findings,
        data={"pages_checked": checked, "page_count": len(checked)},
    )
