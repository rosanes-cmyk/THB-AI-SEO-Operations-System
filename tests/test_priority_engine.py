"""Stage 7 — unified reasoning and priority engine.

The assertions that matter most: buckets are decided in Python and Claude
cannot move them, corroborated findings merge into one item, and a board
built on partial coverage says so before it says anything else.
"""

from __future__ import annotations

import pytest

from analysis.priority_engine import (
    Bucket,
    Coverage,
    PriorityBoard,
    board_from_results,
    build_board,
    correlate,
    narrate,
)
from collectors.base import CollectorResult
from core.models import VERDICT_WITHHELD, CollectorStatus, Finding, Severity


def finding(
    agent: str = "technical_seo",
    problem: str = "something is wrong",
    url: str = "https://example.test/",
    severity: Severity = Severity.MEDIUM,
    weight: int = 50,
    confidence: float = 0.9,
    blocking: bool = False,
    entity: str = "",
    evidence: dict | None = None,
) -> Finding:
    return Finding(
        source_agent=agent,
        problem=problem,
        url=url,
        entity=entity,
        severity=severity,
        confidence=confidence,
        revenue_weight=weight,
        conversion_blocking=blocking,
        evidence=evidence or {},
    )


def board(*findings, coverage=None, persistence=None) -> PriorityBoard:
    return build_board(
        list(findings), coverage or Coverage(ran=["technical_seo"]), persistence=persistence
    )


# ==========================================================================
# Coverage honesty — the most dangerous failure mode
# ==========================================================================


def test_an_empty_board_with_missing_agents_never_says_all_clear(settings):
    coverage = Coverage(
        ran=["site_guardian"],
        unavailable={"ga4": "no credentials"},
        failed={"search_console": "quota exceeded"},
    )
    headline = build_board([], coverage).headline()
    assert "all clear" not in headline.lower()
    assert "healthy" not in headline.lower()
    assert "Incomplete picture" in headline
    assert "ga4" in headline and "search_console" in headline


def test_an_empty_board_with_full_coverage_may_say_healthy():
    coverage = Coverage(ran=["site_guardian", "ga4", "technical_seo"])
    headline = build_board([], coverage).headline()
    assert "healthy" in headline.lower()
    assert "Incomplete" not in headline


def test_the_caveat_precedes_the_verdict_even_when_something_is_critical():
    coverage = Coverage(ran=["vision_ai"], unavailable={"ga4": "no credentials"})
    b = build_board([finding(severity=Severity.CRITICAL)], coverage)
    headline = b.headline()
    assert headline.startswith("Incomplete picture")


def test_no_agent_reporting_at_all_is_stated_plainly():
    b = build_board([], Coverage(failed={"site_guardian": "connection refused"}))
    assert "Incomplete picture" in b.headline()
    assert b.coverage.complete is False


def test_a_cold_start_says_nothing_has_been_measured_not_zero_of_zero():
    b = build_board([], Coverage())
    headline = b.headline()
    assert "0 of 0" not in headline
    assert "Nothing has been measured yet" in headline
    assert "healthy" not in headline.lower()


def test_coverage_is_derived_from_results_not_assumed():
    results = [
        CollectorResult(agent="technical_seo", findings=[finding()]),
        CollectorResult.unavailable("ga4", "GA4_PROPERTY_ID is not set"),
        CollectorResult.failed("pagespeed", "timed out"),
    ]
    b = board_from_results(results)
    assert b.coverage.ran == ["technical_seo"]
    assert b.coverage.unavailable["ga4"] == "GA4_PROPERTY_ID is not set"
    assert b.coverage.failed["pagespeed"] == "timed out"
    assert "ga4: GA4_PROPERTY_ID is not set" in b.coverage.blind_spots


# ==========================================================================
# Correlation
# ==========================================================================


def test_two_agents_on_one_page_become_one_item_with_the_cause_named():
    b = board(
        finding("vision_ai", "Seller form is not visible on mobile", severity=Severity.HIGH,
                weight=95),
        finding("ga4", "Conversions down 80% while traffic held steady",
                severity=Severity.HIGH, weight=95),
    )
    assert len(b.items) == 1
    item = b.items[0]
    assert item.is_corroborated
    assert item.corroborating_agents == ["ga4"] or item.corroborating_agents == ["vision_ai"]
    assert item.correlation_notes
    assert "cause we can see" in " ".join(item.correlation_notes)


def test_corroboration_outranks_an_isolated_finding_of_equal_severity():
    b = board(
        finding("vision_ai", "Form hidden", url="https://example.test/a",
                severity=Severity.HIGH, weight=80),
        finding("ga4", "Conversions collapsed", url="https://example.test/a",
                severity=Severity.HIGH, weight=80),
        finding("technical_seo", "Something else", url="https://example.test/b",
                severity=Severity.HIGH, weight=80),
    )
    assert b.items[0].finding.url == "https://example.test/a"
    assert b.items[0].score > b.items[1].score


def test_the_same_agent_reporting_twice_is_not_corroboration():
    b = board(
        finding("technical_seo", "Missing title"),
        finding("technical_seo", "Missing meta description"),
    )
    assert len(b.items) == 2
    assert not any(i.is_corroborated for i in b.items)


def test_a_second_defect_from_the_lead_agent_is_not_swallowed_by_the_merge():
    items = correlate(
        [
            finding("vision_ai", "Form hidden", severity=Severity.HIGH, weight=95),
            finding("vision_ai", "Phone CTA missing", severity=Severity.HIGH, weight=95),
            finding("ga4", "Conversions down", severity=Severity.HIGH, weight=95),
        ]
    )
    problems = {i.finding.problem for i in items}
    assert "Phone CTA missing" in problems, "a real second defect was merged away"
    assert len(items) == 2


def test_findings_on_different_pages_do_not_merge():
    b = board(
        finding("vision_ai", "Form hidden", url="https://example.test/a"),
        finding("ga4", "Conversions down", url="https://example.test/b"),
    )
    assert len(b.items) == 2


def test_urls_are_normalized_before_grouping():
    """A page seen as www+tracking by one agent and bare by another is one page."""
    b = board(
        finding("vision_ai", "Form hidden", url="https://www.example.test/sell?utm_source=x",
                severity=Severity.HIGH, weight=95),
        finding("ga4", "Conversions down", url="https://example.test/sell",
                severity=Severity.HIGH, weight=95),
    )
    assert len(b.items) == 1


def test_channel_findings_group_on_entity_when_there_is_no_url():
    b = board(
        finding("revenue", "google_ads ROAS 1.2x", url="", entity="google_ads",
                severity=Severity.HIGH, weight=90),
        finding("ga4", "Conversions down", url="", entity="google_ads",
                severity=Severity.MEDIUM, weight=90),
    )
    assert len(b.items) == 1


def test_findings_with_neither_url_nor_entity_never_merge():
    b = board(
        finding("revenue", "one", url="", entity=""),
        finding("ga4", "two", url="", entity=""),
    )
    assert len(b.items) == 2


# ==========================================================================
# Bucketing
# ==========================================================================


def test_conversion_blocking_is_always_critical_now():
    b = board(finding(severity=Severity.LOW, weight=0, blocking=True))
    assert b.items[0].bucket is Bucket.CRITICAL_NOW
    assert "blocks a visitor" in b.items[0].reasons[-1]


def test_high_severity_on_a_money_page_is_critical_now():
    b = board(finding(severity=Severity.HIGH, weight=95, confidence=0.9))
    assert b.items[0].bucket is Bucket.CRITICAL_NOW


def test_the_same_severity_on_a_low_weight_page_is_not_critical():
    b = board(finding(severity=Severity.HIGH, weight=8, confidence=0.9))
    assert b.items[0].bucket is not Bucket.CRITICAL_NOW


def test_low_confidence_goes_to_monitor_however_severe():
    b = board(finding(severity=Severity.HIGH, weight=95, confidence=0.4))
    assert b.items[0].bucket is Bucket.MONITOR
    assert "too low to act on" in b.items[0].reasons[-1]


def test_a_withheld_verdict_goes_to_monitor_however_it_scores():
    b = board(
        finding(
            "revenue",
            "google_ads has 1 deal — too few for a ROAS verdict",
            severity=Severity.HIGH,
            weight=95,
            evidence={VERDICT_WITHHELD: True},
        )
    )
    assert b.items[0].bucket is Bucket.MONITOR
    assert "sample size" in b.items[0].reasons[-1]


def test_info_severity_with_revenue_attached_is_growth_not_noise():
    b = board(finding("search_console", "In striking distance at position 6.2",
                      severity=Severity.INFO, weight=80))
    assert b.items[0].bucket is Bucket.GROWTH


def test_info_severity_with_no_revenue_attached_is_no_action():
    b = board(finding("technical_seo", "Cosmetic note", severity=Severity.INFO, weight=0))
    assert b.items[0].bucket is Bucket.NO_ACTION


def test_search_console_query_opportunities_reach_growth_despite_zero_page_weight():
    """A query has no page, so it has no weight — but demand is still upside."""
    b = board(
        finding("search_console", "Query 'sell my house fast' — 4,000 impressions",
                url="", entity="sell my house fast", severity=Severity.INFO, weight=0)
    )
    assert b.items[0].bucket is Bucket.GROWTH


def test_low_severity_with_no_revenue_weight_is_no_action():
    b = board(finding(severity=Severity.LOW, weight=0))
    assert b.items[0].bucket is Bucket.NO_ACTION


def test_a_medium_seen_once_waits_in_monitor_before_becoming_work():
    f = finding(severity=Severity.MEDIUM, weight=95)
    once = build_board([f], Coverage(ran=["technical_seo"]))
    assert once.items[0].bucket is Bucket.MONITOR
    assert "confirm before acting" in once.items[0].reasons[-1]

    repeated = build_board(
        [f], Coverage(ran=["technical_seo"]), persistence={f.fingerprint(): 4}
    )
    assert repeated.items[0].bucket is Bucket.FIX_NEXT


def test_persistence_raises_the_score_and_a_single_sighting_lowers_it():
    f = finding(severity=Severity.HIGH, weight=60)
    once = build_board([f], Coverage(), persistence={f.fingerprint(): 1}).items[0]
    thrice = build_board([f], Coverage(), persistence={f.fingerprint(): 3}).items[0]
    assert thrice.score > once.score
    assert "seen once" in " ".join(once.reasons)
    assert "3 cycles" in " ".join(thrice.reasons)


def test_every_item_explains_why_it_landed_where_it_did():
    b = board(
        finding(severity=Severity.HIGH, weight=95, blocking=True),
        finding("search_console", "In striking distance", severity=Severity.INFO, weight=80,
                url="https://example.test/x"),
        finding("technical_seo", "Trivia", severity=Severity.LOW, weight=0,
                url="https://example.test/y"),
    )
    for item in b.items:
        assert item.reasons, f"{item.finding.problem} has no explanation"


# ==========================================================================
# Ordering
# ==========================================================================


def test_buckets_order_before_scores():
    b = board(
        finding("search_console", "Huge opportunity", url="https://example.test/a",
                severity=Severity.INFO, weight=100),
        finding("vision_ai", "Form broken", url="https://example.test/b",
                severity=Severity.CRITICAL, weight=10, blocking=True),
    )
    assert b.items[0].bucket is Bucket.CRITICAL_NOW
    assert b.items[1].bucket is Bucket.GROWTH


def test_top_action_skips_monitor_and_no_action():
    b = board(
        finding("technical_seo", "Trivia", url="https://example.test/y",
                severity=Severity.LOW, weight=0),
        finding("search_console", "Striking distance", url="https://example.test/x",
                severity=Severity.INFO, weight=80),
    )
    assert b.top_action is not None
    assert b.top_action.bucket is Bucket.GROWTH


def test_top_action_is_none_when_nothing_is_actionable():
    b = board(finding(severity=Severity.LOW, weight=0))
    assert b.top_action is None


def test_counts_cover_all_five_buckets():
    b = board(finding())
    assert set(b.counts()) == {bucket.value for bucket in Bucket}


def test_serialization_carries_the_coverage_caveat_and_the_reasons():
    coverage = Coverage(ran=["vision_ai"], unavailable={"ga4": "not configured"})
    payload = build_board([finding(blocking=True)], coverage).to_dict()
    assert payload["coverage"]["complete"] is False
    assert payload["coverage"]["caveat"]
    assert payload["buckets"]["critical_now"][0]["reasons"]
    assert payload["top_action"]["fingerprint"]


# ==========================================================================
# Narration cannot override the engine
# ==========================================================================


class FakeReport:
    def __init__(self, **kwargs):
        self.available = True
        self.headline = kwargs.get("headline", "")
        self.critical_now = kwargs.get("critical_now", [])
        self.fix_next = kwargs.get("fix_next", [])
        self.growth_opportunities = kwargs.get("growth", [])
        self.monitor = kwargs.get("monitor", [])
        self.no_action = []
        self.single_highest_priority_action = kwargs.get("action", "")


class FakeAnalyzer:
    available = True
    unavailable_reason = ""

    def __init__(self, report):
        self._report = report
        self.calls = 0

    def prioritize(self, findings, context=None):
        self.calls += 1
        self.last_context = context
        return self._report


def test_narration_cannot_move_an_item_between_buckets():
    b = board(finding("technical_seo", "Trivia", severity=Severity.LOW, weight=0))
    assert b.items[0].bucket is Bucket.NO_ACTION

    narrate(b, FakeAnalyzer(FakeReport(critical_now=["Trivia"], headline="EMERGENCY")))
    # The prose says emergency; the board still says NO ACTION.
    assert b.items[0].bucket is Bucket.NO_ACTION
    assert b.counts()["critical_now"] == 0
    assert b.narrative["headline"] == "EMERGENCY"


def test_an_invented_finding_is_discarded_from_the_narration():
    b = board(finding("technical_seo", "Missing title tag on /about"))
    narrate(
        b,
        FakeAnalyzer(
            FakeReport(
                critical_now=[
                    "Missing title tag on /about",
                    "The database has been deleted",
                ]
            )
        ),
    )
    assert b.narrative["critical_now"] == ["Missing title tag on /about"]
    assert b.narrative["discarded_unmatched"] == 1


def test_a_narration_crash_leaves_the_board_intact():
    class Exploding:
        available = True
        unavailable_reason = ""

        def prioritize(self, *_a, **_k):
            raise RuntimeError("API down")

    b = board(finding(blocking=True))
    narrate(b, Exploding())
    assert b.items[0].bucket is Bucket.CRITICAL_NOW
    assert b.narrative["available"] is False
    assert "API down" in b.narrative["reason"]


def test_no_analyzer_is_reported_not_faked():
    b = board(finding())
    narrate(b, None)
    assert b.narrative["available"] is False


def test_narration_is_given_the_deterministic_board_as_context():
    b = board(finding(blocking=True))
    analyzer = FakeAnalyzer(FakeReport())
    narrate(b, analyzer, {"site": "example.test"})
    assert analyzer.last_context["deterministic_board"]["critical_now"] == 1
    assert analyzer.last_context["site"] == "example.test"
    assert "coverage" in analyzer.last_context


# ==========================================================================
# End to end across all seven agents
# ==========================================================================


def test_a_full_cycle_ranks_money_above_mechanics():
    results = [
        CollectorResult(
            agent="vision_ai",
            findings=[
                finding("vision_ai", "Seller form is not visible on mobile",
                        url="https://example.test/sell", severity=Severity.CRITICAL,
                        weight=95, blocking=True)
            ],
        ),
        CollectorResult(
            agent="ga4",
            findings=[
                finding("ga4", "Conversions down 80% while traffic held steady",
                        url="https://example.test/sell", severity=Severity.HIGH, weight=95)
            ],
        ),
        CollectorResult(
            agent="technical_seo",
            findings=[
                finding("technical_seo", "Duplicate title on 3 old blog posts",
                        url="https://example.test/blog/old", severity=Severity.LOW, weight=0)
            ],
        ),
        CollectorResult(
            agent="search_console",
            findings=[
                finding("search_console", "In striking distance at position 6.2",
                        url="https://example.test/oakland", severity=Severity.INFO, weight=74)
            ],
        ),
        CollectorResult.unavailable("revenue", "no revenue data"),
    ]
    b = board_from_results(results)

    assert b.items[0].bucket is Bucket.CRITICAL_NOW
    assert b.items[0].is_corroborated, "vision and GA4 agreed and were not merged"
    assert b.counts()["critical_now"] == 1
    assert b.counts()["growth_opportunities"] == 1
    assert b.counts()["no_action"] == 1
    # And it admits the revenue picture is missing.
    assert "revenue" in b.headline()
    assert b.coverage.complete is False
