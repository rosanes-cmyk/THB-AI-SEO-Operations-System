from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import (  # noqa: E402
    IncidentConfig,
    LimitsConfig,
    PageConfig,
    PageExpectations,
    RetentionConfig,
    RevenueConfig,
    ScheduleConfig,
    Secrets,
    Settings,
    Viewport,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """A fully-formed Settings pointed at a temp directory.

    Built explicitly rather than loaded from YAML so tests never depend on the
    committed config, and so no test can accidentally touch the real site.
    """
    data_dir = tmp_path / "data"
    return Settings(
        site_name="Test Site",
        base_url="https://example.test",
        timezone="America/Los_Angeles",
        pages=(
            PageConfig(
                url="https://example.test/",
                name="Homepage",
                money_page=True,
                revenue_weight=95,
                expect=PageExpectations(
                    status=200,
                    form=True,
                    phone_cta=True,
                    text_contains=("cash offer",),
                    min_visible_buttons=1,
                ),
            ),
            PageConfig(
                url="https://example.test/blog/",
                name="Blog",
                money_page=False,
                revenue_weight=5,
                expect=PageExpectations(status=200),
            ),
        ),
        viewports=(
            Viewport("mobile", 390, 844, True),
            Viewport("desktop", 1440, 900, False),
        ),
        schedule=ScheduleConfig(),
        incidents=IncidentConfig(
            escalation_after_seconds=3600,
            min_persistence_to_alert=2,
            min_confidence_to_alert=0.75,
        ),
        retention=RetentionConfig(),
        limits=LimitsConfig(crawl_delay_seconds=0.0),
        revenue=RevenueConfig(),
        secrets=Secrets(),
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        log_dir=tmp_path / "logs",
        screenshot_dir=data_dir / "screenshots",
        claude_model="claude-opus-5",
        claude_effort="high",
        wordpress_writes_enabled=False,
        log_level="WARNING",
    )
