"""Visual recognition collector — browser evidence + AI judgment.

This is the system's main advantage over a traditional SEO platform: it looks
at the page the way a seller actually sees it, at a real mobile viewport, with
JavaScript executed and third-party scripts loaded. HTML presence is not
enough — a form can exist in the DOM and be covered by a cookie banner, pushed
off-screen by a broken Elementor section, or rendered invisible by a CSS
regression, and every one of those is a total conversion failure that the
heartbeat cannot see.

Two halves, deliberately separable:

  capture_evidence()  deterministic browser facts — status, screenshot,
                      console errors, failed requests, element counts. No AI.
  run()               captures evidence, then optionally asks Claude to judge
                      the screenshot. A Claude outage degrades this collector
                      to evidence-only; it does not disable it (Rule 8).

If Playwright is not installed, the collector reports UNAVAILABLE rather than
guessing at what the page looks like (Rule 7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from collectors.base import CollectorResult, collector_guard
from core.config import PageConfig, Settings, Viewport
from core.models import Finding, RiskTier, Severity, utcnow

logger = logging.getLogger(__name__)

AGENT = "vision"

USER_AGENT_SUFFIX = " THB-AI-SEO-Ops/1.0"

# Selectors used for the deterministic element counts. Kept broad on purpose:
# these are corroborating evidence for the AI verdict, not the verdict itself.
FORM_SELECTOR = "form"
BUTTON_SELECTOR = (
    "button:visible, input[type=submit]:visible, input[type=button]:visible, "
    "[role=button]:visible, a.button:visible, a.btn:visible"
)
PHONE_SELECTOR = "a[href^='tel:']"


@dataclass
class BrowserEvidence:
    """Deterministic facts about one render. No inference in this object."""

    url: str
    viewport: str
    viewport_width: int
    viewport_height: int
    final_url: str = ""
    http_status: int = 0
    page_title: str = ""
    form_count: int = 0
    visible_button_count: int = 0
    phone_link_count: int = 0
    expected_form_visible: bool | None = None
    body_text_length: int = 0
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[dict[str, str]] = field(default_factory=list)
    screenshot_path: str = ""
    capture_error: str = ""

    @property
    def rendered(self) -> bool:
        """Did anything meaningful paint?"""
        return self.http_status < 400 and self.body_text_length > 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "viewport": self.viewport,
            "viewport_size": f"{self.viewport_width}x{self.viewport_height}",
            "final_url": self.final_url,
            "http_status": self.http_status,
            "page_title": self.page_title,
            "form_count": self.form_count,
            "visible_button_count": self.visible_button_count,
            "phone_link_count": self.phone_link_count,
            "expected_form_visible": self.expected_form_visible,
            "body_text_length": self.body_text_length,
            "console_errors": self.console_errors[:20],
            "page_errors": self.page_errors[:10],
            "failed_requests": self.failed_requests[:20],
            "screenshot_path": self.screenshot_path,
            "capture_error": self.capture_error,
        }


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure means unavailable
        return False
    return True


def _screenshot_path(settings: Settings, page: PageConfig, viewport: Viewport) -> Path:
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    slug = "".join(c if c.isalnum() else "-" for c in page.name.lower()).strip("-")
    return settings.screenshot_dir / f"{stamp}_{slug}_{viewport.name}.png"


def capture_evidence(
    settings: Settings, page: PageConfig, viewport: Viewport
) -> BrowserEvidence:
    """Render one page at one viewport and record what a visitor would get.

    Never raises: a capture failure is recorded on the evidence object so the
    remaining page/viewport combinations still run.
    """
    from playwright.sync_api import Error as PlaywrightError  # noqa: PLC0415
    from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: PLC0415
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    evidence = BrowserEvidence(
        url=page.url,
        viewport=viewport.name,
        viewport_width=viewport.width,
        viewport_height=viewport.height,
    )
    timeout_ms = settings.limits.http_timeout_seconds * 1000
    settings.screenshot_dir.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                context = browser.new_context(
                    viewport={"width": viewport.width, "height": viewport.height},
                    is_mobile=viewport.is_mobile,
                    has_touch=viewport.is_mobile,
                    device_scale_factor=2 if viewport.is_mobile else 1,
                )
                tab = context.new_page()

                tab.on(
                    "console",
                    lambda msg: (
                        evidence.console_errors.append(str(msg.text)[:300])
                        if msg.type == "error"
                        else None
                    ),
                )
                tab.on("pageerror", lambda err: evidence.page_errors.append(str(err)[:300]))
                tab.on(
                    "requestfailed",
                    lambda req: evidence.failed_requests.append(
                        {
                            "url": str(req.url)[:300],
                            "method": str(req.method),
                            "failure": str((req.failure or "")),
                        }
                    ),
                )

                response = tab.goto(page.url, wait_until="load", timeout=timeout_ms)
                evidence.http_status = response.status if response else 0
                evidence.final_url = tab.url

                # Let late-loading conversion elements (chat widgets, cookie
                # banners, lazy forms) settle — those are exactly what breaks
                # a CTA, so a screenshot taken before them is misleading.
                try:
                    tab.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeout:
                    pass

                evidence.page_title = (tab.title() or "")[:200]
                evidence.form_count = tab.locator(FORM_SELECTOR).count()
                evidence.visible_button_count = tab.locator(BUTTON_SELECTOR).count()
                evidence.phone_link_count = tab.locator(PHONE_SELECTOR).count()

                body_text = tab.inner_text("body") if tab.locator("body").count() else ""
                evidence.body_text_length = len(body_text)

                if page.expect.form:
                    forms = tab.locator(FORM_SELECTOR)
                    visible = False
                    for i in range(min(forms.count(), 10)):
                        try:
                            if forms.nth(i).is_visible():
                                visible = True
                                break
                        except PlaywrightError:
                            continue
                    evidence.expected_form_visible = visible

                shot = _screenshot_path(settings, page, viewport)
                tab.screenshot(path=str(shot), full_page=False)
                evidence.screenshot_path = str(shot)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - one capture must not kill the run
        evidence.capture_error = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning("vision capture failed for %s @%s: %s", page.url, viewport.name, exc)

    return evidence


def _deterministic_findings(page: PageConfig, ev: BrowserEvidence) -> list[Finding]:
    """Findings we can assert from browser facts alone, with no AI involved.

    These are the immediate-alert exceptions: a page that fails entirely, or a
    lead form that is provably not visible, does not wait for a second
    detection or an AI opinion.
    """
    findings: list[Finding] = []

    def add(
        problem: str,
        *,
        severity: Severity,
        blocking: bool,
        impact: str,
        action: str,
    ) -> None:
        findings.append(
            Finding(
                source_agent=AGENT,
                problem=problem,
                url=page.url,
                entity=page.name,
                viewport=ev.viewport,
                severity=severity,
                confidence=0.98,
                revenue_weight=page.revenue_weight,
                conversion_blocking=blocking,
                business_impact=impact,
                evidence=ev.to_dict(),
                screenshot_path=ev.screenshot_path,
                recommended_action=action,
                risk_tier=RiskTier.HUMAN_ONLY,
                verification_plan=(
                    "Re-render this page at this viewport and confirm the element "
                    "is present and visible."
                ),
            )
        )

    if ev.capture_error:
        add(
            f"Page could not be rendered ({ev.capture_error.split(':')[0]})",
            severity=Severity.HIGH,
            blocking=False,  # a capture failure is our problem until corroborated
            impact="We currently have no visual confirmation that this page works.",
            action="Check browser dependencies and whether the page loads manually.",
        )
        return findings

    if ev.http_status >= 500:
        add(
            f"Page returned HTTP {ev.http_status} in a real browser",
            severity=Severity.CRITICAL,
            blocking=page.money_page,
            impact="Visitors are seeing a server error instead of the page.",
            action="Check origin health and recent deploys.",
        )
        return findings

    if not ev.rendered:
        add(
            "Page rendered blank or near-empty",
            severity=Severity.CRITICAL,
            blocking=page.money_page,
            impact=(
                "Sellers see an effectively empty page. Conversion from this page "
                "is zero while this persists."
            ),
            action="Check for a JavaScript error or failed critical resource.",
        )
        return findings

    if page.expect.form and ev.expected_form_visible is False:
        add(
            "Lead form is present in the DOM but not visible to a visitor",
            severity=Severity.CRITICAL,
            blocking=True,
            impact=(
                "The form exists in markup, so uptime checks pass, but no seller "
                "can actually use it. This is a silent total loss of leads."
            ),
            action="Inspect CSS/overlay/layout on this viewport.",
        )

    if page.expect.form and ev.form_count == 0:
        add(
            "No form element rendered on the page",
            severity=Severity.CRITICAL,
            blocking=True,
            impact="No seller can submit a lead from this page.",
            action="Check the form plugin and the most recent page edit.",
        )

    if page.expect.phone_cta and ev.phone_link_count == 0:
        add(
            "No tappable phone link rendered",
            severity=Severity.HIGH,
            blocking=page.money_page,
            impact="Mobile sellers cannot tap to call from this page.",
            action="Confirm the phone CTA renders at this viewport.",
        )

    if page.expect.min_visible_buttons and (
        ev.visible_button_count < page.expect.min_visible_buttons
    ):
        add(
            f"Only {ev.visible_button_count} visible CTAs "
            f"(expected at least {page.expect.min_visible_buttons})",
            severity=Severity.HIGH if page.money_page else Severity.LOW,
            blocking=page.money_page,
            impact="The primary call to action may be hidden or not rendering.",
            action="Inspect the CTA component at this viewport.",
        )

    return findings


@collector_guard(AGENT)
def run(
    settings: Settings,
    *,
    pages: tuple[PageConfig, ...] | None = None,
    viewports: tuple[Viewport, ...] | None = None,
    analyzer: Any = None,
) -> CollectorResult:
    """Capture browser evidence for each page/viewport, then optionally judge it.

    `analyzer` is a `analysis.claude_analyzer.ClaudeAnalyzer` or None. When it
    is None (or its call fails), the collector still returns the deterministic
    findings — vision degrades, it does not disappear.
    """
    if not playwright_available():
        return CollectorResult.unavailable(
            AGENT,
            "Playwright is not installed; run `pip install playwright` and "
            "`playwright install chromium` to enable visual monitoring",
        )

    targets = pages if pages is not None else settings.money_pages
    targets = targets[: settings.limits.vision_max_pages]
    if not targets:
        return CollectorResult.unavailable(AGENT, "no pages configured for vision")

    views = viewports if viewports is not None else settings.viewports

    findings: list[Finding] = []
    captures: list[dict[str, Any]] = []
    ai_verdicts: list[dict[str, Any]] = []

    for page in targets:
        for viewport in views:
            evidence = capture_evidence(settings, page, viewport)
            captures.append(evidence.to_dict())
            findings.extend(_deterministic_findings(page, evidence))

            if analyzer is None or not evidence.screenshot_path:
                continue
            try:
                verdict = analyzer.inspect_screenshot(page, evidence)
            except Exception as exc:  # noqa: BLE001 - AI failure is never fatal
                logger.warning("vision AI judgment failed for %s: %s", page.url, exc)
                continue
            if verdict is None:
                continue
            ai_verdicts.append(verdict.to_dict())
            findings.extend(verdict.findings)

    return CollectorResult(
        agent=AGENT,
        findings=findings,
        data={
            "captures": captures,
            "ai_verdicts": ai_verdicts,
            "pages": len(targets),
            "viewports": [v.name for v in views],
        },
    )
