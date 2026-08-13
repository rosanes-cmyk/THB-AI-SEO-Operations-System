"""Search Console intelligence — Stage 4.

Replaces the ranking and performance work a human would otherwise do by hand
in an SEO platform. Search Console is first-party truth: it is what Google
actually recorded, not a third party's model of it.

Three principles shape this file:

  Deterministic first.  Every delta, percentage, and threshold is computed in
  plain Python before Claude sees anything. The reasoning engine ranks
  pre-computed signals; it never invents a number (Rule 7).

  Volume before percentage.  A page going from 2 clicks to 4 is not a 100%
  win. Every opportunity check enforces a minimum volume floor, because
  percentage swings on tiny numbers are the main source of SEO noise.

  Revenue weight decides severity.  A 30% click decline on the seller form
  outranks the same decline on a city page, always.

History is written dated and never overwritten, so trend analysis in Stage 12
has real history to read rather than a single current snapshot.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from collectors.base import CollectorResult, collector_guard
from core.config import Settings
from core.models import Finding, RiskTier, Severity
from core.urls import normalize_url

logger = logging.getLogger(__name__)

AGENT = "search_console"

SCOPES = ("https://www.googleapis.com/auth/webmasters.readonly",)
API_ROWS = 25_000

# Search Console data lags roughly two days. Comparing "yesterday" against a
# complete prior week reads every partial day as a collapse.
DATA_LAG_DAYS = 2

# Volume floors. Below these, a percentage change is noise, not signal.
MIN_CLICKS_FOR_DECLINE = 10
MIN_IMPRESSIONS_FOR_CTR = 200
MIN_IMPRESSIONS_FOR_STRIKING = 100

# A page in positions 4-15 is close enough that improving it is realistic.
STRIKING_RANGE = (4.0, 15.0)

# Below this CTR at meaningful impressions, the listing is underperforming.
WEAK_CTR = 0.02


@dataclass
class Metrics:
    """One row of Search Console data for one entity in one window."""

    clicks: int = 0
    impressions: int = 0
    ctr: float = 0.0
    position: float = 0.0

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Metrics":
        return cls(
            clicks=int(row.get("clicks", 0)),
            impressions=int(row.get("impressions", 0)),
            ctr=float(row.get("ctr", 0.0)),
            position=float(row.get("position", 0.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "clicks": self.clicks,
            "impressions": self.impressions,
            "ctr": round(self.ctr, 4),
            "position": round(self.position, 1),
        }


@dataclass
class Comparison:
    """An entity's current vs prior window, with deltas already computed."""

    key: str
    current: Metrics
    prior: Metrics
    url: str = ""
    revenue_weight: int = 0
    money_page: bool = False
    name: str = ""

    @property
    def click_delta(self) -> int:
        return self.current.clicks - self.prior.clicks

    @property
    def impression_delta(self) -> int:
        return self.current.impressions - self.prior.impressions

    @property
    def ctr_delta(self) -> float:
        return self.current.ctr - self.prior.ctr

    @property
    def position_delta(self) -> float:
        """Negative is an improvement — position 3 beats position 8."""
        if not self.prior.position or not self.current.position:
            return 0.0
        return self.current.position - self.prior.position

    @property
    def click_change_pct(self) -> float:
        if not self.prior.clicks:
            return 0.0
        return (self.click_delta / self.prior.clicks) * 100

    @property
    def is_striking_distance(self) -> bool:
        low, high = STRIKING_RANGE
        return (
            low <= self.current.position <= high
            and self.current.impressions >= MIN_IMPRESSIONS_FOR_STRIKING
        )

    @property
    def has_weak_ctr(self) -> bool:
        return (
            self.current.impressions >= MIN_IMPRESSIONS_FOR_CTR
            and self.current.ctr < WEAK_CTR
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "url": self.url,
            "name": self.name,
            "revenue_weight": self.revenue_weight,
            "money_page": self.money_page,
            "current": self.current.to_dict(),
            "prior": self.prior.to_dict(),
            "click_delta": self.click_delta,
            "click_change_pct": round(self.click_change_pct, 1),
            "impression_delta": self.impression_delta,
            "ctr_delta": round(self.ctr_delta, 4),
            "position_delta": round(self.position_delta, 1),
            "striking_distance": self.is_striking_distance,
            "weak_ctr": self.has_weak_ctr,
        }


# A querier takes (property_url, request_body) and returns the API response.
Querier = Callable[[str, dict[str, Any]], dict[str, Any]]


def _build_querier(settings: Settings) -> tuple[Querier | None, str]:
    """Build a real API client, or explain why we cannot."""
    creds_path = settings.secrets.google_credentials_path
    if not creds_path:
        return None, "GOOGLE_APPLICATION_CREDENTIALS is not set"

    path = Path(creds_path)
    if not path.exists():
        return None, f"credentials file not found at {creds_path}"

    try:
        from google.oauth2 import service_account  # noqa: PLC0415
        from googleapiclient.discovery import build  # noqa: PLC0415
    except ImportError:
        return None, (
            "google-api-python-client is not installed; run "
            "`pip install google-api-python-client google-auth`"
        )

    try:
        credentials = service_account.Credentials.from_service_account_file(
            str(path), scopes=list(SCOPES)
        )
        service = build(
            "searchconsole", "v1", credentials=credentials, cache_discovery=False
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"could not authenticate: {type(exc).__name__}: {exc}"

    def query(property_url: str, body: dict[str, Any]) -> dict[str, Any]:
        return (
            service.searchanalytics()
            .query(siteUrl=property_url, body=body)
            .execute()
        )

    return query, ""


def windows(today: date, days: int) -> tuple[tuple[date, date], tuple[date, date]]:
    """Current and prior comparison windows, respecting Search Console's lag."""
    end = today - timedelta(days=DATA_LAG_DAYS)
    start = end - timedelta(days=days - 1)
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=days - 1)
    return (start, end), (prior_start, prior_end)


def _fetch(
    query: Querier,
    property_url: str,
    dimension: str,
    window: tuple[date, date],
) -> dict[str, Metrics]:
    """One dimension, one window, keyed by dimension value."""
    body = {
        "startDate": window[0].isoformat(),
        "endDate": window[1].isoformat(),
        "dimensions": [dimension],
        "rowLimit": API_ROWS,
        "dataState": "final",
    }
    response = query(property_url, body) or {}
    out: dict[str, Metrics] = {}
    for row in response.get("rows", []) or []:
        keys = row.get("keys") or []
        if not keys:
            continue
        key = str(keys[0])
        if dimension == "page":
            key = normalize_url(key) or key
        out[key] = Metrics.from_row(row)
    return out


def compare(
    current: dict[str, Metrics],
    prior: dict[str, Metrics],
    settings: Settings,
    *,
    is_page: bool,
) -> list[Comparison]:
    """Join two windows into per-entity comparisons, weighted by revenue."""
    weights = {
        normalize_url(p.url): (p.revenue_weight, p.money_page, p.name)
        for p in settings.pages
    }
    out: list[Comparison] = []
    for key in set(current) | set(prior):
        weight, money, name = (0, False, "")
        if is_page:
            weight, money, name = weights.get(key, (0, False, ""))
        out.append(
            Comparison(
                key=key,
                current=current.get(key, Metrics()),
                prior=prior.get(key, Metrics()),
                url=key if is_page else "",
                revenue_weight=weight,
                money_page=money,
                name=name or (key if is_page else ""),
            )
        )
    return out


def _severity(weight: int, magnitude: float) -> Severity:
    """Revenue weight sets the ceiling; magnitude sets the level under it."""
    if weight >= 80:
        return Severity.HIGH if magnitude >= 25 else Severity.MEDIUM
    if weight >= 40:
        return Severity.MEDIUM if magnitude >= 25 else Severity.LOW
    return Severity.LOW


def detect(pages: list[Comparison], queries: list[Comparison], window_days: int) -> list[Finding]:
    """Deterministic opportunity detection. No AI involved."""
    findings: list[Finding] = []

    def add(
        c: Comparison,
        problem: str,
        *,
        severity: Severity,
        impact: str,
        action: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        findings.append(
            Finding(
                source_agent=AGENT,
                problem=problem,
                url=c.url,
                entity=c.name or c.key,
                severity=severity,
                confidence=0.95,  # measured, but attribution of cause is not
                revenue_weight=c.revenue_weight,
                conversion_blocking=False,
                business_impact=impact,
                evidence={"window_days": window_days, **c.to_dict(), **(extra or {})},
                recommended_action=action,
                risk_tier=RiskTier.HUMAN_ONLY,
                verification_plan=(
                    "Re-pull Search Console for the next window and confirm the "
                    "direction of travel."
                ),
            )
        )

    for c in pages:
        # 1. Money pages losing clicks. The headline check.
        if c.prior.clicks >= MIN_CLICKS_FOR_DECLINE and c.click_delta < 0:
            drop = abs(c.click_change_pct)
            if drop >= 20 and c.revenue_weight > 0:
                add(
                    c,
                    f"Clicks down {drop:.0f}% over {window_days} days "
                    f"({c.prior.clicks} to {c.current.clicks})",
                    severity=_severity(c.revenue_weight, drop),
                    impact=(
                        f"{c.name or c.key} produces seller leads. A sustained "
                        f"{drop:.0f}% click decline is fewer people reaching the form."
                        if c.money_page
                        else "Traffic decline on a page with some revenue weight."
                    ),
                    action=(
                        "Check for a ranking change, a SERP layout change, or a "
                        "recent edit to this page."
                    ),
                )

        # 2. Ranking decline on a page that matters.
        if (
            c.position_delta >= 3
            and c.prior.impressions >= MIN_IMPRESSIONS_FOR_STRIKING
            and c.revenue_weight >= 40
        ):
            add(
                c,
                f"Average position fell {c.position_delta:.1f} places "
                f"({c.prior.position:.1f} to {c.current.position:.1f})",
                severity=_severity(c.revenue_weight, c.position_delta * 8),
                impact="Losing rank on a revenue page precedes losing the leads it produces.",
                action="Compare against competitors now ranking above this page.",
            )

        # 3. Demand is there, the listing is not earning the click.
        if c.has_weak_ctr and c.revenue_weight >= 40:
            add(
                c,
                f"CTR {c.current.ctr:.1%} at {c.current.impressions:,} impressions",
                severity=Severity.LOW,
                impact=(
                    "People see this page in search and do not click. Title and "
                    "description are the cheapest fix in SEO."
                ),
                action="Rewrite the title and meta description around seller intent.",
            )

        # 4. Close enough to page one to be worth the effort.
        if c.is_striking_distance and c.revenue_weight >= 40:
            add(
                c,
                f"In striking distance at position {c.current.position:.1f}",
                severity=Severity.INFO,
                impact=(
                    f"{c.current.impressions:,} impressions already. Moving this "
                    "into the top 3 converts existing demand."
                ),
                action="Strengthen the page's depth and internal links.",
            )

    # 5. Queries with real demand the site is not converting into clicks.
    for c in queries:
        if c.has_weak_ctr:
            add(
                c,
                f"Query '{c.key}' — {c.current.impressions:,} impressions, "
                f"CTR {c.current.ctr:.1%}",
                severity=Severity.INFO,
                impact="Demand exists for this query and the site is not capturing it.",
                action="Target this query explicitly on the closest money page.",
                extra={"query": c.key},
            )

    return findings


def _persist(settings: Settings, payload: dict[str, Any], pulled_on: date) -> Path:
    """Dated raw history. Never overwritten — Stage 12 needs the trend."""
    directory = settings.data_dir / "search_console"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pulled_on.isoformat()}.json"
    path.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    return path


@collector_guard(AGENT)
def run(
    settings: Settings,
    *,
    querier: Querier | None = None,
    window_days: int = 28,
    today: date | None = None,
) -> CollectorResult:
    """Pull Search Console, compute deltas, and emit weighted findings."""
    query = querier
    if query is None:
        query, reason = _build_querier(settings)
        if query is None:
            return CollectorResult.unavailable(AGENT, reason)

    property_url = settings.secrets.search_console_property or settings.base_url
    now = today or date.today()
    current_window, prior_window = windows(now, window_days)

    try:
        pages_now = _fetch(query, property_url, "page", current_window)
        pages_before = _fetch(query, property_url, "page", prior_window)
        queries_now = _fetch(query, property_url, "query", current_window)
        queries_before = _fetch(query, property_url, "query", prior_window)
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        if "403" in message or "permission" in message.lower():
            return CollectorResult.failed(
                AGENT,
                f"no access to Search Console property {property_url}. Grant the "
                f"service account read access. ({message[:120]})",
            )
        if "404" in message:
            return CollectorResult.failed(
                AGENT, f"property {property_url} not found in Search Console"
            )
        if "429" in message or "quota" in message.lower():
            return CollectorResult.failed(AGENT, f"Search Console quota exceeded: {message[:120]}")
        return CollectorResult.failed(AGENT, message[:200])

    if not pages_now and not queries_now:
        return CollectorResult.unavailable(
            AGENT,
            f"Search Console returned no rows for {property_url} in "
            f"{current_window[0]}..{current_window[1]}",
        )

    page_comparisons = compare(pages_now, pages_before, settings, is_page=True)
    query_comparisons = compare(queries_now, queries_before, settings, is_page=False)
    findings = detect(page_comparisons, query_comparisons, window_days)

    totals = {
        "clicks": sum(m.clicks for m in pages_now.values()),
        "impressions": sum(m.impressions for m in pages_now.values()),
        "prior_clicks": sum(m.clicks for m in pages_before.values()),
        "prior_impressions": sum(m.impressions for m in pages_before.values()),
    }

    payload = {
        "property": property_url,
        "window": [current_window[0].isoformat(), current_window[1].isoformat()],
        "prior_window": [prior_window[0].isoformat(), prior_window[1].isoformat()],
        "totals": totals,
        "pages": [c.to_dict() for c in sorted(
            page_comparisons, key=lambda c: -c.current.clicks)[:200]],
        "queries": [c.to_dict() for c in sorted(
            query_comparisons, key=lambda c: -c.current.clicks)[:200]],
    }
    stored = _persist(settings, payload, now)

    return CollectorResult(
        agent=AGENT,
        findings=findings,
        data={**payload, "history_file": str(stored), "page_count": len(page_comparisons)},
    )


def normalized_landing_pages(result: CollectorResult) -> dict[str, dict[str, Any]]:
    """Search Console half of the Stage 5 landing-page join.

    Keyed on the same normalized URL GA4 will use, so the two datasets merge
    without guessing.
    """
    out: dict[str, dict[str, Any]] = {}
    for row in result.data.get("pages", []) or []:
        url = normalize_url(row.get("url", ""))
        if not url:
            continue
        out[url] = {
            "clicks": row["current"]["clicks"],
            "impressions": row["current"]["impressions"],
            "ctr": row["current"]["ctr"],
            "position": row["current"]["position"],
            "click_delta": row["click_delta"],
            "position_delta": row["position_delta"],
        }
    return out
