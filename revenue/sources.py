"""Where funnel data comes from.

Two kinds of source live here.

The CSV bridge works today. It reads whatever the CRM, REI BlackBook, or a
spreadsheet can export, which is the only revenue data that actually exists
right now. It is deliberately forgiving about column names and deliberately
unforgiving about values: a row it cannot parse is recorded as a rejected row
with the reason, never dropped silently and never defaulted.

The API adapters are declared but not implemented. Each one reports exactly
what it needs before it can run. That is the honest state of the integration
(Rule 7) — a stub that returned zeros would be indistinguishable from a
channel that genuinely produced nothing, which is the most expensive lie this
system could tell.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from revenue.funnel import Attribution, Deal, FunnelStage, Spend

logger = logging.getLogger(__name__)


@dataclass
class LoadResult:
    """What a source produced, plus everything it could not use."""

    deals: list[Deal] = field(default_factory=list)
    spends: list[Spend] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    sources_read: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    def merge(self, other: "LoadResult") -> None:
        self.deals.extend(other.deals)
        self.spends.extend(other.spends)
        self.rejected.extend(other.rejected)
        self.sources_read.extend(other.sources_read)
        self.notes.extend(other.notes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deal_count": len(self.deals),
            "spend_records": len(self.spends),
            "rejected_count": self.rejected_count,
            # Capped so one malformed export cannot bloat the report, but the
            # count above is always the true total.
            "rejected": self.rejected[:50],
            "sources_read": self.sources_read,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------
# CSV bridge
# --------------------------------------------------------------------------

# Column aliases. Every CRM names these differently and none of them ask.
DEAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "deal_id": ("deal_id", "id", "lead_id", "opportunity_id", "record_id", "uuid"),
    "channel": ("channel", "source", "lead_source", "marketing_source", "campaign_source"),
    "attribution": ("attribution", "attribution_method", "attribution_type", "how_known"),
    "stage": ("stage", "status", "deal_stage", "pipeline_stage", "funnel_stage"),
    "created_on": ("created_on", "created", "created_date", "date", "lead_date", "created_at"),
    "closed_on": ("closed_on", "closed", "closed_date", "close_date", "closing_date"),
    "gross_profit": (
        "gross_profit", "profit", "net_profit", "margin", "gp", "gross_profit_usd",
    ),
    "landing_page": ("landing_page", "landing_url", "entry_page", "url", "page"),
    "source_detail": ("source_detail", "campaign", "ad_group", "keyword", "detail"),
    "notes": ("notes", "note", "comment", "comments"),
}

SPEND_COLUMNS: dict[str, tuple[str, ...]] = {
    "channel": ("channel", "source", "platform", "medium"),
    "period_start": ("period_start", "start", "start_date", "from", "month_start"),
    "period_end": ("period_end", "end", "end_date", "to", "month_end"),
    "amount": ("amount", "spend", "cost", "total", "budget_spent", "usd"),
}

DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%b %d, %Y",
    "%B %d, %Y",
    "%Y-%m-%dT%H:%M:%S",
)


def _normalize_header(name: str) -> str:
    return (name or "").strip().lower().replace(" ", "_").replace("-", "_").lstrip("﻿")


def _pick(row: dict[str, str], aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        if alias in row and str(row[alias]).strip():
            return str(row[alias]).strip()
    return ""


def parse_date(raw: str) -> date | None:
    """Parse a date the way a spreadsheet actually wrote it, or give up."""
    text = (raw or "").strip()
    if not text:
        return None
    if " " in text and "T" not in text:
        text = text.split(" ")[0] if "," not in text else text
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_money(raw: str) -> float | None:
    """Parse currency as exported: $1,234.56, (500) for negative, 1234.56."""
    text = (raw or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    for char in "$,()USD ":
        text = text.replace(char, "")
    if not text or text in {"-", "."}:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def _read_rows(path: Path) -> tuple[list[dict[str, str]], str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            sample = handle.read(4096)
            handle.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(handle, dialect=dialect)
            rows = [
                {_normalize_header(k): (v if v is not None else "") for k, v in row.items()}
                for row in reader
            ]
        return rows, ""
    except OSError as exc:
        return [], f"could not read {path.name}: {exc}"
    except csv.Error as exc:
        return [], f"malformed CSV in {path.name}: {exc}"


def load_deals_csv(path: Path) -> LoadResult:
    """Read a deals export. Rows that cannot be parsed are reported, not dropped."""
    result = LoadResult()
    rows, error = _read_rows(path)
    if error:
        result.notes.append(error)
        return result
    result.sources_read.append(str(path))

    seen: set[str] = set()
    for line_no, row in enumerate(rows, start=2):
        def col(field_name: str) -> str:
            return _pick(row, DEAL_COLUMNS[field_name])

        def reject(reason: str) -> None:
            result.rejected.append(
                {"file": path.name, "line": str(line_no), "reason": reason}
            )

        stage_raw = col("stage")
        stage = FunnelStage.parse(stage_raw)
        if stage is None:
            reject(
                f"unrecognized stage {stage_raw!r}; expected one of "
                f"{', '.join(s.value for s in FunnelStage)}"
            )
            continue

        created = parse_date(col("created_on"))
        if created is None:
            reject(f"unparseable created date {col('created_on')!r}")
            continue

        deal_id = col("deal_id") or f"{path.stem}-{line_no}"
        if deal_id in seen:
            reject(f"duplicate deal_id {deal_id!r}")
            continue
        seen.add(deal_id)

        profit = parse_money(col("gross_profit"))
        if stage is FunnelStage.CLOSING and profit is None:
            # Counting a closing as $0 profit would drag every ROAS down and
            # look like a real result. Reject it so someone fixes the export.
            reject(f"deal {deal_id} is closed but has no gross profit recorded")
            continue

        result.deals.append(
            Deal(
                deal_id=deal_id,
                channel=col("channel"),
                attribution=Attribution.parse(col("attribution")),
                stage=stage,
                created_on=created,
                closed_on=parse_date(col("closed_on")),
                gross_profit=profit or 0.0,
                landing_page=col("landing_page"),
                source_detail=col("source_detail"),
                notes=col("notes"),
            )
        )
    return result


def load_spend_csv(path: Path) -> LoadResult:
    """Read a channel spend export."""
    result = LoadResult()
    rows, error = _read_rows(path)
    if error:
        result.notes.append(error)
        return result
    result.sources_read.append(str(path))

    for line_no, row in enumerate(rows, start=2):
        def col(field_name: str) -> str:
            return _pick(row, SPEND_COLUMNS[field_name])

        def reject(reason: str) -> None:
            result.rejected.append(
                {"file": path.name, "line": str(line_no), "reason": reason}
            )

        channel = col("channel")
        if not channel:
            reject("no channel column value")
            continue
        start = parse_date(col("period_start"))
        end = parse_date(col("period_end"))
        if start is None or end is None:
            reject(f"unparseable period {col('period_start')!r}..{col('period_end')!r}")
            continue
        if end < start:
            reject(f"period ends before it starts ({start}..{end})")
            continue
        amount = parse_money(col("amount"))
        if amount is None:
            reject(f"unparseable amount {col('amount')!r}")
            continue

        result.spends.append(
            Spend(channel=channel, period_start=start, period_end=end, amount=amount)
        )
    return result


def load_csv_directory(directory: Path) -> LoadResult:
    """Load every deals/spend CSV in a directory.

    Files are matched by name: anything containing "deal", "lead", "crm", or
    "pipeline" is read as deals; anything containing "spend", "cost", or "ad"
    is read as spend. A file matching neither is reported so it is not
    silently ignored.
    """
    result = LoadResult()
    if not directory.exists():
        result.notes.append(
            f"no revenue data directory at {directory}. Export deals.csv and "
            f"spend.csv from your CRM and drop them there."
        )
        return result

    files = sorted(p for p in directory.glob("*.csv") if p.is_file())
    if not files:
        result.notes.append(f"{directory} contains no CSV files")
        return result

    for path in files:
        name = path.stem.lower()
        if any(token in name for token in ("spend", "cost", "ad_", "ads", "budget")):
            result.merge(load_spend_csv(path))
        elif any(token in name for token in ("deal", "lead", "crm", "pipeline", "closing")):
            result.merge(load_deals_csv(path))
        else:
            result.notes.append(
                f"skipped {path.name}: filename does not say whether it holds "
                f"deals or spend. Rename it to include 'deals' or 'spend'."
            )
    return result


# --------------------------------------------------------------------------
# API adapters — declared, not yet implemented
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Adapter:
    """A channel data source that is not wired up yet."""

    key: str
    label: str
    provides: tuple[str, ...]
    requires: tuple[str, ...]

    def status(self) -> dict[str, Any]:
        return {
            "channel": self.key,
            "label": self.label,
            "state": "not_implemented",
            "provides": list(self.provides),
            "requires": list(self.requires),
        }


ADAPTERS: tuple[Adapter, ...] = (
    Adapter(
        key="google_ads",
        label="Google Ads",
        provides=("spend", "source_reported conversions"),
        requires=("developer token", "OAuth client", "customer ID"),
    ),
    Adapter(
        key="lsa",
        label="Google Local Services Ads",
        provides=("spend", "charged leads"),
        requires=("Local Services API access", "account ID"),
    ),
    Adapter(
        key="yelp",
        label="Yelp Ads",
        provides=("spend", "leads"),
        requires=("Yelp partner API credentials"),
    ),
    Adapter(
        key="direct_mail",
        label="Direct mail",
        provides=("spend", "response tracking"),
        requires=("campaign cost export", "tracking number or code map"),
    ),
    Adapter(
        key="callrail",
        label="CallRail",
        provides=("calls", "first/last touch source", "recordings"),
        requires=("CallRail API key", "account ID"),
    ),
    Adapter(
        key="crm",
        label="CRM pipeline",
        provides=("deal stages", "gross profit", "close dates"),
        requires=("CRM API credentials"),
    ),
    Adapter(
        key="rei_blackbook",
        label="REI BlackBook",
        provides=("deal stages", "seller records"),
        requires=("REI BlackBook API key"),
    ),
)


def adapter_status() -> list[dict[str, Any]]:
    """What each channel integration would need. Reported in every run so the
    gap between "no data" and "no integration" is never ambiguous."""
    return [a.status() for a in ADAPTERS]


def load(directory: Path) -> LoadResult:
    """The one entry point Stage 6 calls. CSV today, adapters later."""
    result = load_csv_directory(directory)
    result.notes.append(
        "Channel data is read from CSV exports. API adapters "
        f"({', '.join(a.key for a in ADAPTERS)}) are declared but not "
        "implemented; no channel is being read automatically."
    )
    return result


def deals_in_window(deals: Iterable[Deal], start: date, end: date) -> list[Deal]:
    return [d for d in deals if start <= d.created_on <= end]
