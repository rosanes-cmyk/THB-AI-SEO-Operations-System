"""Never-stop orchestrator.

This is the production runtime. It is designed around one assumption: every
dependency will fail eventually, and the monitoring must outlive all of them.

  * Every task runs inside the scheduler's isolation boundary (Rule 8).
  * The main loop body is itself wrapped, so an unexpected exception anywhere
    logs and continues rather than exiting (Rule 10).
  * State is written atomically after every cycle, so a restart resumes with
    incident history, persistence counts, and task health intact.
  * A dead-man heartbeat file is written every cycle for external supervision.
  * SIGTERM/SIGINT drain the current cycle and shut down cleanly, so `systemctl
    restart` is not a data-loss event.

Do not run this from an interactive terminal in production. Use systemd — see
SYSTEMD.md.
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from analysis.claude_analyzer import ClaudeAnalyzer, PriorityReport
from collectors import crawler, heartbeat, pagespeed, vision
from collectors.base import CollectorResult
from core.config import Settings, load_settings
from core.incidents import IncidentStore
from core.logging_setup import setup_logging
from core.models import Finding, iso, utcnow
from core.retention import run_retention
from core.scheduler import Scheduler, Task
from core.state import StateStore, atomic_write_json, write_heartbeat
from notifications.google_chat import GoogleChatNotifier

logger = logging.getLogger("thb.runner")

# Upper bound on how long the loop sleeps, so a SIGTERM is never waited out.
MAX_SLEEP_SECONDS = 15.0

# Consecutive failures before a task's own health is escalated to Chat.
TASK_FAILURE_ALERT_THRESHOLD = 3


class OperationsRunner:
    """Wires the agents together and runs them forever."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self.settings.ensure_directories()

        setup_logging(
            self.settings.log_dir,
            level=self.settings.log_level,
            secrets=self.settings.secrets.values(),
        )

        self.state = StateStore.load(self.settings.data_dir / "state.json")
        if self.state.recovered_from_corruption:
            logger.error(
                "state file was unreadable and has been quarantined; "
                "starting from a clean state"
            )

        self.incidents = IncidentStore(self.state, self.settings.incidents)
        self.notifier = GoogleChatNotifier(
            self.settings.secrets.google_chat_webhook_url,
            self.settings.data_dir / "chat_outbox.jsonl",
            timeout=self.settings.limits.http_timeout_seconds,
        )
        self.analyzer = ClaudeAnalyzer(self.settings)
        self.scheduler = Scheduler(self.state)

        self._running = False
        self._shutdown_reason = ""
        self._register_tasks()

    # -- setup ------------------------------------------------------------

    def _register_tasks(self) -> None:
        s = self.settings.schedule

        self.scheduler.add(
            Task(
                name="heartbeat",
                func=self.task_heartbeat,
                interval_seconds=s.heartbeat_interval_seconds,
                # Cheap and critical: never back off far enough to go blind.
                max_backoff_seconds=900,
            )
        )
        self.scheduler.add(
            Task(
                name="vision",
                func=self.task_vision,
                interval_seconds=s.vision_interval_seconds,
                # Expensive: back off hard rather than burning budget on a
                # browser or API that is currently broken.
                max_backoff_seconds=6 * 3600,
                run_on_start=False,
            )
        )
        self.scheduler.add(
            Task(
                name="crawl",
                func=self.task_crawl,
                interval_seconds=s.crawl_interval_seconds,
                max_backoff_seconds=6 * 3600,
                run_on_start=False,
            )
        )
        self.scheduler.add(
            Task(
                name="pagespeed",
                func=self.task_pagespeed,
                interval_seconds=s.pagespeed_interval_seconds,
                max_backoff_seconds=6 * 3600,
                run_on_start=False,
            )
        )
        self.scheduler.add(
            Task(
                name="retention",
                func=self.task_retention,
                interval_seconds=s.retention_interval_seconds,
                max_backoff_seconds=6 * 3600,
            )
        )
        self.scheduler.add(
            Task(
                name="digest",
                func=self.task_digest,
                daily_at=s.digest_at,
                timezone_name=self.settings.timezone,
                max_backoff_seconds=3600,
            )
        )

    # -- shared plumbing --------------------------------------------------

    def _write_report(self, name: str, payload: dict[str, Any]) -> Path:
        stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
        path = self.settings.reports_dir / f"{stamp}_{name}.json"
        atomic_write_json(path, payload)
        return path

    def _process(self, result: CollectorResult, scope: set[str]) -> dict[str, Any]:
        """Reconcile a collector's findings and deliver any resulting alerts.

        `scope` is the set of source agents this run covered, so recovery is
        only inferred for things this run actually looked at.
        """
        if result.status.value != "ok":
            # An unavailable or failed collector must not be read as "all
            # clear" — reconciling here would recover incidents we did not
            # re-check (Rule 7).
            logger.info(
                "collector %s reported %s: %s",
                result.agent,
                result.status.value,
                result.error or result.reason,
            )
            self._write_report(result.agent, result.to_dict())
            return {"status": result.status.value, "alerts": 0}

        alerts = self.incidents.reconcile(result.findings, scope)
        delivery = self.notifier.send_alerts(alerts)
        self.state.bump("alerts_sent", delivery["delivered"])
        self.state.bump("alerts_failed", delivery["failed"])
        self.state.save()

        report_path = self._write_report(
            result.agent,
            {
                **result.to_dict(),
                "alerts": [a.to_dict() for a in alerts],
                "delivery": delivery,
            },
        )

        return {
            "status": "ok",
            "findings": len(result.findings),
            "alerts": len(alerts),
            "delivery": delivery,
            "report": str(report_path),
        }

    # -- tasks ------------------------------------------------------------

    def task_heartbeat(self) -> dict[str, Any]:
        result = heartbeat.run(self.settings)
        return self._process(result, scope={heartbeat.AGENT})

    def task_vision(self) -> dict[str, Any]:
        result = vision.run(
            self.settings,
            analyzer=self.analyzer if self.analyzer.available else None,
        )
        # Vision produces findings under two agent names: deterministic browser
        # findings and AI findings. Both are in scope for recovery.
        return self._process(result, scope={vision.AGENT, "vision_ai"})

    def task_crawl(self) -> dict[str, Any]:
        result = crawler.run(self.settings)
        return self._process(result, scope={crawler.AGENT})

    def task_pagespeed(self) -> dict[str, Any]:
        result = pagespeed.run(self.settings)
        return self._process(result, scope={pagespeed.AGENT})

    def task_retention(self) -> dict[str, Any]:
        report = run_retention(
            self.settings.retention,
            screenshot_dir=self.settings.screenshot_dir,
            reports_dir=self.settings.reports_dir,
            log_dir=self.settings.log_dir,
            data_dir=self.settings.data_dir,
        )
        removed = self.incidents.prune()
        report["pruned_incidents"] = removed
        self.state.save()
        logger.info(
            "retention: freed %.2f MB across %d directories",
            sum(p["freed_mb"] for p in report["pruned"]),
            len(report["pruned"]),
        )
        return report

    def task_digest(self) -> dict[str, Any]:
        """The 7 AM prioritized digest."""
        open_incidents = self.incidents.open_incidents()
        findings: list[Finding] = [i.finding for i in open_incidents]

        priorities = PriorityReport(available=False, error=self.analyzer.unavailable_reason)
        if self.analyzer.available:
            priorities = self.analyzer.prioritize(
                findings,
                context={
                    "site": self.settings.site_name,
                    "open_incident_count": len(open_incidents),
                    "money_pages": [p.url for p in self.settings.money_pages],
                    "task_health": {
                        name: task.to_dict()
                        for name, task in self.state.tasks.items()
                    },
                },
            )

        text = self._format_digest(open_incidents, priorities)
        delivered = self.notifier.send_text(text, kind="digest")

        payload = {
            "generated_at": iso(utcnow()),
            "site": self.settings.site_name,
            "open_incidents": [i.to_dict() for i in open_incidents],
            "priorities": priorities.to_dict(),
            "health": self.health(),
            "delivered": delivered,
        }
        path = self._write_report("digest", payload)
        self.state.save()
        return {"open_incidents": len(open_incidents), "delivered": delivered, "report": str(path)}

    def _format_digest(self, incidents: list[Any], priorities: PriorityReport) -> str:
        ranked = sorted(
            incidents, key=lambda i: i.finding.priority_score(), reverse=True
        )
        lines = [
            f"📋 *Daily Digest — {self.settings.site_name}*",
            f"_{utcnow().strftime('%Y-%m-%d %H:%M UTC')}_",
            "",
            f"*Open incidents:* {len(incidents)}",
        ]

        blocking = [i for i in ranked if i.finding.conversion_blocking]
        if blocking:
            lines.append("")
            lines.append(f"🚨 *CONVERSION BLOCKED ({len(blocking)})*")
            for incident in blocking[:5]:
                f = incident.finding
                lines.append(f"• {f.entity or f.url} — {f.problem}")

        if priorities.available:
            if priorities.headline:
                lines += ["", f"*Assessment:* {priorities.headline}"]
            for title, items in (
                ("CRITICAL NOW", priorities.critical_now),
                ("FIX NEXT", priorities.fix_next),
                ("GROWTH OPPORTUNITIES", priorities.growth_opportunities),
                ("MONITOR", priorities.monitor),
            ):
                if items:
                    lines += ["", f"*{title}*"] + [f"• {i}" for i in items]
            if priorities.single_highest_priority_action:
                lines += [
                    "",
                    f"*Do this first:* {priorities.single_highest_priority_action}",
                ]
        else:
            lines += [
                "",
                f"_AI prioritization unavailable: {priorities.error or 'unknown'}._",
                "_Findings below are ordered by deterministic business-impact score._",
            ]
            for incident in ranked[:10]:
                f = incident.finding
                lines.append(
                    f"• [{f.priority_score():.0f}] {f.entity or f.url} — {f.problem}"
                )

        unhealthy = [
            name for name, task in self.state.tasks.items() if not task.healthy
        ]
        if unhealthy:
            lines += ["", f"⚠️ *Agents currently failing:* {', '.join(sorted(unhealthy))}"]

        outbox = self.notifier.outbox_size()
        if outbox:
            lines += ["", f"⚠️ *{outbox} messages were never delivered to Chat.*"]

        return "\n".join(lines)

    # -- health / lifecycle ----------------------------------------------

    def health(self) -> dict[str, Any]:
        return {
            "site": self.settings.site_name,
            "running": self._running,
            "state": self.state.health_snapshot(),
            "claude_available": self.analyzer.available,
            "claude_reason": self.analyzer.unavailable_reason,
            "chat_configured": self.notifier.configured,
            "chat_outbox": self.notifier.outbox_size(),
            "vision_available": vision.playwright_available(),
            "pagespeed_configured": bool(self.settings.secrets.pagespeed_api_key),
            "wordpress_writes_enabled": self.settings.wordpress_writes_enabled,
            "counters": dict(self.state.counters),
        }

    def _ping_healthcheck(self) -> None:
        """Optional external dead-man ping. Failure is logged, never fatal."""
        url = self.settings.secrets.healthcheck_ping_url
        if not url:
            return
        try:
            requests.get(url, timeout=10)
        except Exception as exc:  # noqa: BLE001
            logger.debug("healthcheck ping failed: %s", type(exc).__name__)

    def _alert_on_task_health(self) -> None:
        """Escalate an agent that has been failing repeatedly.

        Deliberately throttled by the same threshold logic as incidents: one
        message per crossing, not one per cycle.
        """
        for name, task in self.state.tasks.items():
            key = f"task_alerted:{name}"
            if task.consecutive_failures >= TASK_FAILURE_ALERT_THRESHOLD:
                if not self.state.counters.get(key):
                    self.state.counters[key] = True
                    self.notifier.send_text(
                        f"⚠️ *AGENT DEGRADED: {name}*\n"
                        f"{task.consecutive_failures} consecutive failures.\n"
                        f"Last error: {task.last_error}",
                        kind="agent_health",
                    )
            elif task.consecutive_failures == 0 and self.state.counters.get(key):
                self.state.counters[key] = False
                self.notifier.send_text(
                    f"✅ *AGENT RECOVERED: {name}*", kind="agent_health"
                )

    def _install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: Any) -> None:
            self._shutdown_reason = signal.Signals(signum).name
            self._running = False
            logger.info("received %s; finishing cycle and shutting down", self._shutdown_reason)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                logger.debug("could not install handler for %s", sig)

    def run_once(self) -> list[Any]:
        """One scheduler pass. Used by the loop and by `main.py once`."""
        now = datetime.now(timezone.utc)
        outcomes = self.scheduler.run_due(now)
        self._alert_on_task_health()
        self.state.save()
        write_heartbeat(
            self.settings.data_dir / "heartbeat.json",
            {
                "pid": _pid(),
                "cycle_completed_at": iso(utcnow()),
                "tasks_run": [o.name for o in outcomes],
                "failures": [o.name for o in outcomes if not o.ok],
                "open_incidents": len(self.incidents.open_incidents()),
            },
        )
        self._ping_healthcheck()
        return outcomes

    def run_forever(self) -> int:
        """The production loop. Returns a process exit code."""
        self._install_signal_handlers()
        self._running = True
        self.scheduler.prime()

        logger.info(
            "THB AI SEO Operations System starting — site=%s money_pages=%d "
            "claude=%s vision=%s chat=%s wordpress_writes=%s",
            self.settings.site_name,
            len(self.settings.money_pages),
            "available" if self.analyzer.available else "unavailable",
            "available" if vision.playwright_available() else "unavailable",
            "configured" if self.notifier.configured else "unconfigured",
            "ENABLED" if self.settings.wordpress_writes_enabled else "disabled",
        )
        self.state.bump("process_starts")
        self.state.save()

        while self._running:
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - the never-stop boundary
                # Nothing below this line is allowed to end the process.
                logger.exception("unhandled error in main loop; continuing")
                self.state.bump("loop_errors")
                try:
                    self.state.save()
                except Exception:  # noqa: BLE001
                    logger.exception("could not persist state after loop error")
                time.sleep(5.0)
                continue

            sleep_for = min(self.scheduler.seconds_until_next(), MAX_SLEEP_SECONDS)
            if sleep_for > 0 and self._running:
                time.sleep(sleep_for)

        logger.info("shutdown complete (%s)", self._shutdown_reason or "requested")
        self.state.save()
        return 0


def _pid() -> int:
    import os

    return os.getpid()


def main() -> int:  # pragma: no cover - process entry point
    return OperationsRunner().run_forever()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
