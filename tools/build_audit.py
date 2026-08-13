#!/usr/bin/env python3
"""Turn a real crawl into the dashboard's Site Audit data.

    python tools/build_audit.py                 # crawl now, then write
    python tools/build_audit.py --from FILE      # reuse a saved crawl

Writes a `window.__AUDIT__` block into dashboard/index.html. The dashboard
renders whatever this produces and shows an explicit empty state when there is
nothing — it never falls back to sample numbers for this view.

Site health is deliberately simple and stated in the output: the share of
crawled pages that carry no finding. A score nobody can explain is a score
nobody should act on.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DASHBOARD = REPO / "dashboard" / "index.html"
MARK_OPEN = "<!--@AUDIT@-->"
MARK_CLOSE = "<!--@/AUDIT@-->"

# Severity -> which bucket it lands in on the audit summary.
BUCKET = {
    "critical": "errors",
    "high": "errors",
    "medium": "warnings",
    "low": "notices",
    "info": "notices",
}


def collect(saved: Path | None) -> dict:
    if saved:
        return json.loads(saved.read_text(encoding="utf-8"))

    from collectors import crawler  # noqa: PLC0415
    from core.config import load_settings  # noqa: PLC0415

    result = crawler.run(load_settings())
    return {
        "pages_crawled": result.data.get("pages_crawled", 0),
        "redirects": result.data.get("redirects", {}),
        "fetch_errors": result.data.get("fetch_errors", []),
        "pages": result.data.get("pages", []),
        "findings": [f.to_dict() for f in result.findings],
    }


def themes(pages: list[dict]) -> list[list]:
    """Percentage of crawled pages passing each structural check."""
    total = len(pages) or 1

    def pct(predicate) -> int:
        return round(100 * sum(1 for p in pages if predicate(p)) / total)

    return [
        ["Crawlability", pct(lambda p: p.get("status", 0) < 400)],
        ["Titles", pct(lambda p: bool(p.get("title")))],
        ["Meta descriptions", pct(lambda p: bool(p.get("meta_description")))],
        ["H1 present", pct(lambda p: bool(p.get("h1")))],
        ["Canonical self-referencing", pct(lambda p: p.get("canonical") == p.get("url"))],
        ["Indexable", pct(lambda p: not p.get("noindex"))],
    ]


def build(raw: dict) -> dict:
    pages = raw.get("pages", [])
    findings = raw.get("findings", [])
    page_count = raw.get("pages_crawled", len(pages))

    counts = Counter(BUCKET.get(f.get("severity", "low"), "notices") for f in findings)
    affected = {f.get("url") for f in findings if f.get("url")}
    health = round(100 * (page_count - len(affected)) / page_count) if page_count else 0

    grouped: dict[str, dict] = {}
    for f in findings:
        # Collapse "Duplicate title shared by 3 pages" and friends into one
        # row -- but never collapse distinct HTTP statuses. Grouping 404 and
        # 503 together and labelling the row with whichever arrived first
        # reports a rate limit as a broken page.
        problem = f.get("problem", "")
        status = re.search(r"HTTP (\d{3})", problem)
        key = f"http-{status.group(1)}" if status else re.sub(r"\d+", "N", problem)[:90]
        row = grouped.setdefault(
            key,
            {
                "problem": f.get("problem", "")[:90],
                "severity": f.get("severity", "low"),
                "pages": 0,
                "weight": 0,
                "score": 0.0,
            },
        )
        row["pages"] += 1
        row["weight"] = max(row["weight"], int(f.get("revenue_weight", 0)))
        row["score"] = max(row["score"], float(f.get("priority_score", 0)))

    issues = sorted(grouped.values(), key=lambda r: -r["score"])[:15]
    for row in issues:
        row["score"] = round(row["score"])

    if not findings:
        verdict = "No defects found"
        throttled = int(raw.get("throttled", 0))
        blurb = (
            f"All {page_count:,} crawled pages returned 200 and carry a title, meta "
            "description, H1, and a self-referencing canonical."
        )
        if throttled:
            blurb += (
                f" The crawl stopped early after {throttled} throttled responses — "
                "the host returned 503 under sustained crawling, so coverage is "
                "partial. That is a server-load signal, not a page defect."
            )
    else:
        blocking = sum(1 for f in findings if f.get("conversion_blocking"))
        verdict = (
            f"{blocking} conversion-blocking issue{'s' if blocking != 1 else ''}"
            if blocking
            else f"{len(findings)} issue{'s' if len(findings) != 1 else ''}, none blocking conversion"
        )
        blurb = (
            f"Health is the share of the {page_count:,} crawled pages carrying no "
            "finding. Issues are ranked by revenue impact, so a defect on a money "
            "page outranks the same defect on a city page."
        )

    return {
        "crawled_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "pages": page_count,
        "health": health,
        "errors": counts.get("errors", 0),
        "warnings": counts.get("warnings", 0),
        "notices": counts.get("notices", 0),
        "redirects": len(raw.get("redirects", {})),
        "fetch_errors": len(raw.get("fetch_errors", [])),
        "throttled": int(raw.get("throttled", 0)),
        "verdict": verdict,
        "blurb": blurb,
        "issues": issues,
        "themes": themes(pages),
    }


def inject(audit: dict) -> None:
    html = DASHBOARD.read_text(encoding="utf-8")
    block = (
        f"{MARK_OPEN}\n<script>window.__AUDIT__ = "
        f"{json.dumps(audit, separators=(',', ':'))};</script>\n{MARK_CLOSE}"
    )
    if MARK_OPEN in html:
        # Lambda, not a string: the JSON payload contains \uXXXX escapes that
        # re.sub would otherwise try to interpret as replacement templates.
        html = re.sub(
            re.escape(MARK_OPEN) + r".*?" + re.escape(MARK_CLOSE),
            lambda _m: block, html, flags=re.S,
        )
    else:
        html = html.replace("<script>\n(() => {", block + "\n<script>\n(() => {", 1)
    DASHBOARD.write_text(html, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="saved", type=Path, default=None)
    args = ap.parse_args()

    audit = build(collect(args.saved))
    inject(audit)

    print(f"pages      {audit['pages']:,}")
    print(f"health     {audit['health']}%")
    print(f"errors     {audit['errors']}   warnings {audit['warnings']}   notices {audit['notices']}")
    print(f"verdict    {audit['verdict']}")
    for row in audit["issues"][:8]:
        print(f"  [{row['score']:>4}] {row['severity']:<8} x{row['pages']:<4} {row['problem'][:56]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
