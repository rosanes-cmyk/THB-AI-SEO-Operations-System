"""Command-line entry point.

`main.py` is for humans and cron; `runner.py` is the production service. Every
subcommand here is read-only or write-gated — nothing in this CLI can perform a
production content change.

    python main.py health              what is configured, what is failing
    python main.py config-check        validate config.yaml and exit
    python main.py policy              print the machine-readable risk policy
    python main.py heartbeat           one revenue-page check, printed
    python main.py vision              one visual inspection pass
    python main.py crawl               one technical crawl
    python main.py pagespeed           one PageSpeed pass
    python main.py digest              build and send the prioritized digest
    python main.py once                run every currently-due task once
    python main.py run                 the never-stop loop (use systemd)
    python main.py selftest            failure-injection checks, no network
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from actions.policy import policy_table
from core.config import ConfigError, load_settings


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _collector_summary(result: Any) -> dict[str, Any]:
    return {
        "agent": result.agent,
        "status": result.status.value,
        "reason": result.reason,
        "error": result.error,
        "finding_count": len(result.findings),
        "findings": [
            {
                "problem": f.problem,
                "page": f.entity or f.url,
                "viewport": f.viewport,
                "severity": f.severity.value,
                "revenue_weight": f.revenue_weight,
                "conversion_blocking": f.conversion_blocking,
                "priority_score": f.priority_score(),
                "business_impact": f.business_impact,
            }
            for f in sorted(
                result.findings, key=lambda f: f.priority_score(), reverse=True
            )
        ],
        "data_keys": sorted(result.data.keys()),
    }


def cmd_config_check(_args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"CONFIG INVALID: {exc}", file=sys.stderr)
        return 2

    _print(
        {
            "valid": True,
            "site": settings.site_name,
            "base_url": settings.base_url,
            "timezone": settings.timezone,
            "pages": [
                {
                    "name": p.name,
                    "url": p.url,
                    "money_page": p.money_page,
                    "revenue_weight": p.revenue_weight,
                }
                for p in settings.pages
            ],
            "money_page_count": len(settings.money_pages),
            "viewports": [v.name for v in settings.viewports],
            "secrets_present": {
                "anthropic": bool(settings.secrets.anthropic_api_key),
                "google_chat": bool(settings.secrets.google_chat_webhook_url),
                "pagespeed": bool(settings.secrets.pagespeed_api_key),
                "wordpress": bool(settings.secrets.wordpress_app_password),
            },
            "wordpress_writes_enabled": settings.wordpress_writes_enabled,
        }
    )
    return 0


def cmd_policy(_args: argparse.Namespace) -> int:
    _print({"risk_policy": policy_table()})
    return 0


def cmd_health(_args: argparse.Namespace) -> int:
    from runner import OperationsRunner

    _print(OperationsRunner().health())
    return 0


def cmd_heartbeat(_args: argparse.Namespace) -> int:
    from collectors import heartbeat

    _print(_collector_summary(heartbeat.run(load_settings())))
    return 0


def cmd_crawl(args: argparse.Namespace) -> int:
    from collectors import crawler

    _print(_collector_summary(crawler.run(load_settings(), max_pages=args.max_pages)))
    return 0


def cmd_pagespeed(_args: argparse.Namespace) -> int:
    from collectors import pagespeed

    _print(_collector_summary(pagespeed.run(load_settings())))
    return 0


def cmd_vision(args: argparse.Namespace) -> int:
    from analysis.claude_analyzer import ClaudeAnalyzer
    from collectors import vision

    settings = load_settings()
    analyzer = None
    if not args.no_ai:
        candidate = ClaudeAnalyzer(settings)
        analyzer = candidate if candidate.available else None
        if analyzer is None:
            print(
                f"note: AI judgment unavailable ({candidate.unavailable_reason}); "
                "running evidence-only",
                file=sys.stderr,
            )
    _print(_collector_summary(vision.run(settings, analyzer=analyzer)))
    return 0


def cmd_digest(_args: argparse.Namespace) -> int:
    from runner import OperationsRunner

    _print(OperationsRunner().task_digest())
    return 0


def cmd_once(_args: argparse.Namespace) -> int:
    from runner import OperationsRunner

    runner = OperationsRunner()
    runner.scheduler.prime()
    outcomes = runner.run_once()
    _print(
        [
            {
                "task": o.name,
                "ok": o.ok,
                "error": o.error,
                "seconds": round(o.duration_seconds, 2),
                "result": o.result,
            }
            for o in outcomes
        ]
    )
    return 0 if all(o.ok for o in outcomes) else 1


def cmd_run(_args: argparse.Namespace) -> int:
    from runner import OperationsRunner

    return OperationsRunner().run_forever()


def cmd_selftest(_args: argparse.Namespace) -> int:
    """Offline failure-injection checks.

    Proves the isolation guarantees without touching the network: a raising
    collector, a corrupt state file, and an unreachable Chat webhook must all
    leave the service running.
    """
    import tempfile
    from pathlib import Path

    from core.scheduler import Scheduler, Task
    from core.state import StateStore
    from notifications.google_chat import GoogleChatNotifier

    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"check": name, "passed": passed, "detail": detail})

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # 1. A task that raises must not escape the scheduler.
        state = StateStore(path=root / "state.json")
        scheduler = Scheduler(state)

        def boom() -> None:
            raise RuntimeError("injected failure")

        scheduler.add(Task(name="exploding", func=boom, interval_seconds=60))
        scheduler.add(Task(name="healthy", func=lambda: "ok", interval_seconds=60))
        scheduler.prime()
        outcomes = scheduler.run_due()
        check(
            "task exception is isolated",
            len(outcomes) == 2
            and any(o.name == "exploding" and not o.ok for o in outcomes)
            and any(o.name == "healthy" and o.ok for o in outcomes),
            "a failing task did not prevent a healthy task from running",
        )
        check(
            "failure is counted in state",
            state.task("exploding").consecutive_failures == 1,
            f"consecutive_failures={state.task('exploding').consecutive_failures}",
        )
        check(
            "failing task backs off",
            (scheduler.tasks[0].next_run_at or None) is not None,
            "next_run_at was rescheduled after the failure",
        )

        # 2. A corrupt state file must recover, not crash.
        corrupt = root / "corrupt.json"
        corrupt.write_text("{not json at all", encoding="utf-8")
        recovered = StateStore.load(corrupt)
        check(
            "corrupt state recovers",
            recovered.recovered_from_corruption and recovered.tasks == {},
            "quarantined the bad file and started clean",
        )
        check(
            "corrupt state is preserved for investigation",
            any(p.name.startswith("corrupt.json.corrupt-") for p in root.iterdir()),
            "original file moved aside rather than deleted",
        )

        # 3. State must survive a restart.
        state.save()
        reloaded = StateStore.load(root / "state.json")
        check(
            "state survives restart",
            reloaded.task("exploding").consecutive_failures == 1,
            "task health reloaded from disk",
        )

        # 4. A Chat failure must be non-fatal and must spool the message.
        def failing_poster(*_a: Any, **_k: Any) -> int:
            raise ConnectionError("injected chat outage")

        notifier = GoogleChatNotifier(
            "https://chat.googleapis.com/injected",
            root / "outbox.jsonl",
            poster=failing_poster,
        )
        delivered = notifier.send_text("test", kind="selftest")
        check(
            "chat outage is non-fatal",
            delivered is False and notifier.outbox_size() == 1,
            "message spooled to the outbox instead of being lost",
        )

        # 5. An unconfigured Chat webhook must also spool rather than raise.
        quiet = GoogleChatNotifier("", root / "outbox2.jsonl")
        check(
            "unconfigured chat is non-fatal",
            quiet.send_text("test") is False and quiet.outbox_size() == 1,
            "no webhook configured; message retained",
        )

    passed = all(c["passed"] for c in checks)
    _print({"passed": passed, "checks": checks})
    return 0 if passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="thb",
        description="THB AI SEO Operations System",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("config-check", help="validate configuration").set_defaults(
        func=cmd_config_check
    )
    sub.add_parser("policy", help="print the risk policy table").set_defaults(
        func=cmd_policy
    )
    sub.add_parser("health", help="print system health").set_defaults(func=cmd_health)
    sub.add_parser("heartbeat", help="run one revenue-page heartbeat").set_defaults(
        func=cmd_heartbeat
    )

    crawl = sub.add_parser("crawl", help="run one technical crawl")
    crawl.add_argument("--max-pages", type=int, default=None)
    crawl.set_defaults(func=cmd_crawl)

    sub.add_parser("pagespeed", help="run one PageSpeed pass").set_defaults(
        func=cmd_pagespeed
    )

    vis = sub.add_parser("vision", help="run one visual inspection pass")
    vis.add_argument(
        "--no-ai", action="store_true", help="capture browser evidence only"
    )
    vis.set_defaults(func=cmd_vision)

    sub.add_parser("digest", help="build and send the prioritized digest").set_defaults(
        func=cmd_digest
    )
    sub.add_parser("once", help="run every due task once").set_defaults(func=cmd_once)
    sub.add_parser("run", help="run the never-stop loop").set_defaults(func=cmd_run)
    sub.add_parser("selftest", help="offline failure-injection checks").set_defaults(
        func=cmd_selftest
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
