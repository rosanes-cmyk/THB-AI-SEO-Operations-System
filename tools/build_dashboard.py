#!/usr/bin/env python3
"""Feed the dashboard from the real agents — Stages 4 through 7.

    python tools/build_dashboard.py              # run every agent, then write
    python tools/build_dashboard.py --offline    # skip the crawl (no site load)
    python tools/build_dashboard.py --audit-from FILE

Writes one `window.__THB__` block into dashboard/index.html carrying the
priority board, coverage, Search Console, GA4, and revenue.

The rule this file exists to enforce: **a collector that could not measure
something is passed through as `unavailable` with its reason attached, and
the dashboard renders that reason instead of a number.** There is no code
path here that substitutes a zero, a dash, or a plausible estimate for an
unmeasured value. A dashboard showing "0 leads" when GA4 was unreachable is
worse than one showing nothing, because someone will act on the zero.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DASHBOARD = REPO / "dashboard" / "index.html"
MARK_OPEN = "<!--@THB@-->"
MARK_CLOSE = "<!--@/THB@-->"

# How many rows each table carries into the page. Stated in the payload so
# the dashboard can say "top 40 of 312" rather than implying it is the whole set.
ROW_CAP = 40


def envelope(result: Any, **extra: Any) -> dict[str, Any]:
    """Normalize a CollectorResult into what the dashboard consumes.

    Data keys are only copied through when the collector reported OK. An
    unavailable or failed collector contributes its reason and nothing else,
    so no view can accidentally render a leftover key as a measurement.
    """
    status = result.status.value
    payload: dict[str, Any] = {
        "status": status,
        "reason": result.reason or result.error or "",
        "findings": len(result.findings),
    }
    if status == "ok":
        payload.update(extra)
    return payload


def search_panel(result: Any) -> dict[str, Any]:
    data = result.data if result.status.value == "ok" else {}
    pages = data.get("pages", []) or []
    queries = data.get("queries", []) or []
    return envelope(
        result,
        window=data.get("window"),
        prior_window=data.get("prior_window"),
        totals=data.get("totals", {}),
        page_count=len(pages),
        query_count=len(queries),
        pages=pages[:ROW_CAP],
        queries=queries[:ROW_CAP],
    )


def ga4_panel(result: Any) -> dict[str, Any]:
    data = result.data if result.status.value == "ok" else {}
    pages = data.get("pages", []) or []
    return envelope(
        result,
        window=data.get("window"),
        totals=data.get("totals", {}),
        label=data.get("label", ""),
        page_count=len(pages),
        pages=pages[:ROW_CAP],
    )


def revenue_panel(result: Any) -> dict[str, Any]:
    data = result.data if result.status.value == "ok" else {}
    return envelope(
        result,
        window=data.get("window"),
        currency=data.get("currency", "USD"),
        target_roas=data.get("target_roas"),
        canonical_chain=data.get("canonical_chain", []),
        channels=data.get("channels", []),
        totals=data.get("totals", {}),
        funnel_totals=data.get("funnel_totals", {}),
        data_quality=data.get("data_quality", {}),
        adapters=data.get("adapters", []),
        label=data.get("label", ""),
    )
    # Note: `adapters` is carried even though it is only meaningful when the
    # collector ran, because it tells the reader which channels are read
    # automatically (none, today) versus by hand.


def collect(offline: bool, audit_from: Path | None) -> dict[str, Any]:
    from analysis.priority_engine import board_from_results  # noqa: PLC0415
    from collectors import crawler, ga4, revenue, search_console  # noqa: PLC0415
    from collectors.base import CollectorResult  # noqa: PLC0415
    from core.config import load_settings  # noqa: PLC0415

    settings = load_settings()

    if offline:
        crawl = CollectorResult.unavailable(
            crawler.AGENT, "skipped: --offline was passed, so the site was not crawled"
        )
    else:
        print("crawling…", flush=True)
        crawl = crawler.run(settings)

    print("search console…", flush=True)
    search = search_console.run(settings)
    print("ga4…", flush=True)
    analytics = ga4.run(settings)
    print("revenue…", flush=True)
    money = revenue.run(settings)

    joined: list[dict[str, Any]] = []
    if analytics.status.value == "ok" or search.status.value == "ok":
        joined = ga4.join_with_search(
            analytics, search_console.normalized_landing_pages(search)
        )

    board = board_from_results([crawl, search, analytics, money])

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "site": settings.site_name,
        "base_url": settings.base_url,
        "row_cap": ROW_CAP,
        "board": board.to_dict(),
        "search": search_panel(search),
        "ga4": ga4_panel(analytics),
        "revenue": revenue_panel(money),
        "joined": joined[:ROW_CAP],
        "joined_count": len(joined),
        "crawl": envelope(crawl, pages_crawled=crawl.data.get("pages_crawled", 0)),
        "_audit_source": crawl,
        "_audit_from": audit_from,
    }


def inject(payload: dict[str, Any]) -> None:
    html = DASHBOARD.read_text(encoding="utf-8")
    block = (
        f"{MARK_OPEN}\n<script>window.__THB__ = "
        f"{json.dumps(payload, separators=(',', ':'), default=str)};</script>\n"
        f"{MARK_CLOSE}"
    )
    if MARK_OPEN in html:
        # A lambda, not a replacement string: the JSON contains \uXXXX escapes
        # that re.sub would otherwise read as backreference templates.
        html = re.sub(
            re.escape(MARK_OPEN) + r".*?" + re.escape(MARK_CLOSE),
            lambda _m: block,
            html,
            flags=re.S,
        )
    else:
        html = html.replace("<script>\n(() => {", block + "\n<script>\n(() => {", 1)
    DASHBOARD.write_text(html, encoding="utf-8")


def summarize(payload: dict[str, Any]) -> None:
    board = payload["board"]
    print()
    print(board["headline"])
    print()
    for name, count in board["counts"].items():
        if count:
            print(f"  {name:<22} {count}")
    print()
    for key in ("search", "ga4", "revenue"):
        panel = payload[key]
        mark = "ok  " if panel["status"] == "ok" else "-- "
        detail = panel["reason"] or f"{panel['findings']} finding(s)"
        print(f"  {mark} {key:<16} {detail[:90]}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--offline",
        action="store_true",
        help="skip the crawl; use only sources that need no site load",
    )
    ap.add_argument("--audit-from", type=Path, default=None, help="reuse a saved crawl")
    args = ap.parse_args()

    payload = collect(args.offline, args.audit_from)

    # The Site Audit view has its own long-standing block; keep it in sync in
    # the same pass so the two halves of the page can never disagree about
    # when they were built.
    audit_source = payload.pop("_audit_source")
    audit_from = payload.pop("_audit_from")
    if audit_from or audit_source.status.value == "ok":
        from build_audit import build as build_audit  # noqa: PLC0415
        from build_audit import collect as collect_audit  # noqa: PLC0415
        from build_audit import inject as inject_audit  # noqa: PLC0415

        raw = (
            collect_audit(audit_from)
            if audit_from
            else {
                "pages_crawled": audit_source.data.get("pages_crawled", 0),
                "redirects": audit_source.data.get("redirects", {}),
                "fetch_errors": audit_source.data.get("fetch_errors", []),
                "throttled": audit_source.data.get("throttled", 0),
                "pages": audit_source.data.get("pages", []),
                "findings": [f.to_dict() for f in audit_source.findings],
            }
        )
        inject_audit(build_audit(raw))

    inject(payload)
    summarize(payload)
    print(f"wrote {DASHBOARD}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
