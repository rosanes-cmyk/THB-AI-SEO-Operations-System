"""Stage 5 — GA4 conversion intelligence and the landing-page join.

An injected runner stands in for the Analytics Data API, so these tests need
no credentials and no network.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from collectors import ga4
from collectors.base import CollectorResult
from core.models import CollectorStatus, Severity


def page_row(url: str, sessions: int, conversions: int, device: str | None = None):
    row: dict[str, Any] = {
        "landingPagePlusQueryString": url,
        "sessions": float(sessions),
        "totalUsers": float(sessions),
        "keyEvents": float(conversions),
        "engagedSessions": float(int(sessions * 0.6)),
    }
    if device is not None:
        row["deviceCategory"] = device
    return row


def fake_runner(totals: dict[str, list], devices: dict[str, list] | None = None):
    """Runner keyed on the request's start date, split by device dimension."""
    devices = devices or {}

    def run(_prop: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
        if "deviceCategory" in spec["dimensions"]:
            return devices.get(spec["start"], [])
        return totals.get(spec["start"], [])

    return run


CURRENT = "2026-03-02"  # 28 days ending 2026-03-29 for today=2026-03-30
PRIOR = "2026-02-02"


def test_windows_end_on_the_last_complete_day():
    current, prior = ga4._windows(date(2026, 3, 30), 28)
    assert current == {"start": "2026-03-02", "end": "2026-03-29"}
    assert prior == {"start": "2026-02-02", "end": "2026-03-01"}


# -- honest unavailability -------------------------------------------------


def test_no_property_id_reports_unavailable(settings):
    result = ga4.run(settings)
    assert result.status is CollectorStatus.UNAVAILABLE
    assert "GA4_PROPERTY_ID" in result.reason


def test_no_rows_reports_unavailable_not_zero(settings):
    result = ga4.run(settings, runner=fake_runner({}), today=date(2026, 3, 30))
    assert result.status is CollectorStatus.UNAVAILABLE
    assert "no rows" in result.reason
    assert "totals" not in result.data


def test_permission_error_names_the_fix(settings):
    def boom(_p, _s):
        raise RuntimeError("403 PermissionDenied on property")

    result = ga4.run(settings, runner=boom, today=date(2026, 3, 30))
    assert result.status is CollectorStatus.ERROR
    assert "Property Access Management" in result.error


def test_crash_is_contained(settings):
    def boom(_p, _s):
        raise ValueError("unexpected")

    result = ga4.run(settings, runner=boom, today=date(2026, 3, 30))
    assert result.status is CollectorStatus.ERROR


# -- the honesty label -----------------------------------------------------


def test_payload_states_that_key_events_are_not_qualified_leads(settings):
    runner = fake_runner({CURRENT: [page_row("https://example.test/", 500, 20)],
                          PRIOR: [page_row("https://example.test/", 500, 20)]})
    result = ga4.run(settings, runner=runner, today=date(2026, 3, 30))
    label = result.data["label"].lower()
    assert "not qualified seller" in label
    assert "crm" in label


def test_no_finding_ever_calls_a_key_event_a_lead(settings):
    runner = fake_runner(
        {
            CURRENT: [page_row("https://example.test/", 900, 4)],
            PRIOR: [page_row("https://example.test/", 880, 40)],
        }
    )
    result = ga4.run(settings, runner=runner, today=date(2026, 3, 30))
    assert result.findings
    for finding in result.findings:
        text = f"{finding.problem} {finding.business_impact}".lower()
        assert " lead" not in text.replace("seller leads on the table", "")
        assert "contract" not in text


# -- the signal that matters most -----------------------------------------


def test_conversions_falling_while_traffic_holds_is_flagged_high(settings):
    """The classic broken-form signature."""
    runner = fake_runner(
        {
            CURRENT: [page_row("https://example.test/", 1000, 6)],
            PRIOR: [page_row("https://example.test/", 1020, 30)],
        }
    )
    result = ga4.run(settings, runner=runner, today=date(2026, 3, 30))
    hits = [f for f in result.findings if "traffic held steady" in f.problem]
    assert len(hits) == 1
    assert hits[0].severity is Severity.HIGH
    assert hits[0].revenue_weight == 95
    assert "80%" in hits[0].problem


def test_conversions_falling_alongside_traffic_is_not_that_signal(settings):
    """Traffic down 60% too — that is a search problem, not a form problem."""
    runner = fake_runner(
        {
            CURRENT: [page_row("https://example.test/", 400, 6)],
            PRIOR: [page_row("https://example.test/", 1020, 30)],
        }
    )
    result = ga4.run(settings, runner=runner, today=date(2026, 3, 30))
    assert [f for f in result.findings if "traffic held steady" in f.problem] == []


def test_small_conversion_counts_are_not_treated_as_collapses(settings):
    runner = fake_runner(
        {
            CURRENT: [page_row("https://example.test/", 1000, 0)],
            PRIOR: [page_row("https://example.test/", 1000, 2)],
        }
    )
    result = ga4.run(settings, runner=runner, today=date(2026, 3, 30))
    assert [f for f in result.findings if "traffic held steady" in f.problem] == []


def test_traffic_with_zero_conversions_questions_the_tracking_too(settings):
    runner = fake_runner(
        {
            CURRENT: [page_row("https://example.test/", 800, 0)],
            PRIOR: [page_row("https://example.test/", 780, 0)],
        }
    )
    result = ga4.run(settings, runner=runner, today=date(2026, 3, 30))
    hits = [f for f in result.findings if "zero recorded conversions" in f.problem]
    assert len(hits) == 1
    # Rule 7: it does not assert the page is broken when tracking may be.
    assert "tracking" in hits[0].business_impact.lower()
    assert "verify" in hits[0].recommended_action.lower()


# -- mobile gap ------------------------------------------------------------


def test_mobile_gap_is_flagged_when_both_devices_have_volume(settings):
    runner = fake_runner(
        totals={
            CURRENT: [page_row("https://example.test/", 1000, 30)],
            PRIOR: [page_row("https://example.test/", 1000, 30)],
        },
        devices={
            CURRENT: [
                page_row("https://example.test/", 700, 7, "mobile"),
                page_row("https://example.test/", 300, 23, "desktop"),
            ]
        },
    )
    result = ga4.run(settings, runner=runner, today=date(2026, 3, 30))
    hits = [f for f in result.findings if "Mobile converts" in f.problem]
    assert len(hits) == 1
    assert hits[0].severity is Severity.HIGH
    assert "phone" in hits[0].business_impact.lower()


def test_mobile_gap_is_silent_without_enough_desktop_volume(settings):
    runner = fake_runner(
        totals={
            CURRENT: [page_row("https://example.test/", 1000, 30)],
            PRIOR: [page_row("https://example.test/", 1000, 30)],
        },
        devices={
            CURRENT: [
                page_row("https://example.test/", 990, 29, "mobile"),
                page_row("https://example.test/", 10, 1, "desktop"),
            ]
        },
    )
    result = ga4.run(settings, runner=runner, today=date(2026, 3, 30))
    assert [f for f in result.findings if "Mobile converts" in f.problem] == []


# -- weighting -------------------------------------------------------------


def test_pages_with_no_revenue_weight_produce_no_findings(settings):
    runner = fake_runner(
        {
            CURRENT: [page_row("https://example.test/random/", 5000, 0)],
            PRIOR: [page_row("https://example.test/random/", 5000, 0)],
        }
    )
    result = ga4.run(settings, runner=runner, today=date(2026, 3, 30))
    assert result.ok
    assert result.findings == []
    # It is still measured and reported, just not alerted on.
    assert result.data["totals"]["sessions"] == 5000


def test_urls_are_normalized_before_weights_attach(settings):
    runner = fake_runner(
        {
            CURRENT: [page_row("https://www.example.test/?gclid=abc", 1000, 2)],
            PRIOR: [page_row("https://example.test/", 1000, 30)],
        }
    )
    result = ga4.run(settings, runner=runner, today=date(2026, 3, 30))
    assert len(result.data["pages"]) == 1
    assert result.data["pages"][0]["revenue_weight"] == 95
    assert result.findings, "weights did not attach, so nothing was evaluated"


# -- the Stage 5 deliverable: the join ------------------------------------


def _joined(settings):
    runner = fake_runner(
        {
            CURRENT: [
                page_row("https://example.test/", 1000, 30),
                page_row("https://example.test/only-ga4", 40, 1),
            ],
            PRIOR: [page_row("https://example.test/", 1000, 30)],
        }
    )
    result = ga4.run(settings, runner=runner, today=date(2026, 3, 30))
    search = {
        "https://example.test/": {
            "clicks": 1200,
            "impressions": 40000,
            "ctr": 0.03,
            "position": 4.1,
            "click_delta": -50,
            "position_delta": 0.3,
        },
        "https://example.test/only-search": {
            "clicks": 90,
            "impressions": 3000,
            "ctr": 0.03,
            "position": 9.0,
            "click_delta": 5,
            "position_delta": -0.2,
        },
    }
    return {r["url"]: r for r in ga4.join_with_search(result, search)}


def test_join_marks_which_sources_each_row_came_from(settings):
    rows = _joined(settings)
    assert rows["https://example.test/"]["sources"] == ["ga4", "search_console"]
    assert rows["https://example.test/only-ga4"]["sources"] == ["ga4"]
    assert rows["https://example.test/only-search"]["sources"] == ["search_console"]


def test_join_never_zero_fills_a_missing_half(settings):
    """A zero would read as "measured zero". None reads as "not measured"."""
    rows = _joined(settings)
    assert rows["https://example.test/only-ga4"]["search"] is None
    assert rows["https://example.test/only-search"]["sessions"] is None
    assert rows["https://example.test/only-search"]["conversions"] is None
    assert rows["https://example.test/only-search"]["clicks_vs_sessions"] is None


def test_join_surfaces_the_clicks_versus_sessions_gap(settings):
    rows = _joined(settings)
    assert rows["https://example.test/"]["clicks_vs_sessions"] == 200
    assert rows["https://example.test/"]["search"]["position"] == 4.1


def test_join_orders_by_revenue_weight_first(settings):
    result = ga4.run(
        settings,
        runner=fake_runner(
            {
                CURRENT: [
                    page_row("https://example.test/busy/", 90000, 1),
                    page_row("https://example.test/", 100, 5),
                ],
                PRIOR: [],
            }
        ),
        today=date(2026, 3, 30),
    )
    ordered = ga4.join_with_search(result, {})
    assert ordered[0]["url"] == "https://example.test/", "traffic outranked revenue"


def test_join_tolerates_an_unavailable_ga4_result():
    """One half missing must not take the join down (Rule 8)."""
    unavailable = CollectorResult.unavailable("ga4", "no credentials")
    rows = ga4.join_with_search(
        unavailable,
        {"https://example.test/": {"clicks": 10, "impressions": 100, "ctr": 0.1,
                                   "position": 3.0, "click_delta": 0, "position_delta": 0.0}},
    )
    assert len(rows) == 1
    assert rows[0]["sources"] == ["search_console"]
    assert rows[0]["sessions"] is None
