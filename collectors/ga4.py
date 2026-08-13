"""GA4 conversion intelligence — Stage 5.

Stops measuring SEO as traffic alone. Search Console says how many people
arrived; this says what they did next.

The honesty constraint that shapes this file: **a GA4 conversion is not a
qualified seller lead.** GA4 records that a form was submitted or a call
button was tapped. Whether that person owns a house, wants to sell it, and
eventually signs a contract is unknown until the CRM says so in Stage 6.
Every label here says "conversion", never "lead" or "contract", and the
`label` field on the output states the caveat explicitly so a downstream
report cannot quietly promote a button click into a signed deal.

The output is the joined landing-page dataset Stage 6 and Stage 7 consume:
one row per page carrying both search and behavioral metrics, keyed on the
same normalized URL both sources agree on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

from collectors.base import CollectorResult, collector_guard
from core.config import Settings
from core.models import Finding, RiskTier, Severity
from core.urls import normalize_url

logger = logging.getLogger(__name__)

AGENT = "ga4"

SCOPES = ("https://www.googleapis.com/auth/analytics.readonly",)

# Volume floors. A page going 1 conversion -> 0 is not a 100% collapse.
MIN_SESSIONS_FOR_RATE = 100
MIN_SESSIONS_FOR_ZERO_CONV = 200
MIN_CONVERSIONS_FOR_DECLINE = 5

# A revenue page converting below this is worth looking at.
WEAK_CONVERSION_RATE = 0.01

# What GA4 calls the conversion. Configurable because every property differs.
DEFAULT_KEY_EVENTS = ("generate_lead", "form_submit", "contact", "phone_call")


@dataclass
class PageMetrics:
    """Behavioral metrics for one landing page in one window."""

    sessions: int = 0
    users: int = 0
    conversions: int = 0
    engaged_sessions: int = 0

    @property
    def conversion_rate(self) -> float:
        return (self.conversions / self.sessions) if self.sessions else 0.0

    @property
    def engagement_rate(self) -> float:
        return (self.engaged_sessions / self.sessions) if self.sessions else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions": self.sessions,
            "users": self.users,
            "conversions": self.conversions,
            "conversion_rate": round(self.conversion_rate, 4),
            "engagement_rate": round(self.engagement_rate, 4),
        }


@dataclass
class LandingPage:
    """One landing page, current vs prior, with deltas precomputed."""

    url: str
    current: PageMetrics = field(default_factory=PageMetrics)
    prior: PageMetrics = field(default_factory=PageMetrics)
    revenue_weight: int = 0
    money_page: bool = False
    name: str = ""
    by_device: dict[str, PageMetrics] = field(default_factory=dict)

    @property
    def session_delta(self) -> int:
        return self.current.sessions - self.prior.sessions

    @property
    def conversion_delta(self) -> int:
        return self.current.conversions - self.prior.conversions

    @property
    def rate_delta(self) -> float:
        return self.current.conversion_rate - self.prior.conversion_rate

    @property
    def session_change_pct(self) -> float:
        return (self.session_delta / self.prior.sessions * 100) if self.prior.sessions else 0.0

    @property
    def mobile_weakness(self) -> float:
        """How much worse mobile converts than desktop, in rate points.

        Positive means mobile is losing. Returns 0.0 when either device lacks
        the volume to compare honestly.
        """
        mobile = self.by_device.get("mobile")
        desktop = self.by_device.get("desktop")
        if not mobile or not desktop:
            return 0.0
        if mobile.sessions < MIN_SESSIONS_FOR_RATE or desktop.sessions < MIN_SESSIONS_FOR_RATE:
            return 0.0
        return desktop.conversion_rate - mobile.conversion_rate

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "name": self.name,
            "revenue_weight": self.revenue_weight,
            "money_page": self.money_page,
            "current": self.current.to_dict(),
            "prior": self.prior.to_dict(),
            "session_delta": self.session_delta,
            "session_change_pct": round(self.session_change_pct, 1),
            "conversion_delta": self.conversion_delta,
            "rate_delta": round(self.rate_delta, 4),
            "mobile_weakness": round(self.mobile_weakness, 4),
            "by_device": {k: v.to_dict() for k, v in self.by_device.items()},
        }


# A runner takes (property_id, request) and returns GA4 rows.
Runner = Callable[[str, dict[str, Any]], list[dict[str, Any]]]


def _build_runner(settings: Settings) -> tuple[Runner | None, str]:
    creds_path = settings.secrets.google_credentials_path
    property_id = settings.secrets.ga4_property_id

    if not property_id:
        return None, "THB_GA4_PROPERTY_ID is not set"
    if not creds_path:
        return None, "GOOGLE_APPLICATION_CREDENTIALS is not set"

    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient  # noqa: PLC0415
        from google.analytics.data_v1beta.types import (  # noqa: PLC0415
            DateRange,
            Dimension,
            Metric,
            RunReportRequest,
        )
        from google.oauth2 import service_account  # noqa: PLC0415
    except ImportError:
        return None, (
            "google-analytics-data is not installed; run "
            "`pip install google-analytics-data`"
        )

    try:
        credentials = service_account.Credentials.from_service_account_file(
            creds_path, scopes=list(SCOPES)
        )
        client = BetaAnalyticsDataClient(credentials=credentials)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not authenticate: {type(exc).__name__}: {exc}"

    def run_report(prop: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
        request = RunReportRequest(
            property=f"properties/{prop}",
            date_ranges=[DateRange(start_date=spec["start"], end_date=spec["end"])],
            dimensions=[Dimension(name=d) for d in spec["dimensions"]],
            metrics=[Metric(name=m) for m in spec["metrics"]],
            limit=spec.get("limit", 5000),
        )
        response = client.run_report(request)
        rows: list[dict[str, Any]] = []
        for row in response.rows:
            record = {
                d.name: v.value
                for d, v in zip(response.dimension_headers, row.dimension_values)
            }
            for m, v in zip(response.metric_headers, row.metric_values):
                try:
                    record[m.name] = float(v.value)
                except (TypeError, ValueError):
                    record[m.name] = 0.0
            rows.append(record)
        return rows

    return run_report, ""


def _windows(today: date, days: int) -> tuple[dict[str, str], dict[str, str]]:
    end = today - timedelta(days=1)  # GA4 "yesterday" is the last complete day
    start = end - timedelta(days=days - 1)
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=days - 1)
    return (
        {"start": start.isoformat(), "end": end.isoformat()},
        {"start": prior_start.isoformat(), "end": prior_end.isoformat()},
    )


def _metrics_from(row: dict[str, Any]) -> PageMetrics:
    return PageMetrics(
        sessions=int(row.get("sessions", 0)),
        users=int(row.get("totalUsers", 0)),
        conversions=int(row.get("keyEvents", row.get("conversions", 0))),
        engaged_sessions=int(row.get("engagedSessions", 0)),
    )


def _collect(runner: Runner, prop: str, window: dict[str, str], with_device: bool):
    dims = ["landingPagePlusQueryString"]
    if with_device:
        dims.append("deviceCategory")
    rows = runner(
        prop,
        {
            **window,
            "dimensions": dims,
            "metrics": ["sessions", "totalUsers", "keyEvents", "engagedSessions"],
        },
    )
    out: dict[str, Any] = {}
    for row in rows:
        url = normalize_url(row.get("landingPagePlusQueryString", ""))
        if not url:
            continue
        if with_device:
            device = str(row.get("deviceCategory", "unknown")).lower()
            out.setdefault(url, {})[device] = _metrics_from(row)
        else:
            existing = out.get(url)
            metrics = _metrics_from(row)
            if existing:
                existing.sessions += metrics.sessions
                existing.users += metrics.users
                existing.conversions += metrics.conversions
                existing.engaged_sessions += metrics.engaged_sessions
            else:
                out[url] = metrics
    return out


def detect(pages: list[LandingPage], window_days: int) -> list[Finding]:
    """Deterministic conversion analysis. No AI."""
    findings: list[Finding] = []

    def add(p: LandingPage, problem: str, *, severity: Severity, impact: str, action: str) -> None:
        findings.append(
            Finding(
                source_agent=AGENT,
                problem=problem,
                url=p.url,
                entity=p.name or p.url,
                severity=severity,
                confidence=0.9,
                revenue_weight=p.revenue_weight,
                conversion_blocking=False,
                business_impact=impact,
                evidence={"window_days": window_days, **p.to_dict()},
                recommended_action=action,
                risk_tier=RiskTier.HUMAN_ONLY,
                verification_plan="Re-pull GA4 for the next window and confirm the trend.",
            )
        )

    for p in pages:
        if p.revenue_weight == 0 and not p.money_page:
            continue

        # 1. The most valuable signal in the system: traffic holding, conversions
        #    falling. That pattern is almost always a broken or degraded form.
        if (
            p.prior.conversions >= MIN_CONVERSIONS_FOR_DECLINE
            and p.conversion_delta < 0
            and abs(p.session_change_pct) < 15
        ):
            drop = abs(p.conversion_delta / p.prior.conversions * 100)
            if drop >= 25:
                add(
                    p,
                    f"Conversions down {drop:.0f}% while traffic held steady "
                    f"({p.prior.conversions} to {p.current.conversions})",
                    severity=Severity.HIGH if p.money_page else Severity.MEDIUM,
                    impact=(
                        "Same number of visitors, fewer converting. This pattern "
                        "usually means the form or CTA degraded, not that demand fell."
                    ),
                    action=(
                        "Check the form end to end on this page, on mobile first. "
                        "Cross-reference the vision agent's screenshots for this window."
                    ),
                )

        # 2. Real traffic, zero conversions.
        if (
            p.current.sessions >= MIN_SESSIONS_FOR_ZERO_CONV
            and p.current.conversions == 0
            and p.revenue_weight >= 40
        ):
            add(
                p,
                f"{p.current.sessions:,} sessions and zero recorded conversions",
                severity=Severity.HIGH if p.money_page else Severity.MEDIUM,
                impact=(
                    "Meaningful organic traffic producing nothing. Either the page "
                    "cannot convert, or conversion tracking is not firing on it."
                ),
                action=(
                    "Verify the GA4 key event fires on this page before concluding "
                    "the page is at fault."
                ),
            )

        # 3. Converting, but poorly, on a page that should.
        elif (
            p.current.sessions >= MIN_SESSIONS_FOR_RATE
            and p.current.conversion_rate < WEAK_CONVERSION_RATE
            and p.money_page
        ):
            add(
                p,
                f"Conversion rate {p.current.conversion_rate:.2%} on a revenue page",
                severity=Severity.MEDIUM,
                impact="A money page converting under 1% is leaving sellers on the table.",
                action="Review the form length, above-the-fold CTA, and mobile layout.",
            )

        # 4. Mobile losing to desktop by a wide margin.
        weakness = p.mobile_weakness
        if weakness >= 0.02 and p.revenue_weight >= 40:
            mobile = p.by_device.get("mobile")
            desktop = p.by_device.get("desktop")
            add(
                p,
                f"Mobile converts {weakness:.1%} worse than desktop "
                f"({mobile.conversion_rate:.2%} vs {desktop.conversion_rate:.2%})",
                severity=Severity.HIGH if p.money_page else Severity.MEDIUM,
                impact=(
                    "Most sellers searching for a cash buyer are on a phone. A mobile "
                    "conversion gap on a revenue page is the most expensive kind."
                ),
                action=(
                    "Inspect this page at a mobile viewport — the vision agent already "
                    "captures it hourly."
                ),
            )

        # 5. Traffic falling on a page that produces conversions.
        if (
            p.prior.sessions >= MIN_SESSIONS_FOR_RATE
            and p.session_change_pct <= -25
            and p.prior.conversions >= MIN_CONVERSIONS_FOR_DECLINE
        ):
            add(
                p,
                f"Sessions down {abs(p.session_change_pct):.0f}% on a converting page",
                severity=Severity.MEDIUM,
                impact="Fewer visitors to a page that demonstrably produces conversions.",
                action="Cross-reference Search Console for a ranking or impression change.",
            )

    return findings


@collector_guard(AGENT)
def run(
    settings: Settings,
    *,
    runner: Runner | None = None,
    window_days: int = 28,
    today: date | None = None,
) -> CollectorResult:
    execute = runner
    if execute is None:
        execute, reason = _build_runner(settings)
        if execute is None:
            return CollectorResult.unavailable(AGENT, reason)

    prop = settings.secrets.ga4_property_id or "unset"
    current_window, prior_window = _windows(today or date.today(), window_days)

    try:
        now_totals = _collect(execute, prop, current_window, with_device=False)
        before_totals = _collect(execute, prop, prior_window, with_device=False)
        by_device = _collect(execute, prop, current_window, with_device=True)
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        lowered = message.lower()
        if "permission" in lowered or "403" in message:
            return CollectorResult.failed(
                AGENT,
                f"no access to GA4 property {prop}. Add the service account as a "
                f"Viewer under Property Access Management. ({message[:100]})",
            )
        if "quota" in lowered or "429" in message:
            return CollectorResult.failed(AGENT, f"GA4 quota exceeded: {message[:120]}")
        return CollectorResult.failed(AGENT, message[:200])

    if not now_totals:
        return CollectorResult.unavailable(
            AGENT,
            f"GA4 property {prop} returned no rows for "
            f"{current_window['start']}..{current_window['end']}",
        )

    weights = {
        normalize_url(p.url): (p.revenue_weight, p.money_page, p.name)
        for p in settings.pages
    }

    pages: list[LandingPage] = []
    for url in set(now_totals) | set(before_totals):
        weight, money, name = weights.get(url, (0, False, ""))
        pages.append(
            LandingPage(
                url=url,
                current=now_totals.get(url, PageMetrics()),
                prior=before_totals.get(url, PageMetrics()),
                revenue_weight=weight,
                money_page=money,
                name=name or url,
                by_device=by_device.get(url, {}),
            )
        )

    findings = detect(pages, window_days)
    pages.sort(key=lambda p: (-p.revenue_weight, -p.current.sessions))

    return CollectorResult(
        agent=AGENT,
        findings=findings,
        data={
            "property": prop,
            "window": current_window,
            "prior_window": prior_window,
            # Stated on the payload so no downstream consumer can quietly
            # reinterpret a button click as a signed contract.
            "label": (
                "GA4 key events are behavioral signals, not qualified seller "
                "leads. They become leads only when joined to CRM data in Stage 6."
            ),
            "totals": {
                "sessions": sum(p.current.sessions for p in pages),
                "conversions": sum(p.current.conversions for p in pages),
                "prior_sessions": sum(p.prior.sessions for p in pages),
                "prior_conversions": sum(p.prior.conversions for p in pages),
            },
            "pages": [p.to_dict() for p in pages[:200]],
        },
    )


def join_with_search(
    ga4_result: CollectorResult, search_pages: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """The Stage 5 deliverable: one dataset carrying both sources.

    Rows are emitted when either source has data. A page present in only one is
    marked so, rather than being dropped or having the missing half zero-filled
    — a zero-filled row reads as "measured zero" and is a lie.
    """
    rows: dict[str, dict[str, Any]] = {}

    for page in ga4_result.data.get("pages", []) or []:
        url = normalize_url(page.get("url", ""))
        if not url:
            continue
        rows[url] = {
            "url": url,
            "name": page.get("name") or url,
            "revenue_weight": page.get("revenue_weight", 0),
            "money_page": page.get("money_page", False),
            "sessions": page["current"]["sessions"],
            "conversions": page["current"]["conversions"],
            "conversion_rate": page["current"]["conversion_rate"],
            "session_delta": page.get("session_delta", 0),
            "conversion_delta": page.get("conversion_delta", 0),
            "mobile_weakness": page.get("mobile_weakness", 0.0),
            "search": None,
            "sources": ["ga4"],
        }

    for raw_url, search in search_pages.items():
        # Normalized again defensively: a caller passing raw Search Console
        # keys would otherwise create a second row for a page already present.
        url = normalize_url(raw_url) or raw_url
        row = rows.get(url)
        if row is None:
            rows[url] = {
                "url": url,
                "name": url,
                "revenue_weight": 0,
                "money_page": False,
                "sessions": None,
                "conversions": None,
                "conversion_rate": None,
                "session_delta": None,
                "conversion_delta": None,
                "mobile_weakness": None,
                "search": search,
                "sources": ["search_console"],
            }
        else:
            row["search"] = search
            row["sources"].append("search_console")

    # Clicks with no sessions, or sessions with no clicks, is worth seeing.
    for row in rows.values():
        search = row.get("search") or {}
        clicks = search.get("clicks")
        sessions = row.get("sessions")
        row["clicks_vs_sessions"] = (
            None if clicks is None or sessions is None else clicks - sessions
        )

    return sorted(
        rows.values(),
        key=lambda r: (-int(r.get("revenue_weight") or 0), -(r.get("sessions") or 0)),
    )
