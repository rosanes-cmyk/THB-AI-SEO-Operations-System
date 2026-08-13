"""Stage 4 — Search Console collector.

Every test injects a fake querier. Nothing here touches Google, so the suite
runs on a machine with no credentials and stays deterministic.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from collectors import search_console as sc
from core.models import CollectorStatus, Severity


def fake_querier(by_window: dict[tuple[str, str], dict[str, list[dict[str, Any]]]]):
    """Build a querier keyed on (startDate, dimension)."""

    def query(_property: str, body: dict[str, Any]) -> dict[str, Any]:
        key = (body["startDate"], body["dimensions"][0])
        return {"rows": by_window.get(key, [])}

    return query


def row(key: str, clicks: int, impressions: int, position: float) -> dict[str, Any]:
    return {
        "keys": [key],
        "clicks": clicks,
        "impressions": impressions,
        "ctr": (clicks / impressions) if impressions else 0.0,
        "position": position,
    }


# -- windows ---------------------------------------------------------------


def test_windows_respect_the_two_day_reporting_lag():
    current, prior = sc.windows(date(2026, 3, 30), 28)
    assert current[1] == date(2026, 3, 28)  # not the 29th
    assert current[0] == date(2026, 3, 1)
    assert prior[1] == date(2026, 2, 28)
    assert prior[0] == date(2026, 2, 1)
    assert (current[1] - current[0]) == (prior[1] - prior[0])


# -- honest unavailability -------------------------------------------------


def test_no_credentials_reports_unavailable_not_failure(settings):
    result = sc.run(settings)
    assert result.status is CollectorStatus.UNAVAILABLE
    assert "GOOGLE_APPLICATION_CREDENTIALS" in result.reason
    assert result.findings == []


def test_empty_response_reports_unavailable_rather_than_zero(settings):
    result = sc.run(settings, querier=fake_querier({}), today=date(2026, 3, 30))
    assert result.status is CollectorStatus.UNAVAILABLE
    assert "no rows" in result.reason
    # Critically: it does not claim 0 clicks.
    assert "totals" not in result.data


def test_permission_error_names_the_fix(settings):
    def boom(_p, _b):
        raise RuntimeError("HttpError 403 insufficient permission")

    result = sc.run(settings, querier=boom, today=date(2026, 3, 30))
    assert result.status is CollectorStatus.ERROR
    assert "service account" in result.error


def test_quota_error_is_distinguished_from_permission(settings):
    def boom(_p, _b):
        raise RuntimeError("HttpError 429 quota exceeded")

    result = sc.run(settings, querier=boom, today=date(2026, 3, 30))
    assert result.status is CollectorStatus.ERROR
    assert "quota" in result.error.lower()


# -- volume floors ---------------------------------------------------------


def test_tiny_numbers_do_not_produce_a_finding(settings):
    """4 clicks to 1 is an 75% "collapse" and complete noise."""
    home = "https://example.test/"
    q = fake_querier(
        {
            ("2026-03-01", "page"): [row(home, 1, 30, 8.0)],
            ("2026-02-01", "page"): [row(home, 4, 40, 7.0)],
            ("2026-03-01", "query"): [],
            ("2026-02-01", "query"): [],
        }
    )
    result = sc.run(settings, querier=q, today=date(2026, 3, 30))
    assert result.ok
    assert [f for f in result.findings if "Clicks down" in f.problem] == []


def test_real_decline_on_a_money_page_is_high_severity(settings):
    home = "https://example.test/"
    q = fake_querier(
        {
            ("2026-03-01", "page"): [row(home, 60, 3000, 6.0)],
            ("2026-02-01", "page"): [row(home, 120, 3100, 5.0)],
            ("2026-03-01", "query"): [],
            ("2026-02-01", "query"): [],
        }
    )
    result = sc.run(settings, querier=q, today=date(2026, 3, 30))
    declines = [f for f in result.findings if "Clicks down" in f.problem]
    assert len(declines) == 1
    finding = declines[0]
    assert finding.severity is Severity.HIGH
    assert finding.revenue_weight == 95
    assert "50%" in finding.problem
    assert finding.source_agent == "search_console"


def test_same_decline_on_a_low_weight_page_ranks_lower(settings):
    blog = "https://example.test/blog/"
    q = fake_querier(
        {
            ("2026-03-01", "page"): [row(blog, 60, 3000, 6.0)],
            ("2026-02-01", "page"): [row(blog, 120, 3100, 5.0)],
            ("2026-03-01", "query"): [],
            ("2026-02-01", "query"): [],
        }
    )
    result = sc.run(settings, querier=q, today=date(2026, 3, 30))
    declines = [f for f in result.findings if "Clicks down" in f.problem]
    assert len(declines) == 1
    assert declines[0].severity is Severity.LOW
    assert declines[0].priority_score() < 60


# -- opportunity detection -------------------------------------------------


def test_striking_distance_needs_both_position_and_volume(settings):
    home = "https://example.test/"
    near = sc.Comparison(
        key=home, current=sc.Metrics(10, 500, 0.02, 7.2), prior=sc.Metrics()
    )
    assert near.is_striking_distance

    too_few = sc.Comparison(
        key=home, current=sc.Metrics(1, 20, 0.05, 7.2), prior=sc.Metrics()
    )
    assert not too_few.is_striking_distance

    too_deep = sc.Comparison(
        key=home, current=sc.Metrics(10, 500, 0.02, 42.0), prior=sc.Metrics()
    )
    assert not too_deep.is_striking_distance


def test_position_delta_direction_is_not_inverted():
    """Position 5 -> 9 is a decline; the delta must be positive."""
    c = sc.Comparison(
        key="x", current=sc.Metrics(1, 100, 0.01, 9.0), prior=sc.Metrics(1, 100, 0.01, 5.0)
    )
    assert c.position_delta == 4.0

    improved = sc.Comparison(
        key="x", current=sc.Metrics(1, 100, 0.01, 5.0), prior=sc.Metrics(1, 100, 0.01, 9.0)
    )
    assert improved.position_delta == -4.0


def test_missing_prior_position_does_not_read_as_a_collapse():
    c = sc.Comparison(
        key="x", current=sc.Metrics(1, 100, 0.01, 9.0), prior=sc.Metrics(0, 0, 0.0, 0.0)
    )
    assert c.position_delta == 0.0


def test_weak_ctr_query_becomes_an_opportunity(settings):
    q = fake_querier(
        {
            ("2026-03-01", "page"): [row("https://example.test/", 50, 900, 5.0)],
            ("2026-02-01", "page"): [row("https://example.test/", 50, 900, 5.0)],
            ("2026-03-01", "query"): [row("sell my house fast bay area", 3, 4000, 12.0)],
            ("2026-02-01", "query"): [row("sell my house fast bay area", 3, 3900, 12.0)],
        }
    )
    result = sc.run(settings, querier=q, today=date(2026, 3, 30))
    queries = [f for f in result.findings if f.problem.startswith("Query")]
    assert len(queries) == 1
    assert "sell my house fast bay area" in queries[0].problem
    assert queries[0].evidence["query"] == "sell my house fast bay area"


# -- URL joining -----------------------------------------------------------


def test_page_keys_are_normalized_so_the_ga4_join_works(settings):
    """GSC returns www + trailing slash; config may not. They must still join."""
    q = fake_querier(
        {
            ("2026-03-01", "page"): [
                row("https://www.example.test/?utm_source=google", 100, 2000, 4.0)
            ],
            ("2026-02-01", "page"): [row("https://example.test/", 90, 1900, 4.2)],
            ("2026-03-01", "query"): [],
            ("2026-02-01", "query"): [],
        }
    )
    result = sc.run(settings, querier=q, today=date(2026, 3, 30))
    pages = result.data["pages"]
    assert len(pages) == 1, "the same page arrived twice and did not merge"
    assert pages[0]["revenue_weight"] == 95, "config weight did not attach"

    landing = sc.normalized_landing_pages(result)
    assert set(landing) == {"https://example.test/"}
    assert landing["https://example.test/"]["clicks"] == 100


# -- history ---------------------------------------------------------------


def test_history_is_written_dated_and_is_readable(settings):
    q = fake_querier(
        {
            ("2026-03-01", "page"): [row("https://example.test/", 100, 2000, 4.0)],
            ("2026-02-01", "page"): [row("https://example.test/", 90, 1900, 4.2)],
            ("2026-03-01", "query"): [],
            ("2026-02-01", "query"): [],
        }
    )
    result = sc.run(settings, querier=q, today=date(2026, 3, 30))
    path = settings.data_dir / "search_console" / "2026-03-30.json"
    assert path.exists()
    stored = json.loads(path.read_text())
    assert stored["totals"]["clicks"] == 100
    assert stored["window"] == ["2026-03-01", "2026-03-28"]
    assert result.data["history_file"] == str(path)


def test_a_second_day_does_not_overwrite_the_first(settings):
    q = fake_querier(
        {
            ("2026-03-01", "page"): [row("https://example.test/", 100, 2000, 4.0)],
            ("2026-02-01", "page"): [],
            ("2026-03-02", "page"): [row("https://example.test/", 111, 2100, 4.0)],
            ("2026-02-02", "page"): [],
            ("2026-03-01", "query"): [],
            ("2026-02-01", "query"): [],
            ("2026-03-02", "query"): [],
            ("2026-02-02", "query"): [],
        }
    )
    sc.run(settings, querier=q, today=date(2026, 3, 30))
    sc.run(settings, querier=q, today=date(2026, 3, 31))
    directory = settings.data_dir / "search_console"
    assert sorted(p.name for p in directory.glob("*.json")) == [
        "2026-03-30.json",
        "2026-03-31.json",
    ]


# -- isolation -------------------------------------------------------------


def test_an_unexpected_crash_is_contained(settings):
    def boom(_p, _b):
        raise ValueError("something nobody anticipated")

    result = sc.run(settings, querier=boom, today=date(2026, 3, 30))
    assert result.status is CollectorStatus.ERROR
    assert "something nobody anticipated" in result.error
