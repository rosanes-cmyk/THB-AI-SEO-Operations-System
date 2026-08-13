"""Technical SEO crawl — the 6-hour sweep.

Bounded, polite, and business-weighted. It collects the classic technical
signals (status, title, meta description, H1, canonical, robots directives)
but it scores them by the revenue weight of the page they were found on, not
by textbook SEO severity: a duplicate title on an old blog post is noise, and
the same duplicate title on a money page is not.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urljoin

import requests

from collectors.base import CollectorResult, collector_guard
from core.config import Settings
from core.models import Finding, RiskTier, Severity
from core.urls import is_same_site, normalize_url

logger = logging.getLogger(__name__)

AGENT = "technical_seo"

USER_AGENT = "THB-AI-SEO-Ops/1.0 (+monitoring; contact: ops@twinhomebuyer.com)"

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1_RE = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_META_DESC_RE = re.compile(
    r"<meta[^>]+name\s*=\s*[\"']description[\"'][^>]*content\s*=\s*[\"'](.*?)[\"']",
    re.IGNORECASE | re.DOTALL,
)
_META_ROBOTS_RE = re.compile(
    r"<meta[^>]+name\s*=\s*[\"']robots[\"'][^>]*content\s*=\s*[\"'](.*?)[\"']",
    re.IGNORECASE | re.DOTALL,
)
_CANONICAL_RE = re.compile(
    r"<link[^>]+rel\s*=\s*[\"']canonical[\"'][^>]*href\s*=\s*[\"'](.*?)[\"']",
    re.IGNORECASE | re.DOTALL,
)
_HREF_RE = re.compile(r"<a\b[^>]*href\s*=\s*[\"']([^\"'#]+)[\"']", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

Fetcher = Callable[[str, int], tuple[int, str, str]]


def _default_fetcher(url: str, timeout: int) -> tuple[int, str, str]:
    response = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
        allow_redirects=True,
    )
    return response.status_code, response.text or "", response.url


def _text(match: re.Match[str] | None) -> str:
    if not match:
        return ""
    return _TAG_RE.sub("", match.group(1)).strip()[:300]


@dataclass
class CrawledPage:
    url: str
    status: int
    title: str = ""
    meta_description: str = ""
    h1: str = ""
    canonical: str = ""
    robots: str = ""
    links: list[str] = field(default_factory=list)

    @property
    def noindex(self) -> bool:
        return "noindex" in self.robots.lower()

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "title": self.title,
            "meta_description": self.meta_description,
            "h1": self.h1,
            "canonical": self.canonical,
            "robots": self.robots,
            "noindex": self.noindex,
            "outbound_links": len(self.links),
        }


def _parse(url: str, html: str, status: int, base_url: str) -> CrawledPage:
    links: list[str] = []
    for href in _HREF_RE.findall(html):
        try:
            absolute = urljoin(url, href)
        except ValueError:
            continue
        if absolute.startswith(("http://", "https://")) and is_same_site(absolute, base_url):
            links.append(normalize_url(absolute))

    return CrawledPage(
        url=normalize_url(url),
        status=status,
        title=_text(_TITLE_RE.search(html)),
        meta_description=_text(_META_DESC_RE.search(html)),
        h1=_text(_H1_RE.search(html)),
        canonical=normalize_url(_text(_CANONICAL_RE.search(html))),
        robots=_text(_META_ROBOTS_RE.search(html)),
        links=list(dict.fromkeys(links)),
    )


def _weight_for(settings: Settings, url: str) -> tuple[int, bool, str]:
    """Revenue weight, money-page flag, and display name for a crawled URL."""
    for page in settings.pages:
        if normalize_url(page.url) == normalize_url(url):
            return page.revenue_weight, page.money_page, page.name
    return 0, False, url


def _analyze(settings: Settings, pages: dict[str, CrawledPage]) -> list[Finding]:
    findings: list[Finding] = []

    def add(
        page: CrawledPage,
        problem: str,
        *,
        severity: Severity,
        impact: str,
        action: str,
        blocking: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        weight, money, name = _weight_for(settings, page.url)
        findings.append(
            Finding(
                source_agent=AGENT,
                problem=problem,
                url=page.url,
                entity=name,
                severity=severity,
                confidence=0.97,
                revenue_weight=weight,
                conversion_blocking=blocking,
                business_impact=impact,
                evidence={**page.to_dict(), **(extra or {})},
                recommended_action=action,
                # Every remediation here is a production content change and is
                # therefore gated. Stage 8 may promote some of these to
                # APPROVAL once an adapter is proven; none become AUTO.
                risk_tier=RiskTier.APPROVAL,
                verification_plan="Re-crawl the URL and confirm the element is correct.",
            )
        )
        if money:
            findings[-1].severity = severity

    titles: dict[str, list[str]] = defaultdict(list)

    for page in pages.values():
        weight, money, _ = _weight_for(settings, page.url)

        if page.status >= 400:
            add(
                page,
                f"Internal page returns HTTP {page.status}",
                severity=Severity.HIGH if money else Severity.MEDIUM,
                impact=(
                    "A linked page is broken; visitors and crawlers hitting it "
                    "reach a dead end."
                ),
                action="Fix or redirect this URL, and update the pages linking to it.",
            )
            continue

        if page.noindex and money:
            add(
                page,
                "Money page is set to noindex",
                severity=Severity.CRITICAL,
                impact=(
                    "This revenue page is telling Google not to index it. Organic "
                    "seller traffic to it will go to zero."
                ),
                action="Remove the noindex directive after confirming it was unintended.",
                blocking=False,
            )

        if not page.title:
            add(
                page,
                "Page has no title tag",
                severity=Severity.HIGH if money else Severity.LOW,
                impact="Search results show a fallback title, hurting click-through.",
                action="Add a descriptive, unique title tag.",
            )
        else:
            titles[page.title.lower()].append(page.url)

        if not page.meta_description and money:
            add(
                page,
                "Money page has no meta description",
                severity=Severity.MEDIUM,
                impact="Google writes its own snippet, which usually converts worse.",
                action="Write a meta description focused on the seller's intent.",
            )

        if not page.h1 and money:
            add(
                page,
                "Money page has no H1",
                severity=Severity.MEDIUM,
                impact="Weakens topical clarity on a page that produces leads.",
                action="Add a single H1 matching the page's primary intent.",
            )

        if page.canonical and page.canonical != page.url and money:
            add(
                page,
                f"Money page canonicalizes to a different URL ({page.canonical})",
                severity=Severity.HIGH,
                impact=(
                    "Ranking signals for this revenue page are being handed to "
                    "another URL."
                ),
                action="Confirm the canonical target is intentional.",
            )

    for title, urls in titles.items():
        if len(urls) < 2:
            continue
        # Only escalate duplicates that touch a page we actually care about.
        weights = [_weight_for(settings, u) for u in urls]
        max_weight = max(w for w, _, _ in weights)
        if max_weight == 0:
            continue
        page = pages[urls[0]]
        add(
            page,
            f"Duplicate title shared by {len(urls)} pages",
            severity=Severity.MEDIUM if max_weight >= 50 else Severity.LOW,
            impact=(
                "Duplicate titles make these pages compete with each other in "
                "search."
                if max_weight >= 50
                else "Low-value duplicate; monitor only."
            ),
            action="Give each page a distinct, intent-specific title.",
            extra={"duplicate_title": title[:120], "urls": urls[:10]},
        )

    return findings


@collector_guard(AGENT)
def run(
    settings: Settings,
    *,
    fetcher: Fetcher | None = None,
    max_pages: int | None = None,
) -> CollectorResult:
    """Breadth-first crawl from the base URL, bounded by `crawl_max_pages`."""
    fetch = fetcher or _default_fetcher
    limit = max_pages if max_pages is not None else settings.limits.crawl_max_pages
    timeout = settings.limits.http_timeout_seconds
    delay = settings.limits.crawl_delay_seconds

    seeds = [settings.base_url] + [p.url for p in settings.pages]
    queue: deque[str] = deque(dict.fromkeys(normalize_url(s) for s in seeds if s))
    visited: dict[str, CrawledPage] = {}
    errors: list[dict[str, str]] = []

    while queue and len(visited) < limit:
        url = queue.popleft()
        if not url or url in visited:
            continue

        try:
            status, html, final_url = fetch(url, timeout)
        except Exception as exc:  # noqa: BLE001 - per-URL isolation
            errors.append({"url": url, "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue

        page = _parse(final_url or url, html, status, settings.base_url)
        # Record under the requested URL so link graphs stay consistent even
        # when a redirect changed the address.
        visited[url] = page

        if status < 400:
            for link in page.links:
                if link not in visited and link not in queue:
                    queue.append(link)

        if delay > 0:
            time.sleep(delay)

    if not visited:
        return CollectorResult.failed(
            AGENT,
            "crawl visited zero pages",
            errors=errors,
            truncated=bool(queue),
        )

    findings = _analyze(settings, visited)

    return CollectorResult(
        agent=AGENT,
        findings=findings,
        data={
            "pages_crawled": len(visited),
            "page_limit": limit,
            "truncated": bool(queue),
            "fetch_errors": errors[:25],
            "pages": [p.to_dict() for p in visited.values()],
        },
    )
