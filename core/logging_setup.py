"""Logging with credential redaction.

Rule 11: never print credentials into logs. Redaction is enforced by a filter
on the root logger rather than by discipline at call sites, because discipline
at call sites is how credentials end up in logs.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path
from typing import Iterable

# Patterns that look like credentials even when we don't hold the value —
# covers anything a third-party library might echo back at us.
_GENERIC_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"https://chat\.googleapis\.com/\S+"),
    re.compile(r"(?i)(api[_-]?key|password|token|secret)\s*[=:]\s*\S+"),
)

REDACTED = "[REDACTED]"


class RedactionFilter(logging.Filter):
    """Scrubs known secret values and credential-shaped strings."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        # Longest first, so a webhook URL is scrubbed before its host substring.
        self._secrets = sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True)

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        for pattern in _GENERIC_PATTERNS:
            text = pattern.sub(REDACTED, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        scrubbed = self._scrub(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    secrets: Iterable[str] = (),
    filename: str = "thb.log",
) -> logging.Logger:
    """Configure root logging: rotating file + stdout, both redacted."""
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    redaction = RedactionFilter(secrets)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)-24s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / filename, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redaction)
    root.addHandler(file_handler)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    stream.addFilter(redaction)
    root.addHandler(stream)

    # Third-party chatter at INFO drowns our own signal.
    for noisy in ("urllib3", "httpx", "httpcore", "anthropic", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("thb")
