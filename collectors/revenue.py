"""Stage 6 — revenue attribution and ROAS.

Every other agent in this system measures a proxy. This one measures money,
which makes it the one place where a comforting number does real damage. Four
rules govern what it will and will not say:

  1. Unattributed deals stay unattributed. They are reported as their own
     bucket and never spread across channels. Redistributing them inflates
     every channel at once, invisibly, and is the standard way attribution
     dashboards lie.

  2. No spend record means no ROAS. Not infinity, not zero — None, with the
     reason attached. A channel that cost something we did not record has an
     unknown return, and saying so is the whole point of Rule 7.

  3. Gross profit counts on closing only. A projected margin on an open
     contract is a forecast, and forecasts do not belong in a ROAS numerator.

  4. Attribution quality gates the verdict. When too many deals are
     unattributed, the report says the numbers are unreliable *before* it
     reports the numbers, and every channel finding carries the reduced
     confidence.

Nothing in this file is autonomous. Every finding is HUMAN_ONLY: the system
does not move ad budget.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from collectors.base import CollectorResult, collector_guard
from core.config import Settings
from core.models import VERDICT_WITHHELD, Finding, RiskTier, Severity
from revenue import sources
from revenue.funnel import (
    CANONICAL_CHAIN,
    STAGE_ORDER,
    UNATTRIBUTED,
    ChannelPerformance,
    FunnelStage,
    build_channels,
)

logger = logging.getLogger(__name__)

AGENT = "revenue"

# Revenue weight assigned to channel-level findings. Money findings outrank
# page-level SEO findings by construction, which is the Primary Business
# Principle applied literally.
CHANNEL_WEIGHT = 90
DATA_QUALITY_WEIGHT = 70


def _window(settings: Settings, today: date) -> tuple[date, date]:
    end = today
    start = end - timedelta(days=settings.revenue.window_days - 1)
    return start, end


def _confidence(channel: ChannelPerformance) -> float:
    """A finding is only as trustworthy as the attribution under it."""
    return max(0.2, min(0.95, channel.confidence))


def _blended(channels: list[ChannelPerformance]) -> dict[str, Any]:
    """Total return on total spend.

    Deliberately distinct from any per-channel ROAS: the numerator includes
    profit from deals no channel gets credit for. It answers "did the whole
    marketing budget pay for itself", which is a different question from
    "which channel worked".
    """
    spend = sum(c.spend for c in channels if c.spend)
    profit = sum(c.gross_profit for c in channels)
    unattributed_profit = sum(
        c.gross_profit for c in channels if c.channel == UNATTRIBUTED
    )
    return {
        "total_spend": round(spend, 2),
        "total_gross_profit": round(profit, 2),
        "blended_roas": round(profit / spend, 2) if spend > 0 else None,
        "unattributed_gross_profit": round(unattributed_profit, 2),
        "note": (
            "Blended ROAS divides all recorded gross profit by all recorded "
            "spend, including profit from deals no channel is credited with. "
            "It is not the sum of the per-channel ROAS figures and should not "
            "be compared to them."
        ),
    }


def data_quality(
    channels: list[ChannelPerformance], load: sources.LoadResult, settings: Settings
) -> dict[str, Any]:
    total_deals = sum(len(c.deals) for c in channels)
    unattributed = next((c for c in channels if c.channel == UNATTRIBUTED), None)
    unknown_count = len(unattributed.deals) if unattributed else 0
    unknown_share = (unknown_count / total_deals) if total_deals else 0.0

    missing_spend = [
        c.channel
        for c in channels
        if c.roas_status == "no_spend_recorded" and c.deals
    ]
    inferred = sum(
        1 for c in channels for d in c.deals if d.attribution.is_inferred
    )
    return {
        "total_deals": total_deals,
        "unattributed_deals": unknown_count,
        "unknown_share": round(unknown_share, 3),
        "unknown_share_threshold": settings.revenue.max_unknown_share,
        "inferred_attribution_deals": inferred,
        "inferred_share": round(inferred / total_deals, 3) if total_deals else 0.0,
        "channels_missing_spend": missing_spend,
        "rejected_rows": load.rejected_count,
        "reliable": (
            total_deals > 0
            and unknown_share <= settings.revenue.max_unknown_share
            and not missing_spend
        ),
    }


def detect(
    channels: list[ChannelPerformance],
    quality: dict[str, Any],
    load: sources.LoadResult,
    settings: Settings,
) -> list[Finding]:
    """Deterministic revenue analysis. No AI."""
    findings: list[Finding] = []
    target = settings.revenue.target_roas
    currency = settings.revenue.currency

    def add(
        problem: str,
        *,
        entity: str,
        severity: Severity,
        impact: str,
        action: str,
        evidence: dict[str, Any],
        confidence: float = 0.9,
        weight: int = CHANNEL_WEIGHT,
    ) -> None:
        findings.append(
            Finding(
                source_agent=AGENT,
                problem=problem,
                entity=entity,
                severity=severity,
                confidence=confidence,
                revenue_weight=weight,
                conversion_blocking=False,
                business_impact=impact,
                evidence=evidence,
                recommended_action=action,
                # Rule 5: the system does not move money. Ever.
                risk_tier=RiskTier.HUMAN_ONLY,
                verification_plan=(
                    "Re-run against the next CRM export and confirm the trend "
                    "before changing budget."
                ),
            )
        )

    # 1. Attribution quality first. Everything below inherits its reliability,
    #    so reporting a channel verdict above this would be backwards.
    if quality["total_deals"] and quality["unknown_share"] > settings.revenue.max_unknown_share:
        add(
            f"{quality['unknown_share']:.0%} of deals have no recorded source "
            f"({quality['unattributed_deals']} of {quality['total_deals']})",
            entity="attribution",
            severity=Severity.HIGH,
            impact=(
                "Every per-channel ROAS below is computed from the minority of "
                "deals that do have a source. Budget decisions made on these "
                "numbers are being made on incomplete data."
            ),
            action=(
                "Add a required 'how did you hear about us' field at intake, "
                "and record the answer on the deal. This single change is worth "
                "more than any ranking improvement in this report."
            ),
            evidence=quality,
            confidence=0.95,
            weight=DATA_QUALITY_WEIGHT,
        )

    if load.rejected_count:
        add(
            f"{load.rejected_count} row(s) in the revenue export could not be read",
            entity="data_import",
            severity=Severity.MEDIUM,
            impact=(
                "Rejected rows are excluded from every number in this report. "
                "The totals are therefore a floor, not a complete picture."
            ),
            action="Fix the listed rows in the export and re-run.",
            evidence={"rejected": load.rejected[:20], "total": load.rejected_count},
            confidence=1.0,
            weight=DATA_QUALITY_WEIGHT,
        )

    for channel in channels:
        if channel.channel == UNATTRIBUTED:
            continue
        evidence = channel.to_dict()
        closings = channel.counts.get(FunnelStage.CLOSING.value, 0)
        leads = channel.counts.get(FunnelStage.LEAD.value, 0)

        # 2. Spending with nothing to show for it. The most expensive pattern.
        wasted_spend = bool(channel.spend) and channel.spend > 0 and closings == 0
        if wasted_spend:
            contracts = channel.counts.get(FunnelStage.CONTRACT.value, 0)
            add(
                f"{channel.channel} spent {currency} {channel.spend:,.0f} and "
                f"closed nothing",
                entity=channel.channel,
                severity=Severity.HIGH if not contracts else Severity.MEDIUM,
                impact=(
                    f"{leads} lead(s), {contracts} contract(s), zero closings over "
                    f"{settings.revenue.window_days} days. The spend has not "
                    "returned gross profit in this window."
                    + (
                        " Deals may still be in progress — check close dates before cutting."
                        if contracts
                        else ""
                    )
                ),
                action=(
                    "Confirm no closings are simply unrecorded, then review this "
                    "channel's targeting or pause it."
                ),
                evidence=evidence,
                confidence=_confidence(channel),
            )

        # 3. ROAS below the target the business actually manages to.
        #    Skipped when there were no closings at all — "ROAS 0.00x" would
        #    just be a second, weaker way of saying what #2 already said.
        roas = None if wasted_spend else channel.roas
        if roas is not None and len(channel.deals) >= settings.revenue.min_deals_for_roas:
            if roas < target:
                shortfall = (target - roas) / target * 100
                add(
                    f"{channel.channel} ROAS {roas:.2f}x against a {target:.1f}x target",
                    entity=channel.channel,
                    severity=Severity.HIGH if roas < target * 0.5 else Severity.MEDIUM,
                    impact=(
                        f"{currency} {channel.spend:,.0f} spent returned "
                        f"{currency} {channel.gross_profit:,.0f} gross profit — "
                        f"{shortfall:.0f}% below target."
                    ),
                    action=(
                        "Compare cost per contract against the channels above "
                        "target before reallocating."
                    ),
                    evidence=evidence,
                    confidence=_confidence(channel),
                )
            elif roas >= target * 1.5:
                add(
                    f"{channel.channel} ROAS {roas:.2f}x, well above the "
                    f"{target:.1f}x target",
                    entity=channel.channel,
                    severity=Severity.INFO,
                    impact=(
                        f"{currency} {channel.gross_profit:,.0f} gross profit on "
                        f"{currency} {channel.spend:,.0f} spent. This channel has "
                        "room to absorb more budget."
                    ),
                    action="Test a budget increase and re-measure next window.",
                    evidence=evidence,
                    confidence=_confidence(channel),
                )
        elif roas is not None:
            add(
                f"{channel.channel} has {len(channel.deals)} deal(s) — too few "
                f"for a ROAS verdict",
                entity=channel.channel,
                severity=Severity.INFO,
                impact=(
                    f"Measured ROAS is {roas:.2f}x but one closing either way "
                    "would move it substantially. Reported without a verdict."
                ),
                action="Let the sample grow before acting on this number.",
                # Stage 7 routes this to MONITOR however well it scores.
                evidence={**evidence, VERDICT_WITHHELD: True},
                confidence=0.9,
            )

        # 4. Spend that should exist and does not. Blocks ROAS entirely.
        if channel.roas_status == "no_spend_recorded" and channel.deals:
            add(
                f"{channel.channel} produced {len(channel.deals)} deal(s) with no "
                f"spend recorded",
                entity=channel.channel,
                severity=Severity.MEDIUM,
                impact=(
                    "ROAS cannot be computed for this channel. It is not free; "
                    "its cost is simply not in the data."
                ),
                action=(
                    f"Add a spend row for {channel.channel} to spend.csv covering "
                    "this window."
                ),
                evidence=evidence,
                confidence=1.0,
                weight=DATA_QUALITY_WEIGHT,
            )

        # 5. Funnel stage where the drop-off is the problem, not the volume.
        for frm, to in zip(STAGE_ORDER, STAGE_ORDER[1:]):
            rate = channel.rate(frm, to)
            start_count = channel.counts.get(frm.value, 0)
            if rate is None or start_count < 10:
                continue
            if rate < 0.10:
                add(
                    f"{channel.channel}: {rate:.0%} of {frm.value.replace('_', ' ')}s "
                    f"reach {to.value.replace('_', ' ')}",
                    entity=f"{channel.channel}:{frm.value}->{to.value}",
                    severity=Severity.MEDIUM,
                    impact=(
                        f"{start_count} reached {frm.value.replace('_', ' ')} and "
                        f"{channel.counts.get(to.value, 0)} went further. The "
                        "channel is producing volume the process is not converting."
                    ),
                    action=(
                        f"Look at what happens between {frm.value.replace('_', ' ')} "
                        f"and {to.value.replace('_', ' ')} for this channel."
                    ),
                    evidence={**evidence, "stage_from": frm.value, "stage_to": to.value},
                    confidence=_confidence(channel),
                )

    return findings


def _persist(settings: Settings, payload: dict[str, Any], pulled_on: date) -> Path:
    directory = settings.data_dir / "revenue"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pulled_on.isoformat()}.json"
    path.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    return path


@collector_guard(AGENT)
def run(
    settings: Settings,
    *,
    data_dir: Path | None = None,
    today: date | None = None,
    load_result: sources.LoadResult | None = None,
) -> CollectorResult:
    """Build the funnel, compute ROAS where it is computable, and say so where it is not."""
    now = today or date.today()
    directory = data_dir or (settings.data_dir / "revenue" / "input")
    load = load_result if load_result is not None else sources.load(directory)

    if not load.deals and not load.spends:
        return CollectorResult.unavailable(
            AGENT,
            "no revenue data. Export deals.csv (deal_id, channel, attribution, "
            "stage, created_on, gross_profit) and spend.csv (channel, "
            f"period_start, period_end, amount) into {directory}.",
            adapters=sources.adapter_status(),
            notes=load.notes,
            rejected=load.rejected[:20],
        )

    start, end = _window(settings, now)
    channels = build_channels(
        load.deals,
        load.spends,
        (start, end),
        organic_channels=settings.revenue.organic_channels,
    )

    if not channels:
        return CollectorResult.unavailable(
            AGENT,
            f"revenue data exists but none of it falls in {start}..{end}",
            loaded=load.to_dict(),
        )

    quality = data_quality(channels, load, settings)
    findings = detect(channels, quality, load, settings)

    # Attributed channels first by profit, unattributed always last so it
    # reads as the residual it is rather than as the top channel.
    ordered = sorted(
        channels,
        key=lambda c: (c.channel == UNATTRIBUTED, -c.gross_profit, c.channel),
    )

    payload = {
        "window": [start.isoformat(), end.isoformat()],
        "window_days": settings.revenue.window_days,
        "currency": settings.revenue.currency,
        "target_roas": settings.revenue.target_roas,
        "canonical_chain": list(CANONICAL_CHAIN),
        "channels": [c.to_dict() for c in ordered],
        "totals": _blended(channels),
        "funnel_totals": {
            stage.value: sum(c.counts.get(stage.value, 0) for c in channels)
            for stage in STAGE_ORDER
        },
        "data_quality": quality,
        "loaded": load.to_dict(),
        "adapters": sources.adapter_status(),
        "label": (
            "Unattributed deals are reported as their own bucket and are never "
            "distributed across channels. Per-channel ROAS is computed only "
            "where spend was recorded; where it was not, ROAS is null with the "
            "reason attached, not zero."
        ),
    }
    stored = _persist(settings, payload, now)

    return CollectorResult(
        agent=AGENT, findings=findings, data={**payload, "history_file": str(stored)}
    )
