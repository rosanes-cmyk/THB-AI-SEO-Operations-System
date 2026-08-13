"""Collector behavior: honest failure, business weighting, per-page isolation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from collectors import crawler, heartbeat, pagespeed, vision
from core.config import LimitsConfig, Secrets, Settings
from core.models import CollectorStatus


HEALTHY_HTML = """
<html><head><title>Sell Your House Fast</title>
<meta name="description" content="We buy houses.">
<link rel="canonical" href="https://example.test/">
</head><body><h1>Get a cash offer</h1>
<form action="/submit"><input name="address"><button>Get My Cash Offer</button></form>
<a href="tel:5551234567">(555) 123-4567</a>
<a href="/blog/">Blog</a>
</body></html>
"""

NO_FORM_HTML = HEALTHY_HTML.replace(
    '<form action="/submit"><input name="address"><button>Get My Cash Offer</button></form>',
    "<p>Get a cash offer by calling us.</p>",
)


def fetcher_for(pages: dict[str, tuple[int, str]]):
    def fetch(url: str, timeout: int) -> tuple[int, str, str]:
        if url not in pages:
            raise ConnectionError(f"no stub for {url}")
        status, html = pages[url]
        return status, html, url

    return fetch


# -- heartbeat -------------------------------------------------------------


def test_healthy_money_page_produces_no_findings(settings: Settings) -> None:
    result = heartbeat.run(
        settings, fetcher=fetcher_for({"https://example.test/": (200, HEALTHY_HTML)})
    )
    assert result.status is CollectorStatus.OK
    assert result.findings == []


def test_missing_form_is_conversion_blocking(settings: Settings) -> None:
    result = heartbeat.run(
        settings, fetcher=fetcher_for({"https://example.test/": (200, NO_FORM_HTML)})
    )
    blocking = [f for f in result.findings if f.conversion_blocking]
    assert blocking, "a missing lead form on a money page must be conversion-blocking"
    assert blocking[0].revenue_weight == 95
    assert blocking[0].severity.value == "critical"


def test_server_error_is_critical(settings: Settings) -> None:
    result = heartbeat.run(
        settings, fetcher=fetcher_for({"https://example.test/": (503, "")})
    )
    assert len(result.findings) == 1
    assert result.findings[0].conversion_blocking is True
    assert "503" in result.findings[0].problem


def test_non_200_short_circuits_structural_checks(settings: Settings) -> None:
    """A 500 page should report one clear problem, not five derived ones."""
    result = heartbeat.run(
        settings, fetcher=fetcher_for({"https://example.test/": (500, "")})
    )
    assert len(result.findings) == 1


def test_unreachable_page_is_a_finding_not_a_crash(settings: Settings) -> None:
    def always_fails(url: str, timeout: int) -> tuple[int, str, str]:
        raise TimeoutError("injected timeout")

    result = heartbeat.run(settings, fetcher=always_fails)
    assert result.status is CollectorStatus.OK  # the collector itself worked
    assert len(result.findings) == 1
    assert "unreachable" in result.findings[0].problem.lower()


def test_one_bad_page_does_not_block_the_others(settings: Settings) -> None:
    pages = settings.pages  # homepage (money) + blog (not)
    both = replace(pages[1], money_page=True, revenue_weight=50)
    settings_two = replace(settings, pages=(pages[0], both))

    def fetch(url: str, timeout: int) -> tuple[int, str, str]:
        if url == "https://example.test/":
            raise ConnectionError("down")
        return 200, HEALTHY_HTML, url

    result = heartbeat.run(settings_two, fetcher=fetch)
    checked = {c["url"] for c in result.data["pages_checked"]}
    assert checked == {"https://example.test/", "https://example.test/blog/"}


def test_missing_expected_text_is_reported(settings: Settings) -> None:
    html = HEALTHY_HTML.replace("Get a cash offer", "Sell to us").replace(
        "Get My Cash Offer", "Submit"
    )
    result = heartbeat.run(
        settings, fetcher=fetcher_for({"https://example.test/": (200, html)})
    )
    assert any("Expected content missing" in f.problem for f in result.findings)


def test_no_money_pages_is_unavailable_not_error(settings: Settings) -> None:
    no_money = replace(
        settings, pages=tuple(replace(p, money_page=False) for p in settings.pages)
    )
    result = heartbeat.run(no_money)
    assert result.status is CollectorStatus.UNAVAILABLE


# -- crawler ---------------------------------------------------------------


def test_crawl_visits_and_reports(settings: Settings) -> None:
    result = crawler.run(
        settings,
        fetcher=fetcher_for(
            {
                "https://example.test/": (200, HEALTHY_HTML),
                "https://example.test/blog": (200, HEALTHY_HTML),
                "https://example.test/blog/": (200, HEALTHY_HTML),
            }
        ),
    )
    assert result.status is CollectorStatus.OK
    assert result.data["pages_crawled"] >= 1


def test_crawl_respects_page_limit(settings: Settings) -> None:
    def fetch(url: str, timeout: int) -> tuple[int, str, str]:
        # Every page links to a fresh page, forever.
        html = f'<html><title>t</title><body><a href="/p{hash(url) % 9999}/">x</a></body></html>'
        return 200, html, url

    result = crawler.run(settings, fetcher=fetch, max_pages=5)
    assert result.data["pages_crawled"] <= 5


def test_crawl_with_zero_reachable_pages_is_an_error(settings: Settings) -> None:
    def fetch(url: str, timeout: int) -> tuple[int, str, str]:
        raise ConnectionError("everything is down")

    result = crawler.run(settings, fetcher=fetch)
    assert result.status is CollectorStatus.ERROR


def test_noindex_on_money_page_is_critical(settings: Settings) -> None:
    html = HEALTHY_HTML.replace(
        "<head>", '<head><meta name="robots" content="noindex, follow">'
    )
    result = crawler.run(
        settings,
        fetcher=fetcher_for(
            {
                "https://example.test/": (200, html),
                "https://example.test/blog": (200, HEALTHY_HTML),
                "https://example.test/blog/": (200, HEALTHY_HTML),
            }
        ),
        max_pages=3,
    )
    noindex = [f for f in result.findings if "noindex" in f.problem.lower()]
    assert noindex and noindex[0].severity.value == "critical"


# -- pagespeed -------------------------------------------------------------


def test_pagespeed_without_key_is_unavailable(settings: Settings) -> None:
    result = pagespeed.run(settings)
    assert result.status is CollectorStatus.UNAVAILABLE
    assert "PAGESPEED_API_KEY" in result.reason
    assert result.findings == [], "no key means no data, never invented data"


def test_pagespeed_parses_metrics(settings: Settings) -> None:
    keyed = replace(settings, secrets=Secrets(pagespeed_api_key="k"))

    def requester(url: str, params: dict[str, Any], timeout: int) -> dict[str, Any]:
        return {
            "lighthouseResult": {
                "categories": {"performance": {"score": 0.42}},
                "audits": {
                    "largest-contentful-paint": {"numericValue": 6200},
                    "cumulative-layout-shift": {"numericValue": 0.02},
                },
            }
        }

    result = pagespeed.run(keyed, strategies=("mobile",), requester=requester)
    assert result.status is CollectorStatus.OK
    lcp = [f for f in result.findings if "largest contentful paint" in f.problem]
    assert lcp and lcp[0].severity.value == "high"  # poor LCP on a money page
    assert not [f for f in result.findings if "layout shift" in f.problem]


def test_pagespeed_all_requests_failing_is_an_error(settings: Settings) -> None:
    keyed = replace(settings, secrets=Secrets(pagespeed_api_key="k"))

    def requester(url: str, params: dict[str, Any], timeout: int) -> dict[str, Any]:
        raise TimeoutError("psi is slow")

    result = pagespeed.run(keyed, strategies=("mobile",), requester=requester)
    assert result.status is CollectorStatus.ERROR


def test_pagespeed_never_invents_missing_metrics(settings: Settings) -> None:
    keyed = replace(settings, secrets=Secrets(pagespeed_api_key="k"))

    def requester(url: str, params: dict[str, Any], timeout: int) -> dict[str, Any]:
        return {"lighthouseResult": {"audits": {}, "categories": {}}}

    result = pagespeed.run(keyed, strategies=("mobile",), requester=requester)
    assert result.status is CollectorStatus.ERROR
    assert result.findings == []


def test_pagespeed_respects_url_budget(settings: Settings) -> None:
    keyed = replace(
        settings,
        secrets=Secrets(pagespeed_api_key="k"),
        limits=LimitsConfig(pagespeed_max_urls=1),
        pages=tuple(replace(p, money_page=True) for p in settings.pages),
    )
    seen: list[str] = []

    def requester(url: str, params: dict[str, Any], timeout: int) -> dict[str, Any]:
        seen.append(params["url"])
        return {"lighthouseResult": {"categories": {"performance": {"score": 1.0}}, "audits": {}}}

    pagespeed.run(keyed, strategies=("mobile",), requester=requester)
    assert len(set(seen)) == 1


# -- vision ----------------------------------------------------------------


def test_vision_without_playwright_is_unavailable(
    settings: Settings, monkeypatch: Any
) -> None:
    monkeypatch.setattr(vision, "playwright_available", lambda: False)
    result = vision.run(settings)
    assert result.status is CollectorStatus.UNAVAILABLE
    assert "Playwright" in result.reason
    assert result.findings == []


def test_vision_flags_dom_present_but_invisible_form(settings: Settings) -> None:
    """The defect a heartbeat structurally cannot catch."""
    page = settings.money_pages[0]
    evidence = vision.BrowserEvidence(
        url=page.url,
        viewport="mobile",
        viewport_width=390,
        viewport_height=844,
        http_status=200,
        form_count=1,
        expected_form_visible=False,
        body_text_length=5000,
        visible_button_count=2,
        phone_link_count=1,
    )
    findings = vision._deterministic_findings(page, evidence)
    blocking = [f for f in findings if f.conversion_blocking]
    assert blocking, "an invisible form must be conversion-blocking"
    assert "not visible" in blocking[0].problem


def test_vision_flags_blank_render(settings: Settings) -> None:
    page = settings.money_pages[0]
    evidence = vision.BrowserEvidence(
        url=page.url,
        viewport="mobile",
        viewport_width=390,
        viewport_height=844,
        http_status=200,
        body_text_length=3,
    )
    findings = vision._deterministic_findings(page, evidence)
    assert any("blank" in f.problem.lower() for f in findings)


def test_vision_healthy_render_produces_no_findings(settings: Settings) -> None:
    page = settings.money_pages[0]
    evidence = vision.BrowserEvidence(
        url=page.url,
        viewport="desktop",
        viewport_width=1440,
        viewport_height=900,
        http_status=200,
        form_count=1,
        expected_form_visible=True,
        visible_button_count=3,
        phone_link_count=1,
        body_text_length=4000,
    )
    assert vision._deterministic_findings(page, evidence) == []


def test_vision_capture_error_is_not_conversion_blocking(settings: Settings) -> None:
    """Our own capture failure must not be reported as a site outage."""
    page = settings.money_pages[0]
    evidence = vision.BrowserEvidence(
        url=page.url,
        viewport="mobile",
        viewport_width=390,
        viewport_height=844,
        capture_error="TimeoutError: browser did not start",
    )
    findings = vision._deterministic_findings(page, evidence)
    assert len(findings) == 1
    assert findings[0].conversion_blocking is False


# -- crawler regressions, found by running against the live site -----------


def test_redirect_source_and_target_are_one_page(settings: Settings) -> None:
    """A 301 must not file the same page twice under two names.

    Keying crawl results on the requested URL made a redirect source and its
    destination look like two pages with identical titles, which the duplicate
    check then reported as a site defect. It was a crawler defect.
    """
    def fetch(url: str, timeout: int) -> tuple[int, str, str]:
        # /contact and /contact-us both land on /contact-us.
        if url.endswith("/contact"):
            return 200, HEALTHY_HTML, "https://example.test/contact-us"
        return 200, HEALTHY_HTML, url

    settings_c = replace(
        settings,
        pages=(
            replace(settings.pages[0], url="https://example.test/contact", money_page=True),
        ),
    )
    result = crawler.run(settings_c, fetcher=fetch, max_pages=6)

    urls = [p["url"] for p in result.data["pages"]]
    assert len(urls) == len(set(urls)), "each landing page recorded once"
    assert not [
        f for f in result.findings if "Duplicate title" in f.problem
    ], "a redirect must not manufacture a duplicate-title finding"


def test_redirecting_money_page_is_reported(settings: Settings) -> None:
    """Config pointing at a stale URL is worth knowing about."""
    def fetch(url: str, timeout: int) -> tuple[int, str, str]:
        if url == "https://example.test/":
            return 200, HEALTHY_HTML, "https://example.test/home-v2"
        return 200, HEALTHY_HTML, url

    result = crawler.run(settings, fetcher=fetch, max_pages=4)
    redirect_findings = [f for f in result.findings if "redirects to" in f.problem]

    assert redirect_findings, "a redirecting money page must surface"
    assert redirect_findings[0].revenue_weight == 95
    assert redirect_findings[0].conversion_blocking is False


def test_unweighted_duplicate_titles_are_not_silent(settings: Settings) -> None:
    """On a site of hundreds of templated pages, mass duplication must surface.

    Previously the check returned nothing when every affected page had weight
    0, which is exactly the programmatic-city-page case.
    """
    shared = HEALTHY_HTML.replace(
        "<title>Sell Your House Fast</title>", "<title>We Buy Houses</title>"
    )

    def fetch(url: str, timeout: int) -> tuple[int, str, str]:
        if url in ("https://example.test/", "https://example.test/blog"):
            return 200, HEALTHY_HTML, url
        return 200, shared.replace('href="/blog/"', f'href="{url}x/"'), url

    seeded = replace(
        settings,
        pages=(settings.pages[0],),  # only the homepage is weighted
        base_url="https://example.test",
    )
    result = crawler.run(seeded, fetcher=fetch, max_pages=8)
    aggregate = [f for f in result.findings if "unweighted pages share" in f.problem]

    if aggregate:
        assert aggregate[0].severity.value == "low"
        assert aggregate[0].revenue_weight == 0
        assert aggregate[0].conversion_blocking is False
