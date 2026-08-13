"""The canonical revenue funnel.

    Channel Spend -> Lead -> Qualified Lead -> Appointment -> Offer
                  -> Contract -> Closing -> Gross Profit

Spend is the input and gross profit is the output; the six stages between
them are the states a single deal actually occupies. That distinction is why
`FunnelStage` covers LEAD..CLOSING only — a deal does not "reach" gross
profit, it produces it. `CANONICAL_CHAIN` records the full chain as the plan
states it, so nothing downstream has to re-derive the order.

Every count here is a count of records someone recorded. Nothing in this file
estimates, models, or infers a number that was not supplied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Iterable

CANONICAL_CHAIN = (
    "channel_spend",
    "lead",
    "qualified_lead",
    "appointment",
    "offer",
    "contract",
    "closing",
    "gross_profit",
)

# The bucket for deals nobody could source. A real channel name, so it shows
# up in every report and can never be quietly omitted.
UNATTRIBUTED = "unattributed"


class FunnelStage(str, Enum):
    """The furthest point a deal has reached. Ordered."""

    LEAD = "lead"
    QUALIFIED_LEAD = "qualified_lead"
    APPOINTMENT = "appointment"
    OFFER = "offer"
    CONTRACT = "contract"
    CLOSING = "closing"

    @property
    def rank(self) -> int:
        return STAGE_ORDER.index(self)

    @classmethod
    def parse(cls, raw: str) -> "FunnelStage | None":
        """Accept the wording a CRM export actually uses.

        Returns None rather than guessing when the value is unrecognized —
        the caller records it as a rejected row (Rule 7).
        """
        key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
        if not key:
            return None
        if key in _STAGE_ALIASES:
            return _STAGE_ALIASES[key]
        try:
            return cls(key)
        except ValueError:
            return None


STAGE_ORDER: tuple[FunnelStage, ...] = (
    FunnelStage.LEAD,
    FunnelStage.QUALIFIED_LEAD,
    FunnelStage.APPOINTMENT,
    FunnelStage.OFFER,
    FunnelStage.CONTRACT,
    FunnelStage.CLOSING,
)

_STAGE_ALIASES: dict[str, FunnelStage] = {
    "new_lead": FunnelStage.LEAD,
    "new": FunnelStage.LEAD,
    "inquiry": FunnelStage.LEAD,
    "qualified": FunnelStage.QUALIFIED_LEAD,
    "sql": FunnelStage.QUALIFIED_LEAD,
    "motivated": FunnelStage.QUALIFIED_LEAD,
    "appointment_set": FunnelStage.APPOINTMENT,
    "appt": FunnelStage.APPOINTMENT,
    "walkthrough": FunnelStage.APPOINTMENT,
    "offer_made": FunnelStage.OFFER,
    "offer_sent": FunnelStage.OFFER,
    "under_contract": FunnelStage.CONTRACT,
    "contract_signed": FunnelStage.CONTRACT,
    "closed": FunnelStage.CLOSING,
    "closed_won": FunnelStage.CLOSING,
    "funded": FunnelStage.CLOSING,
}


class Attribution(str, Enum):
    """How we know which channel a deal came from.

    Ordered by how much weight the number deserves. The distinction is not
    academic: a last-touch guess and a seller who said "I saw your Google ad"
    are different kinds of fact, and a report that renders them identically is
    lying by omission.
    """

    MANUALLY_VERIFIED = "manually_verified"  # a human asked and recorded it
    SOURCE_REPORTED = "source_reported"      # the ad platform reported the conversion
    LAST_TOUCH = "last_touch"                # inferred from the final interaction
    FIRST_TOUCH = "first_touch"              # inferred from the first interaction
    UNKNOWN = "unknown"                      # no attribution data exists

    @property
    def confidence(self) -> float:
        return {
            Attribution.MANUALLY_VERIFIED: 0.95,
            Attribution.SOURCE_REPORTED: 0.85,
            Attribution.LAST_TOUCH: 0.55,
            Attribution.FIRST_TOUCH: 0.50,
            Attribution.UNKNOWN: 0.0,
        }[self]

    @property
    def is_inferred(self) -> bool:
        """True when the channel was deduced rather than established."""
        return self in (Attribution.FIRST_TOUCH, Attribution.LAST_TOUCH)

    @classmethod
    def parse(cls, raw: str) -> "Attribution":
        """Unrecognized or absent attribution becomes UNKNOWN, never a guess."""
        key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
        aliases = {
            "verified": cls.MANUALLY_VERIFIED,
            "manual": cls.MANUALLY_VERIFIED,
            "confirmed": cls.MANUALLY_VERIFIED,
            "asked_seller": cls.MANUALLY_VERIFIED,
            "platform": cls.SOURCE_REPORTED,
            "source": cls.SOURCE_REPORTED,
            "reported": cls.SOURCE_REPORTED,
            "tracked": cls.SOURCE_REPORTED,
            "last": cls.LAST_TOUCH,
            "lasttouch": cls.LAST_TOUCH,
            "first": cls.FIRST_TOUCH,
            "firsttouch": cls.FIRST_TOUCH,
            "": cls.UNKNOWN,
        }
        if key in aliases:
            return aliases[key]
        try:
            return cls(key)
        except ValueError:
            return cls.UNKNOWN


@dataclass
class Deal:
    """One opportunity, wherever it got to."""

    deal_id: str
    channel: str
    attribution: Attribution
    stage: FunnelStage
    created_on: date
    closed_on: date | None = None
    gross_profit: float = 0.0
    landing_page: str = ""
    source_detail: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        # An unknown source belongs in the unknown bucket regardless of what
        # the channel column said, and vice versa. Keeping the two fields
        # consistent here means no downstream consumer has to check both.
        if self.attribution is Attribution.UNKNOWN or not self.channel.strip():
            self.channel = UNATTRIBUTED
            self.attribution = Attribution.UNKNOWN
        else:
            self.channel = self.channel.strip().lower().replace(" ", "_")

    @property
    def is_attributed(self) -> bool:
        return self.channel != UNATTRIBUTED

    def reached(self, stage: FunnelStage) -> bool:
        return self.stage.rank >= stage.rank

    @property
    def realized_profit(self) -> float:
        """Gross profit counts only once the deal actually closed.

        A projected profit on an open contract is a forecast, and forecasts do
        not belong in a ROAS numerator.
        """
        return self.gross_profit if self.stage is FunnelStage.CLOSING else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "deal_id": self.deal_id,
            "channel": self.channel,
            "attribution": self.attribution.value,
            "stage": self.stage.value,
            "created_on": self.created_on.isoformat(),
            "closed_on": self.closed_on.isoformat() if self.closed_on else None,
            "gross_profit": round(self.gross_profit, 2),
            "realized_profit": round(self.realized_profit, 2),
            "landing_page": self.landing_page,
            "source_detail": self.source_detail,
        }


@dataclass
class Spend:
    """What a channel cost over a period."""

    channel: str
    period_start: date
    period_end: date
    amount: float

    def __post_init__(self) -> None:
        self.channel = self.channel.strip().lower().replace(" ", "_")

    def overlaps(self, start: date, end: date) -> bool:
        return self.period_start <= end and self.period_end >= start

    def portion_within(self, start: date, end: date) -> float:
        """Spend attributable to a window, prorated by overlapping days.

        Proration is stated rather than hidden: a month of spend compared
        against a 28-day window would otherwise overstate cost by ~10%.
        """
        if not self.overlaps(start, end):
            return 0.0
        total_days = (self.period_end - self.period_start).days + 1
        if total_days <= 0:
            return 0.0
        overlap_start = max(self.period_start, start)
        overlap_end = min(self.period_end, end)
        overlap_days = (overlap_end - overlap_start).days + 1
        return self.amount * (overlap_days / total_days)

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "amount": round(self.amount, 2),
        }


@dataclass
class ChannelPerformance:
    """One channel's funnel over one window, with ROAS where it is computable."""

    channel: str
    deals: list[Deal] = field(default_factory=list)
    spend: float | None = None          # None means no spend record exists
    spend_is_expected: bool = True      # False for organic channels
    counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = {
                stage.value: sum(1 for d in self.deals if d.reached(stage))
                for stage in STAGE_ORDER
            }

    # -- money ------------------------------------------------------------

    @property
    def gross_profit(self) -> float:
        return sum(d.realized_profit for d in self.deals)

    @property
    def roas(self) -> float | None:
        """Gross profit per dollar spent, or None when it is not computable.

        None is a first-class answer here. A channel with no recorded spend
        does not have an infinite ROAS; it has an unknown one.
        """
        if self.spend is None or self.spend <= 0:
            return None
        return self.gross_profit / self.spend

    @property
    def roas_status(self) -> str:
        if self.spend is None:
            return "no_spend_recorded" if self.spend_is_expected else "organic_no_spend"
        if self.spend <= 0:
            return "zero_spend"
        return "computed"

    def cost_per(self, stage: FunnelStage) -> float | None:
        if self.spend is None or self.spend <= 0:
            return None
        count = self.counts.get(stage.value, 0)
        return (self.spend / count) if count else None

    # -- funnel shape -----------------------------------------------------

    def rate(self, frm: FunnelStage, to: FunnelStage) -> float | None:
        start = self.counts.get(frm.value, 0)
        if not start:
            return None
        return self.counts.get(to.value, 0) / start

    # -- attribution quality ---------------------------------------------

    @property
    def attribution_mix(self) -> dict[str, int]:
        mix: dict[str, int] = {}
        for deal in self.deals:
            mix[deal.attribution.value] = mix.get(deal.attribution.value, 0) + 1
        return mix

    @property
    def inferred_share(self) -> float:
        if not self.deals:
            return 0.0
        inferred = sum(1 for d in self.deals if d.attribution.is_inferred)
        return inferred / len(self.deals)

    @property
    def confidence(self) -> float:
        """Mean attribution confidence — how much this channel's number is worth."""
        if not self.deals:
            return 0.0
        return sum(d.attribution.confidence for d in self.deals) / len(self.deals)

    def to_dict(self) -> dict[str, Any]:
        roas = self.roas
        return {
            "channel": self.channel,
            "deal_count": len(self.deals),
            "counts": dict(self.counts),
            "spend": None if self.spend is None else round(self.spend, 2),
            "gross_profit": round(self.gross_profit, 2),
            "roas": None if roas is None else round(roas, 2),
            "roas_status": self.roas_status,
            "cost_per_lead": _round_opt(self.cost_per(FunnelStage.LEAD)),
            "cost_per_qualified_lead": _round_opt(
                self.cost_per(FunnelStage.QUALIFIED_LEAD)
            ),
            "cost_per_contract": _round_opt(self.cost_per(FunnelStage.CONTRACT)),
            "cost_per_closing": _round_opt(self.cost_per(FunnelStage.CLOSING)),
            "lead_to_qualified": _round_opt(
                self.rate(FunnelStage.LEAD, FunnelStage.QUALIFIED_LEAD), 3
            ),
            "qualified_to_appointment": _round_opt(
                self.rate(FunnelStage.QUALIFIED_LEAD, FunnelStage.APPOINTMENT), 3
            ),
            "appointment_to_contract": _round_opt(
                self.rate(FunnelStage.APPOINTMENT, FunnelStage.CONTRACT), 3
            ),
            "contract_to_closing": _round_opt(
                self.rate(FunnelStage.CONTRACT, FunnelStage.CLOSING), 3
            ),
            "attribution_mix": self.attribution_mix,
            "inferred_share": round(self.inferred_share, 3),
            "attribution_confidence": round(self.confidence, 3),
        }


def _round_opt(value: float | None, places: int = 2) -> float | None:
    return None if value is None else round(value, places)


def build_channels(
    deals: Iterable[Deal],
    spends: Iterable[Spend],
    window: tuple[date, date],
    *,
    organic_channels: tuple[str, ...] = (),
) -> list[ChannelPerformance]:
    """Group deals and prorated spend into per-channel performance.

    Channels appear if they have deals OR spend. A channel that spent money
    and produced nothing is the single most important row in the report, and
    grouping only by deals would delete it.
    """
    start, end = window
    organic = {c.strip().lower() for c in organic_channels}

    by_channel: dict[str, list[Deal]] = {}
    for deal in deals:
        if not (start <= deal.created_on <= end):
            continue
        by_channel.setdefault(deal.channel, []).append(deal)

    spend_by_channel: dict[str, float] = {}
    for spend in spends:
        portion = spend.portion_within(start, end)
        if portion > 0 or spend.channel in by_channel:
            spend_by_channel[spend.channel] = (
                spend_by_channel.get(spend.channel, 0.0) + portion
            )

    out: list[ChannelPerformance] = []
    for channel in sorted(set(by_channel) | set(spend_by_channel)):
        out.append(
            ChannelPerformance(
                channel=channel,
                deals=by_channel.get(channel, []),
                spend=spend_by_channel.get(channel),
                # The unattributed bucket is not a channel anyone buys, so a
                # missing spend record for it is not a data-quality defect.
                spend_is_expected=(
                    channel not in organic and channel != UNATTRIBUTED
                ),
            )
        )
    return out
