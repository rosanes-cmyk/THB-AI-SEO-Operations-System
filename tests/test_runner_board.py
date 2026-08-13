"""The runner's Stage 7 wiring.

These guard the seam that is easiest to break silently: collector statuses
recorded during a cycle must reach the board's coverage, so the digest can
say what it did not measure.
"""

from __future__ import annotations

import pytest

from analysis.priority_engine import Bucket
from collectors.base import CollectorResult
from core.models import Finding, Severity
from runner import OperationsRunner


@pytest.fixture
def runner(settings, monkeypatch):
    settings.ensure_directories()
    r = OperationsRunner(settings)
    r.analyzer = None  # no network, no API key, no narration
    monkeypatch.setattr(r.notifier, "send_alerts", lambda alerts: {
        "delivered": 0, "failed": 0, "spooled": len(alerts)
    })
    monkeypatch.setattr(r.notifier, "send_text", lambda *a, **k: False)
    return r


def finding(agent, problem, url="https://example.test/", **kwargs):
    return Finding(source_agent=agent, problem=problem, url=url, **kwargs)


def test_every_collector_status_reaches_the_boards_coverage(runner):
    runner._process(
        CollectorResult(agent="vision_ai", findings=[finding("vision_ai", "Form hidden")]),
        scope={"vision_ai"},
    )
    runner._process(CollectorResult.unavailable("ga4", "no property id"), scope={"ga4"})
    runner._process(CollectorResult.failed("pagespeed", "HTTP 429"), scope={"pagespeed"})

    coverage = runner.build_board().coverage
    assert coverage.ran == ["vision_ai"]
    assert coverage.unavailable == {"ga4": "no property id"}
    assert coverage.failed == {"pagespeed": "HTTP 429"}
    assert coverage.complete is False


def test_coverage_survives_a_restart(runner, settings):
    runner._process(CollectorResult.unavailable("ga4", "no property id"), scope={"ga4"})
    runner.state.save()

    restarted = OperationsRunner(settings)
    restarted.analyzer = None
    assert restarted.build_board().coverage.unavailable == {"ga4": "no property id"}


def test_a_hand_corrupted_status_block_does_not_take_the_board_down(runner):
    runner.state.counters["collector_status"] = "this is not a mapping"
    board = runner.build_board()
    assert board.coverage.ran == []
    assert board.headline()


def test_persistence_from_the_incident_store_reaches_the_board(runner):
    f = finding(
        "technical_seo", "Missing H1", severity=Severity.MEDIUM,
        revenue_weight=95, confidence=0.9,
    )
    for _ in range(4):
        runner._process(
            CollectorResult(agent="technical_seo", findings=[f]), scope={"technical_seo"}
        )
    item = next(i for i in runner.build_board().items if i.finding.problem == "Missing H1")
    assert item.persistence == 4
    # And having survived four cycles, it is work rather than a maybe.
    assert item.bucket is Bucket.FIX_NEXT


def test_the_digest_renders_with_a_blind_spot_and_names_it(runner):
    runner._process(
        CollectorResult(
            agent="vision_ai",
            findings=[
                finding(
                    "vision_ai",
                    "Seller form is not visible on mobile",
                    severity=Severity.CRITICAL,
                    revenue_weight=95,
                    confidence=0.9,
                    conversion_blocking=True,
                    entity="Homepage",
                    recommended_action="Check the mobile breakpoint.",
                )
            ],
        ),
        scope={"vision_ai"},
    )
    runner._process(CollectorResult.unavailable("revenue", "no revenue data"), scope={"revenue"})

    board = runner.build_board()
    text = runner._format_digest(runner.incidents.open_incidents(), board)

    assert "Incomplete picture" in text
    assert "revenue: no revenue data" in text
    assert "CRITICAL NOW (1)" in text
    assert "*Do this first:* Check the mobile breakpoint." in text
    assert "narration unavailable" in text


def test_the_digest_task_reports_coverage_completeness(runner):
    runner._process(CollectorResult(agent="site_guardian"), scope={"site_guardian"})
    result = runner.task_digest()
    assert result["coverage_complete"] is True
    assert result["critical_now"] == 0

    runner._process(CollectorResult.failed("ga4", "boom"), scope={"ga4"})
    assert runner.task_digest()["coverage_complete"] is False


def test_the_new_collectors_are_actually_scheduled(runner):
    names = {task.name for task in runner.scheduler.tasks}
    assert {"search_console", "ga4", "revenue"} <= names
