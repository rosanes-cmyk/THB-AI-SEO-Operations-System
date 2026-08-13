"""Stage 6 — revenue attribution and ROAS.

The tests that matter most here are the ones asserting what the system
refuses to say: no redistributed unknowns, no ROAS without spend, no profit
counted before a closing.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from collectors import revenue as rev
from core.config import RevenueConfig
from core.models import CollectorStatus, Severity
from revenue import sources
from revenue.funnel import (
    UNATTRIBUTED,
    Attribution,
    ChannelPerformance,
    Deal,
    FunnelStage,
    Spend,
    build_channels,
)

TODAY = date(2026, 3, 30)
IN_WINDOW = date(2026, 2, 1)


def deal(
    deal_id: str,
    channel: str = "google_ads",
    stage: FunnelStage = FunnelStage.CLOSING,
    profit: float = 10000.0,
    attribution: Attribution = Attribution.SOURCE_REPORTED,
    created: date = IN_WINDOW,
) -> Deal:
    return Deal(
        deal_id=deal_id,
        channel=channel,
        attribution=attribution,
        stage=stage,
        created_on=created,
        gross_profit=profit,
    )


def spend(channel: str, amount: float) -> Spend:
    return Spend(channel, date(2026, 1, 1), date(2026, 3, 30), amount)


def run(settings, deals=(), spends=(), **kwargs):
    load = sources.LoadResult(deals=list(deals), spends=list(spends))
    return rev.run(settings, today=TODAY, load_result=load, **kwargs)


# ==========================================================================
# The four honesty rules
# ==========================================================================


def test_unattributed_deals_are_never_spread_across_channels(settings):
    """The single most important assertion in this file."""
    result = run(
        settings,
        deals=[
            deal("a", "google_ads", profit=10000),
            *[
                deal(f"u{i}", "", attribution=Attribution.UNKNOWN, profit=20000)
                for i in range(4)
            ],
        ],
        spends=[spend("google_ads", 5000)],
    )
    by_channel = {c["channel"]: c for c in result.data["channels"]}

    # Google Ads gets credit for its own $10k and not a cent of the $80k.
    assert by_channel["google_ads"]["gross_profit"] == 10000.0
    assert by_channel["google_ads"]["roas"] == 2.0

    assert by_channel[UNATTRIBUTED]["gross_profit"] == 80000.0
    assert result.data["totals"]["unattributed_gross_profit"] == 80000.0
    # And the bucket is not dressed up as an organic channel.
    assert by_channel[UNATTRIBUTED]["roas_status"] == "not_a_channel"


def test_unattributed_bucket_sorts_last_not_first(settings):
    result = run(
        settings,
        deals=[
            deal("a", "google_ads", profit=1000),
            deal("u", "", attribution=Attribution.UNKNOWN, profit=999_999),
        ],
    )
    assert result.data["channels"][-1]["channel"] == UNATTRIBUTED


def test_no_spend_record_means_null_roas_not_zero_and_not_infinity(settings):
    result = run(settings, deals=[deal("a", "yelp", profit=25000)])
    yelp = next(c for c in result.data["channels"] if c["channel"] == "yelp")
    assert yelp["roas"] is None
    assert yelp["roas_status"] == "no_spend_recorded"
    assert yelp["gross_profit"] == 25000.0
    assert yelp["cost_per_closing"] is None


def test_missing_spend_on_a_paid_channel_is_raised_as_a_defect(settings):
    result = run(settings, deals=[deal("a", "yelp")])
    hits = [f for f in result.findings if "no spend recorded" in f.problem]
    assert len(hits) == 1
    assert "not free" in hits[0].business_impact


def test_organic_channels_are_not_faulted_for_having_no_spend(settings):
    result = run(settings, deals=[deal("a", "seo", profit=30000)])
    seo = next(c for c in result.data["channels"] if c["channel"] == "seo")
    assert seo["roas_status"] == "organic_no_spend"
    assert [f for f in result.findings if "no spend recorded" in f.problem] == []


def test_profit_is_counted_only_once_a_deal_closes(settings):
    """An open contract's projected margin is a forecast, not a return."""
    result = run(
        settings,
        deals=[
            deal("open", "google_ads", stage=FunnelStage.CONTRACT, profit=50000),
            deal("done", "google_ads", stage=FunnelStage.CLOSING, profit=10000),
        ],
        spends=[spend("google_ads", 10000)],
    )
    google = next(c for c in result.data["channels"] if c["channel"] == "google_ads")
    assert google["gross_profit"] == 10000.0
    assert google["roas"] == 1.0
    assert google["counts"]["contract"] == 2  # both reached contract
    assert google["counts"]["closing"] == 1


def test_high_unknown_share_is_reported_before_any_channel_verdict(settings):
    deals = [deal("a", "google_ads")] + [
        deal(f"u{i}", "", attribution=Attribution.UNKNOWN) for i in range(9)
    ]
    result = run(settings, deals=deals, spends=[spend("google_ads", 1000)])
    quality = [f for f in result.findings if f.entity == "attribution"]
    assert len(quality) == 1
    assert "90%" in quality[0].problem
    assert quality[0].severity is Severity.HIGH
    assert result.data["data_quality"]["reliable"] is False
    # It names the cheapest real fix rather than a technical one.
    assert "how did you hear about us" in quality[0].recommended_action


def test_attribution_quality_scales_finding_confidence(settings):
    """A last-touch guess must not produce the same confidence as a verified source."""
    verified = run(
        settings,
        deals=[
            deal(f"v{i}", "google_ads", attribution=Attribution.MANUALLY_VERIFIED,
                 profit=1000)
            for i in range(6)
        ],
        spends=[spend("google_ads", 20000)],
    )
    guessed = run(
        settings,
        deals=[
            deal(f"g{i}", "google_ads", attribution=Attribution.LAST_TOUCH, profit=1000)
            for i in range(6)
        ],
        spends=[spend("google_ads", 20000)],
    )
    v = next(f for f in verified.findings if "ROAS" in f.problem)
    g = next(f for f in guessed.findings if "ROAS" in f.problem)
    assert v.confidence > g.confidence
    assert v.priority_score() > g.priority_score()


# ==========================================================================
# ROAS verdicts
# ==========================================================================


def test_roas_below_target_is_flagged_against_the_configured_target(settings):
    settings = _with_target(settings, 4.0)
    result = run(
        settings,
        deals=[deal(f"d{i}", "google_ads", profit=5000) for i in range(6)],
        spends=[spend("google_ads", 20000)],
    )
    hits = [f for f in result.findings if "ROAS" in f.problem and "target" in f.problem]
    assert len(hits) == 1
    assert "1.50x" in hits[0].problem
    assert "4.0x target" in hits[0].problem
    assert hits[0].severity is Severity.HIGH  # under half of target


def test_roas_above_target_becomes_a_growth_opportunity_not_an_alarm(settings):
    result = run(
        settings,
        deals=[deal(f"d{i}", "google_ads", profit=20000) for i in range(6)],
        spends=[spend("google_ads", 20000)],
    )
    hits = [f for f in result.findings if "well above" in f.problem]
    assert len(hits) == 1
    assert hits[0].severity is Severity.INFO
    assert "more budget" in hits[0].business_impact


def test_too_few_deals_gets_no_verdict(settings):
    result = run(
        settings,
        deals=[deal("only", "google_ads", profit=100)],
        spends=[spend("google_ads", 50000)],
    )
    verdicts = [f for f in result.findings if "against a" in f.problem]
    assert verdicts == []
    hedged = [f for f in result.findings if "too few" in f.problem]
    assert len(hedged) == 1
    assert hedged[0].severity is Severity.INFO


def test_spend_with_zero_closings_is_the_loudest_channel_finding(settings):
    result = run(
        settings,
        deals=[deal(f"d{i}", "yelp", stage=FunnelStage.LEAD, profit=0) for i in range(8)],
        spends=[spend("yelp", 12000)],
    )
    hits = [f for f in result.findings if "closed nothing" in f.problem]
    assert len(hits) == 1
    assert hits[0].severity is Severity.HIGH
    assert "12,000" in hits[0].problem
    # It still tells you to check the data before cutting the channel.
    assert "unrecorded" in hits[0].recommended_action


def test_open_contracts_soften_the_zero_closings_verdict(settings):
    result = run(
        settings,
        deals=[
            deal(f"d{i}", "yelp", stage=FunnelStage.CONTRACT, profit=0) for i in range(8)
        ],
        spends=[spend("yelp", 12000)],
    )
    hit = next(f for f in result.findings if "closed nothing" in f.problem)
    assert hit.severity is Severity.MEDIUM
    assert "still be in progress" in hit.business_impact


def test_blended_roas_is_labelled_as_distinct_from_channel_roas(settings):
    result = run(
        settings,
        deals=[
            deal("a", "google_ads", profit=10000),
            deal("u", "", attribution=Attribution.UNKNOWN, profit=50000),
        ],
        spends=[spend("google_ads", 10000)],
    )
    totals = result.data["totals"]
    assert totals["blended_roas"] == 6.0  # 60k profit / 10k spend
    google = next(c for c in result.data["channels"] if c["channel"] == "google_ads")
    assert google["roas"] == 1.0
    assert "not the sum of the per-channel" in totals["note"]


def test_a_channel_that_only_spent_still_appears(settings):
    """Grouping by deals alone would delete the worst-performing channel."""
    result = run(settings, deals=[deal("a", "seo")], spends=[spend("direct_mail", 9000)])
    channels = {c["channel"] for c in result.data["channels"]}
    assert "direct_mail" in channels


# ==========================================================================
# Funnel mechanics
# ==========================================================================


def test_stage_counts_are_cumulative_up_the_funnel():
    perf = ChannelPerformance(
        channel="x",
        deals=[
            deal("a", stage=FunnelStage.LEAD),
            deal("b", stage=FunnelStage.APPOINTMENT),
            deal("c", stage=FunnelStage.CLOSING),
        ],
    )
    assert perf.counts["lead"] == 3
    assert perf.counts["qualified_lead"] == 2
    assert perf.counts["appointment"] == 2
    assert perf.counts["contract"] == 1
    assert perf.counts["closing"] == 1


def test_funnel_collapse_is_flagged_with_enough_volume(settings):
    deals = [deal(f"l{i}", "google_ads", stage=FunnelStage.LEAD, profit=0) for i in range(30)]
    deals += [deal("q", "google_ads", stage=FunnelStage.QUALIFIED_LEAD, profit=0)]
    result = run(settings, deals=deals, spends=[spend("google_ads", 1000)])
    hits = [f for f in result.findings if "reach qualified lead" in f.problem]
    assert len(hits) == 1
    assert "3%" in hits[0].problem


def test_funnel_collapse_is_silent_on_tiny_volume(settings):
    deals = [deal(f"l{i}", "google_ads", stage=FunnelStage.LEAD, profit=0) for i in range(4)]
    result = run(settings, deals=deals, spends=[spend("google_ads", 1000)])
    assert [f for f in result.findings if "reach qualified" in f.problem] == []


def test_spend_is_prorated_across_a_partial_window():
    s = Spend("google_ads", date(2026, 3, 1), date(2026, 3, 31), 3100.0)
    assert s.portion_within(date(2026, 3, 1), date(2026, 3, 31)) == pytest.approx(3100.0)
    assert s.portion_within(date(2026, 3, 1), date(2026, 3, 10)) == pytest.approx(1000.0)
    assert s.portion_within(date(2026, 1, 1), date(2026, 1, 31)) == 0.0


def test_deals_outside_the_window_are_excluded():
    channels = build_channels(
        [deal("old", "google_ads", created=date(2020, 1, 1)), deal("new", "google_ads")],
        [],
        (date(2026, 1, 1), TODAY),
    )
    assert len(channels[0].deals) == 1
    assert channels[0].deals[0].deal_id == "new"


def test_a_deal_with_a_channel_but_unknown_attribution_goes_to_the_unknown_bucket():
    d = Deal("x", "google_ads", Attribution.UNKNOWN, FunnelStage.LEAD, IN_WINDOW)
    assert d.channel == UNATTRIBUTED
    assert not d.is_attributed


def test_a_deal_with_no_channel_is_unattributed_whatever_the_method_claims():
    d = Deal("x", "  ", Attribution.SOURCE_REPORTED, FunnelStage.LEAD, IN_WINDOW)
    assert d.channel == UNATTRIBUTED
    assert d.attribution is Attribution.UNKNOWN


# ==========================================================================
# Autonomy
# ==========================================================================


def test_no_revenue_finding_is_ever_automatable(settings):
    """Rule 5: the system does not move ad budget."""
    result = run(
        settings,
        deals=[deal(f"d{i}", "google_ads", profit=100) for i in range(8)],
        spends=[spend("google_ads", 50000)],
    )
    assert result.findings
    for finding in result.findings:
        assert finding.risk_tier.value == "human_only"


# ==========================================================================
# CSV bridge
# ==========================================================================


def write_csv(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def test_csv_accepts_the_column_names_a_crm_actually_exports(tmp_path):
    path = write_csv(
        tmp_path / "deals.csv",
        """
Lead ID,Lead Source,Attribution Method,Deal Stage,Created Date,Close Date,Gross Profit
100,Google Ads,platform,Closed Won,03/01/2026,03/20/2026,"$28,500.00"
101,Yelp,last touch,Under Contract,03/05/2026,,
102,SEO,verified,appointment set,2026-03-07,,
""",
    )
    result = sources.load_deals_csv(path)
    assert result.rejected == []
    assert len(result.deals) == 3
    first = result.deals[0]
    assert first.channel == "google_ads"
    assert first.attribution is Attribution.SOURCE_REPORTED
    assert first.stage is FunnelStage.CLOSING
    assert first.gross_profit == 28500.0
    assert result.deals[1].stage is FunnelStage.CONTRACT
    assert result.deals[2].attribution is Attribution.MANUALLY_VERIFIED


def test_csv_reports_bad_rows_instead_of_dropping_them(tmp_path):
    path = write_csv(
        tmp_path / "deals.csv",
        """
deal_id,channel,attribution,stage,created_on,gross_profit
1,google_ads,platform,closed_won,2026-03-01,10000
2,google_ads,platform,banana,2026-03-01,10000
3,google_ads,platform,lead,not-a-date,0
1,google_ads,platform,lead,2026-03-01,0
""",
    )
    result = sources.load_deals_csv(path)
    assert len(result.deals) == 1
    reasons = " ".join(r["reason"] for r in result.rejected)
    assert "unrecognized stage 'banana'" in reasons
    assert "unparseable created date" in reasons
    assert "duplicate deal_id" in reasons


def test_a_closed_deal_with_no_profit_is_rejected_not_counted_as_zero(tmp_path):
    """Counting it as $0 would silently drag every ROAS down."""
    path = write_csv(
        tmp_path / "deals.csv",
        """
deal_id,channel,attribution,stage,created_on,gross_profit
1,google_ads,platform,closed_won,2026-03-01,
""",
    )
    result = sources.load_deals_csv(path)
    assert result.deals == []
    assert "no gross profit recorded" in result.rejected[0]["reason"]


def test_unrecognized_attribution_becomes_unknown_never_a_guess(tmp_path):
    path = write_csv(
        tmp_path / "deals.csv",
        """
deal_id,channel,attribution,stage,created_on
1,google_ads,vibes,lead,2026-03-01
""",
    )
    result = sources.load_deals_csv(path)
    assert result.deals[0].attribution is Attribution.UNKNOWN
    assert result.deals[0].channel == UNATTRIBUTED


def test_spend_csv_parses_currency_formatting(tmp_path):
    path = write_csv(
        tmp_path / "spend.csv",
        """
channel,period_start,period_end,amount
Google Ads,2026-03-01,2026-03-31,"$8,400.00"
LSA,03/01/2026,03/31/2026,2150
Broken,2026-03-01,2026-02-01,100
""",
    )
    result = sources.load_spend_csv(path)
    assert len(result.spends) == 2
    assert result.spends[0].channel == "google_ads"
    assert result.spends[0].amount == 8400.0
    assert "ends before it starts" in result.rejected[0]["reason"]


def test_directory_loader_says_when_it_skipped_a_file(tmp_path):
    write_csv(tmp_path / "mystery.csv", "a,b\n1,2")
    result = sources.load_csv_directory(tmp_path)
    assert any("mystery.csv" in n for n in result.notes)


def test_rejected_rows_become_a_finding(settings, tmp_path):
    write_csv(
        tmp_path / "deals.csv",
        """
deal_id,channel,attribution,stage,created_on,gross_profit
1,google_ads,platform,closed_won,2026-02-01,10000
2,google_ads,platform,banana,2026-02-01,10000
""",
    )
    result = rev.run(settings, data_dir=tmp_path, today=TODAY)
    hits = [f for f in result.findings if "could not be read" in f.problem]
    assert len(hits) == 1
    assert "floor, not a complete picture" in hits[0].business_impact


# ==========================================================================
# Unavailability and isolation
# ==========================================================================


def test_no_data_reports_unavailable_and_names_the_files_to_export(settings, tmp_path):
    result = rev.run(settings, data_dir=tmp_path / "nothing", today=TODAY)
    assert result.status is CollectorStatus.UNAVAILABLE
    assert "deals.csv" in result.reason
    assert "spend.csv" in result.reason
    assert "channels" not in result.data


def test_unavailable_result_still_reports_what_each_adapter_needs(settings, tmp_path):
    result = rev.run(settings, data_dir=tmp_path / "nothing", today=TODAY)
    adapters = {a["channel"]: a for a in result.data["adapters"]}
    assert set(adapters) >= {"google_ads", "lsa", "callrail", "crm", "rei_blackbook"}
    assert all(a["state"] == "not_implemented" for a in adapters.values())
    assert adapters["callrail"]["requires"]


def test_data_outside_the_window_reports_unavailable_not_zeroes(settings):
    result = run(settings, deals=[deal("old", "google_ads", created=date(2019, 1, 1))])
    assert result.status is CollectorStatus.UNAVAILABLE
    assert "none of it falls in" in result.reason


def test_history_is_written_dated(settings):
    run(settings, deals=[deal("a", "seo", profit=1000)])
    path = settings.data_dir / "revenue" / "2026-03-30.json"
    assert path.exists()
    stored = json.loads(path.read_text())
    assert stored["canonical_chain"][0] == "channel_spend"
    assert stored["canonical_chain"][-1] == "gross_profit"


def test_a_crash_in_loading_is_contained(settings, monkeypatch):
    monkeypatch.setattr(
        sources, "load", lambda _d: (_ for _ in ()).throw(RuntimeError("disk gone"))
    )
    result = rev.run(settings, today=TODAY)
    assert result.status is CollectorStatus.ERROR
    assert "disk gone" in result.error


# -- helpers ---------------------------------------------------------------


def _with_target(settings, target: float):
    import dataclasses

    return dataclasses.replace(
        settings, revenue=RevenueConfig(target_roas=target)
    )
