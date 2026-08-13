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

from analysis.claude_analyzer import ClaudeAnalyzer
from analysis.priority_engine import Bucket, Coverage, PriorityBoard, build_board, narrate
from collectors import crawler, ga4, heartbeat, pagespeed, revenue, search_console, vision
from collectors.base import CollectorResult
from core.config import Settings, load_settings
from core.incidents import IncidentStore
from core.logging_setup import setup_logging
from core.models import CollectorStatus, Finding, iso, utcnow
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
                name="search_console",
                func=self.task_search_console,
                interval_seconds=s.search_console_interval_seconds,
                max_backoff_seconds=12 * 3600,
                run_on_start=False,
            )
        )
        self.scheduler.add(
            Task(
                name="ga4",
                func=self.task_ga4,
                interval_seconds=s.ga4_interval_seconds,
                max_backoff_seconds=12 * 3600,
                run_on_start=False,
            )
        )
        self.scheduler.add(
            Task(
                name="revenue",
                func=self.task_revenue,
                interval_seconds=s.revenue_interval_seconds,
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

    def _record_status(self, result: CollectorResult) -> None:
        """Remember what each collector last reported.

        Stage 7 builds its coverage from this, so the digest can state which
        agents did not report rather than presenting a partial board as a
        complete one. Persisted in state so a restart does not silently
        promote every collector to "never had a problem".
        """
        statuses = self.state.counters.setdefault("collector_status", {})
        if not isinstance(statuses, dict):  # a hand-edited state file
            statuses = {}
            self.state.counters["collector_status"] = statuses
        statuses[result.agent] = {
            "status": result.status.value,
            "detail": result.error or result.reason,
            "at": iso(utcnow()),
        }

    def _coverage(self) -> Coverage:
        coverage = Coverage()
        statuses = self.state.counters.get("collector_status") or {}
        if not isinstance(statuses, dict):
            return coverage
        for agent, payload in statuses.items():
            if not isinstance(payload, dict):
                continue
            status = str(payload.get("status", ""))
            detail = str(payload.get("detail") or "no detail recorded")
            if status == CollectorStatus.OK.value:
                coverage.ran.append(agent)
            elif status == CollectorStatus.UNAVAILABLE.value:
                coverage.unavailable[agent] = detail
            elif status == CollectorStatus.ERROR.value:
                coverage.failed[agent] = detail
        return coverage

    def _process(self, result: CollectorResult, scope: set[str]) -> dict[str, Any]:
        """Reconcile a collector's findings and deliver any resulting alerts.

        `scope` is the set of source agents this run covered, so recovery is
        only inferred for things this run actually looked at.
        """
        self._record_status(result)

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

    def task_search_console(self) -> dict[str, Any]:
        result = search_console.run(self.settings)
        return self._process(result, scope={search_console.AGENT})

    def task_ga4(self) -> dict[str, Any]:
        result = ga4.run(self.settings)
        return self._process(result, scope={ga4.AGENT})

    def task_revenue(self) -> dict[str, Any]:
        result = revenue.run(self.settings)
        return self._process(result, scope={revenue.AGENT})

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

    def build_board(self) -> PriorityBoard:
        """The Stage 7 board over every currently-open incident.

        Persistence counts come from the incident store, so an issue that has
        survived several cycles outranks one seen once — the same logic Rule 9
        applies to alerting, applied to ranking.
        """
        open_incidents = self.incidents.open_incidents()
        findings: list[Finding] = [i.finding for i in open_incidents]
        persistence = {i.fingerprint: i.persistence_count for i in open_incidents}
        board = build_board(findings, self._coverage(), persistence=persistence)
        narrate(
            board,
            self.analyzer,
            {
                "site": self.settings.site_name,
                "open_incident_count": len(open_incidents),
                "money_pages": [p.url for p in self.settings.money_pages],
                "task_health": {
                    name: task.to_dict() for name, task in self.state.tasks.items()
                },
            },
        )
        return board

    def task_digest(self) -> dict[str, Any]:
        """The 7 AM prioritized digest."""
        open_incidents = self.incidents.open_incidents()
        board = self.build_board()

        text = self._format_digest(open_incidents, board)
        delivered = self.notifier.send_text(text, kind="digest")

        payload = {
            "generated_at": iso(utcnow()),
            "site": self.settings.site_name,
            "open_incidents": [i.to_dict() for i in open_incidents],
            "board": board.to_dict(),
            "health": self.health(),
            "delivered": delivered,
        }
        path = self._write_report("digest", payload)
        self.state.save()
        return {
            "open_incidents": len(open_incidents),
            "critical_now": len(board.critical_now),
            "coverage_complete": board.coverage.complete,
            "delivered": delivered,
            "report": str(path),
        }

    def _format_digest(self, incidents: list[Any], board: PriorityBoard) -> str:
        lines = [
            f"📋 *Daily Digest — {self.settings.site_name}*",
            f"_{utcnow().strftime('%Y-%m-%d %H:%M UTC')}_",
            "",
            board.headline(),
            "",
            f"*Open incidents:* {len(incidents)}",
        ]

        # Blind spots go near the top. A reader who does not know an agent was
        # down will read this digest as a complete picture.
        if board.coverage.blind_spots:
            lines += ["", "⚠️ *Not measured this cycle*"]
            lines += [f"• {spot}" for spot in board.coverage.blind_spots[:6]]

        icons = {
            Bucket.CRITICAL_NOW: "🚨",
            Bucket.FIX_NEXT: "🔧",
            Bucket.GROWTH: "📈",
            Bucket.MONITOR: "👀",
        }
        for bucket in (Bucket.CRITICAL_NOW, Bucket.FIX_NEXT, Bucket.GROWTH, Bucket.MONITOR):
            items = board.bucket(bucket)
            if not items:
                continue
            lines += ["", f"{icons[bucket]} *{bucket.label} ({len(items)})*"]
            for item in items[:5]:
                f = item.finding
                line = f"• [{item.score:.0f}] {f.entity or f.url} — {f.problem}"
                if item.is_corroborated:
                    line += f" _(also seen by {', '.join(item.corroborating_agents)})_"
                lines.append(line)
            for note in items[0].correlation_notes[:1]:
                lines.append(f"  ↳ _{note}_")

        no_action = len(board.bucket(Bucket.NO_ACTION))
        if no_action:
            lines += ["", f"_{no_action} further finding(s) need no action._"]

        if board.top_action:
            lines += ["", f"*Do this first:* {board.top_action.finding.recommended_action or board.top_action.finding.problem}"]

        narrative = board.narrative or {}
        if narrative.get("available") and narrative.get("headline"):
            lines += ["", f"_Assessment: {narrative['headline']}_"]
        elif not narrative.get("available"):
            lines += [
                "",
                f"_AI narration unavailable: {narrative.get('reason') or 'unknown'}. "
                "Ranking above is deterministic and unaffected._",
            ]

        unhealthy = [
            name for name, task in self.state.tasks.items() if not task.healthy
        ]
        if unhealthy:
            lines += ["", f"⚠️ *Agents currently failing:* {', '.join(sorted(unhealthy))}"]

        outbox = self.notifier.outbox_size()
        if outbox:
            plural = "message" if outbox == 1 else "messages"
            lines += ["", f"⚠️ *{outbox} {plural} never delivered to Chat.*"]

        return "\n".join(lines)

    # -- health / lifecycle ----------------------------------------------

    def health(self) -> dict[str, Any]:
        return {
            "site": self.settings.site_name,
            "running": self._running,
            "state": self.state.health_snapshot(),
            # `--no-ai` and tests replace the analyzer with None; health must
            # report that state rather than crash on it.
            "claude_available": bool(getattr(self.analyzer, "available", False)),
            "claude_reason": getattr(
                self.analyzer, "unavailable_reason", "no analyzer configured"
            ),
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
