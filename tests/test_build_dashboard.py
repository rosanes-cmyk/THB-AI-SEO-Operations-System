"""The dashboard builder's one job: never hand the page a number nobody measured.

The dashboard renders whatever this payload contains. If an unavailable
collector's panel carried a `totals` key, the view would render it as a
measurement — so the guard belongs here, at the boundary, not in the JS.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import build_dashboard as bd  # noqa: E402

from collectors.base import CollectorResult  # noqa: E402
from core.models import Finding, Severity  # noqa: E402


def ok_result(agent="revenue", **data):
    return CollectorResult(
        agent=agent,
        findings=[Finding(source_agent=agent, problem="x", severity=Severity.HIGH)],
        data=data,
    )


def test_an_ok_collector_passes_its_data_through():
    panel = bd.envelope(ok_result(), totals={"clicks": 900})
    assert panel["status"] == "ok"
    assert panel["totals"] == {"clicks": 900}
    assert panel["findings"] == 1


def test_an_unavailable_collector_contributes_no_numbers():
    panel = bd.envelope(
        CollectorResult.unavailable("ga4", "THB_GA4_PROPERTY_ID is not set"),
        totals={"sessions": 0},
        pages=[],
    )
    assert panel["status"] == "unavailable"
    assert panel["reason"] == "THB_GA4_PROPERTY_ID is not set"
    # The zeros were computed by the caller and must not survive the boundary.
    assert "totals" not in panel
    assert "pages" not in panel


def test_a_failed_collector_contributes_no_numbers_either():
    panel = bd.envelope(CollectorResult.failed("search_console", "HTTP 429"), totals={"clicks": 0})
    assert panel["status"] == "error"
    assert panel["reason"] == "HTTP 429"
    assert "totals" not in panel


def test_every_panel_builder_obeys_the_rule():
    unavailable = CollectorResult.unavailable("x", "no credentials")
    for builder in (bd.search_panel, bd.ga4_panel, bd.revenue_panel):
        panel = builder(unavailable)
        assert panel["status"] == "unavailable"
        assert panel["reason"] == "no credentials"
        numeric = {k: v for k, v in panel.items()
                   if k not in {"status", "reason", "findings"}}
        assert numeric == {}, f"{builder.__name__} leaked {sorted(numeric)}"


def test_row_caps_are_reported_so_a_table_cannot_imply_completeness():
    pages = [{"url": f"https://example.test/{i}"} for i in range(120)]
    panel = bd.search_panel(ok_result("search_console", pages=pages, queries=[]))
    assert panel["page_count"] == 120
    assert len(panel["pages"]) == bd.ROW_CAP


def test_the_injected_block_is_valid_json_and_replaceable(tmp_path, monkeypatch):
    target = tmp_path / "index.html"
    target.write_text("<body>\n<script>\n(() => {\n})();\n</script>\n", encoding="utf-8")
    monkeypatch.setattr(bd, "DASHBOARD", target)

    bd.inject({"generated_at": "2026-08-13 18:00 UTC", "note": "em dash — and \\u"})
    first = target.read_text(encoding="utf-8")
    assert first.count(bd.MARK_OPEN) == 1
    assert "em dash" in first

    # A rebuild replaces the block rather than appending a second one.
    bd.inject({"generated_at": "later"})
    second = target.read_text(encoding="utf-8")
    assert second.count(bd.MARK_OPEN) == 1
    assert "2026-08-13" not in second
