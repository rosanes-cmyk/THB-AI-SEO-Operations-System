"""Disk retention.

An always-on process that writes screenshots every hour fills a disk. This
enforces both a file-count cap and an age cap on every artifact directory, and
reports total data-directory size so the operator can see the trend before it
becomes an outage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from core.config import RetentionConfig
from core.models import utcnow

logger = logging.getLogger(__name__)


@dataclass
class PruneResult:
    directory: str
    removed_files: int = 0
    freed_bytes: int = 0
    remaining_files: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "directory": self.directory,
            "removed_files": self.removed_files,
            "freed_bytes": self.freed_bytes,
            "freed_mb": round(self.freed_bytes / (1024 * 1024), 2),
            "remaining_files": self.remaining_files,
        }


def _candidates(directory: Path, patterns: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        files.extend(p for p in directory.glob(pattern) if p.is_file())
    # Oldest first — deletion always removes the oldest artifacts.
    return sorted(files, key=lambda p: p.stat().st_mtime)


def prune_directory(
    directory: Path,
    *,
    max_files: int | None = None,
    max_age_days: int | None = None,
    patterns: tuple[str, ...] = ("*",),
) -> PruneResult:
    """Delete by age first, then by count. Missing directory is a no-op."""
    result = PruneResult(directory=str(directory))
    if not directory.exists():
        return result

    files = _candidates(directory, patterns)

    if max_age_days is not None and max_age_days > 0:
        cutoff = (utcnow() - timedelta(days=max_age_days)).timestamp()
        survivors: list[Path] = []
        for path in files:
            try:
                if path.stat().st_mtime < cutoff:
                    size = path.stat().st_size
                    path.unlink()
                    result.removed_files += 1
                    result.freed_bytes += size
                else:
                    survivors.append(path)
            except OSError as exc:
                logger.warning("could not prune %s: %s", path, exc)
                survivors.append(path)
        files = survivors

    if max_files is not None and max_files >= 0 and len(files) > max_files:
        excess = len(files) - max_files
        for path in files[:excess]:
            try:
                size = path.stat().st_size
                path.unlink()
                result.removed_files += 1
                result.freed_bytes += size
            except OSError as exc:
                logger.warning("could not prune %s: %s", path, exc)
        files = files[excess:]

    result.remaining_files = len(files)
    return result


def directory_size_bytes(directory: Path) -> int:
    if not directory.exists():
        return 0
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def run_retention(
    config: RetentionConfig,
    *,
    screenshot_dir: Path,
    reports_dir: Path,
    log_dir: Path,
    data_dir: Path,
) -> dict[str, Any]:
    """Apply every retention policy. Returns a report for the digest."""
    results = [
        prune_directory(
            screenshot_dir,
            max_files=config.screenshots_max_files,
            max_age_days=config.screenshots_max_age_days,
            patterns=("*.png", "*.jpg", "*.jpeg", "*.webp"),
        ),
        prune_directory(
            reports_dir,
            max_files=config.reports_max_files,
            max_age_days=config.reports_max_age_days,
            patterns=("*.json", "*.md", "*.html"),
        ),
        # Rotation handles the active log; this sweeps the rotated backups.
        prune_directory(
            log_dir,
            max_files=None,
            max_age_days=config.logs_max_age_days,
            patterns=("*.log.*", "*.log"),
        ),
    ]

    size_bytes = directory_size_bytes(data_dir)
    size_mb = round(size_bytes / (1024 * 1024), 2)
    # Compare exact bytes, not the rounded MB figure — rounding must not be
    # what decides whether a budget was breached.
    over_budget = size_bytes > config.max_data_dir_mb * 1024 * 1024
    if over_budget:
        logger.warning(
            "data directory is %.2f MB, over the %d MB budget",
            size_mb,
            config.max_data_dir_mb,
        )

    return {
        "pruned": [r.to_dict() for r in results],
        "data_dir_mb": size_mb,
        "data_dir_budget_mb": config.max_data_dir_mb,
        "over_budget": over_budget,
    }
