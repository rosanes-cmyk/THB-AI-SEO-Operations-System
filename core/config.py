"""Configuration loading.

Secrets come from the environment (Rule 11). Everything else comes from a
YAML file so a non-engineer can retune cadences and revenue weights without
touching code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

# Fields that must never be written to a log or a Chat message.
SECRET_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "GOOGLE_CHAT_WEBHOOK_URL",
    "PAGESPEED_API_KEY",
    "WORDPRESS_APP_PASSWORD",
    "WORDPRESS_USERNAME",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "THB_HEALTHCHECK_PING_URL",
)


class ConfigError(RuntimeError):
    """Raised when configuration is missing or structurally invalid."""


@dataclass(frozen=True)
class Viewport:
    name: str
    width: int
    height: int
    is_mobile: bool = False


@dataclass(frozen=True)
class PageExpectations:
    """What a healthy render of this page must contain.

    Every field is optional. An expectation that is not configured is not
    checked — the system never invents a requirement it was not given.
    """

    status: int = 200
    form: bool = False
    phone_cta: bool = False
    text_contains: tuple[str, ...] = ()
    min_visible_buttons: int = 0


@dataclass(frozen=True)
class PageConfig:
    url: str
    name: str
    money_page: bool = False
    revenue_weight: int = 0
    expect: PageExpectations = field(default_factory=PageExpectations)


@dataclass(frozen=True)
class ScheduleConfig:
    heartbeat_interval_seconds: int = 300
    vision_interval_seconds: int = 3600
    crawl_interval_seconds: int = 21600
    pagespeed_interval_seconds: int = 21600
    retention_interval_seconds: int = 86400
    digest_at: str = "07:00"


@dataclass(frozen=True)
class IncidentConfig:
    escalation_after_seconds: int = 3600
    min_persistence_to_alert: int = 2
    min_confidence_to_alert: float = 0.75


@dataclass(frozen=True)
class RetentionConfig:
    screenshots_max_files: int = 400
    screenshots_max_age_days: int = 14
    reports_max_files: int = 180
    reports_max_age_days: int = 180
    logs_max_age_days: int = 30
    max_data_dir_mb: int = 2048


@dataclass(frozen=True)
class RevenueConfig:
    """Stage 6 — revenue attribution thresholds.

    `target_roas` is the number the business is actually managing to; every
    channel verdict is measured against it rather than an invented benchmark.
    """

    target_roas: float = 3.0
    window_days: int = 90
    # Below this many deals, a channel's ROAS is one lucky closing away from
    # meaningless, so it is reported without a verdict.
    min_deals_for_roas: int = 5
    # Above this share of unattributed deals, every channel number is suspect
    # and the report says so before it says anything else.
    max_unknown_share: float = 0.30
    # Channels where zero recorded spend is expected, not a missing record.
    organic_channels: tuple[str, ...] = ("seo", "organic", "direct", "referral")
    currency: str = "USD"


@dataclass(frozen=True)
class LimitsConfig:
    http_timeout_seconds: int = 20
    crawl_max_pages: int = 150
    crawl_delay_seconds: float = 1.0
    pagespeed_max_urls: int = 6
    pagespeed_timeout_seconds: int = 90
    vision_max_pages: int = 6
    claude_max_calls_per_hour: int = 60


@dataclass(frozen=True)
class Secrets:
    """Secret material, read from the environment only.

    `__repr__` is overridden so an accidental log of this object cannot leak
    a credential (Rule 11: never print credentials into logs).
    """

    anthropic_api_key: str = ""
    google_chat_webhook_url: str = ""
    pagespeed_api_key: str = ""
    wordpress_base_url: str = ""
    wordpress_username: str = ""
    wordpress_app_password: str = ""
    healthcheck_ping_url: str = ""
    google_credentials_path: str = ""
    search_console_property: str = ""
    ga4_property_id: str = ""

    def __repr__(self) -> str:  # pragma: no cover - trivial
        present = [
            name
            for name, value in (
                ("anthropic_api_key", self.anthropic_api_key),
                ("google_chat_webhook_url", self.google_chat_webhook_url),
                ("pagespeed_api_key", self.pagespeed_api_key),
                ("wordpress_base_url", self.wordpress_base_url),
                ("wordpress_username", self.wordpress_username),
                ("wordpress_app_password", self.wordpress_app_password),
                ("healthcheck_ping_url", self.healthcheck_ping_url),
                ("google_credentials_path", self.google_credentials_path),
                ("search_console_property", self.search_console_property),
                ("ga4_property_id", self.ga4_property_id),
            )
            if value
        ]
        return f"Secrets(configured={present!r})"

    __str__ = __repr__

    def values(self) -> list[str]:
        """Every non-empty secret value, for log redaction."""
        return [
            v
            for v in (
                self.anthropic_api_key,
                self.google_chat_webhook_url,
                self.pagespeed_api_key,
                self.wordpress_app_password,
                self.healthcheck_ping_url,
            )
            if v
        ]


@dataclass(frozen=True)
class Settings:
    site_name: str
    base_url: str
    timezone: str
    pages: tuple[PageConfig, ...]
    viewports: tuple[Viewport, ...]
    schedule: ScheduleConfig
    incidents: IncidentConfig
    retention: RetentionConfig
    limits: LimitsConfig
    revenue: RevenueConfig
    secrets: Secrets
    data_dir: Path
    reports_dir: Path
    log_dir: Path
    screenshot_dir: Path
    claude_model: str
    claude_effort: str
    wordpress_writes_enabled: bool
    log_level: str

    # -- convenience ------------------------------------------------------

    @property
    def money_pages(self) -> tuple[PageConfig, ...]:
        return tuple(p for p in self.pages if p.money_page)

    def page_by_url(self, url: str) -> PageConfig | None:
        for page in self.pages:
            if page.url == url:
                return page
        return None

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.reports_dir,
            self.log_dir,
            self.screenshot_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _as_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_config_path(explicit: str | Path | None) -> Path:
    if explicit is not None:
        path = Path(explicit)
        if not path.is_absolute():
            path = REPO_ROOT / path
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        return path

    candidate = Path(os.getenv("THB_CONFIG_PATH", "config.yaml"))
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    if candidate.exists():
        return candidate

    # A fresh clone has no config.yaml (it is gitignored). Fall back to the
    # committed example so the system starts and reports rather than crashes.
    example = REPO_ROOT / "config.example.yaml"
    if example.exists():
        return example
    raise ConfigError(
        "no configuration found; copy config.example.yaml to config.yaml"
    )


def _parse_page(raw: dict[str, Any], index: int) -> PageConfig:
    url = raw.get("url")
    if not url:
        raise ConfigError(f"pages[{index}] is missing a url")
    expect_raw = raw.get("expect") or {}
    if not isinstance(expect_raw, dict):
        raise ConfigError(f"pages[{index}].expect must be a mapping")

    weight = int(raw.get("revenue_weight", 0))
    if not 0 <= weight <= 100:
        raise ConfigError(
            f"pages[{index}].revenue_weight must be between 0 and 100, got {weight}"
        )

    text_contains = expect_raw.get("text_contains") or []
    if isinstance(text_contains, str):
        text_contains = [text_contains]

    return PageConfig(
        url=str(url),
        name=str(raw.get("name") or url),
        money_page=bool(raw.get("money_page", False)),
        revenue_weight=weight,
        expect=PageExpectations(
            status=int(expect_raw.get("status", 200)),
            form=bool(expect_raw.get("form", False)),
            phone_cta=bool(expect_raw.get("phone_cta", False)),
            text_contains=tuple(str(t) for t in text_contains),
            min_visible_buttons=int(expect_raw.get("min_visible_buttons", 0)),
        ),
    )


def _dir_from_env(env_key: str, default: str) -> Path:
    path = Path(os.getenv(env_key) or default)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Load settings from YAML + environment.

    Raises ConfigError on structurally invalid configuration. Missing secrets
    are NOT an error here — each subsystem degrades to "unavailable" on its
    own so one missing credential cannot stop the whole service (Rule 8).
    """
    load_dotenv(REPO_ROOT / ".env", override=False)

    path = _resolve_config_path(config_path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    site = raw.get("site") or {}
    base_url = site.get("base_url")
    if not base_url:
        raise ConfigError("site.base_url is required")

    pages_raw = raw.get("pages") or []
    if not isinstance(pages_raw, list) or not pages_raw:
        raise ConfigError("at least one entry under `pages` is required")
    pages = tuple(_parse_page(p, i) for i, p in enumerate(pages_raw))

    viewports_raw = raw.get("viewports") or []
    viewports = tuple(
        Viewport(
            name=str(v.get("name") or f"viewport{i}"),
            width=int(v.get("width", 1440)),
            height=int(v.get("height", 900)),
            is_mobile=bool(v.get("is_mobile", False)),
        )
        for i, v in enumerate(viewports_raw)
    ) or (
        Viewport("mobile", 390, 844, True),
        Viewport("desktop", 1440, 900, False),
    )

    def section(name: str) -> dict[str, Any]:
        value = raw.get(name) or {}
        if not isinstance(value, dict):
            raise ConfigError(f"`{name}` must be a mapping")
        return value

    data_dir = _dir_from_env("THB_DATA_DIR", "data")

    revenue_raw = dict(section("revenue"))
    organic = revenue_raw.pop("organic_channels", None)
    revenue = RevenueConfig(
        organic_channels=tuple(str(c) for c in (organic or RevenueConfig.organic_channels)),
        **revenue_raw,
    )
    if revenue.target_roas <= 0:
        raise ConfigError("revenue.target_roas must be greater than 0")

    return Settings(
        site_name=str(site.get("name") or base_url),
        base_url=str(base_url).rstrip("/") or str(base_url),
        timezone=str(site.get("timezone") or "America/Los_Angeles"),
        pages=pages,
        viewports=viewports,
        schedule=ScheduleConfig(**section("schedule")),
        incidents=IncidentConfig(**section("incidents")),
        retention=RetentionConfig(**section("retention")),
        limits=LimitsConfig(**section("limits")),
        revenue=revenue,
        secrets=Secrets(
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            google_chat_webhook_url=os.getenv("GOOGLE_CHAT_WEBHOOK_URL", ""),
            pagespeed_api_key=os.getenv("PAGESPEED_API_KEY", ""),
            wordpress_base_url=os.getenv("WORDPRESS_BASE_URL", ""),
            wordpress_username=os.getenv("WORDPRESS_USERNAME", ""),
            wordpress_app_password=os.getenv("WORDPRESS_APP_PASSWORD", ""),
            healthcheck_ping_url=os.getenv("THB_HEALTHCHECK_PING_URL", ""),
            google_credentials_path=os.getenv("GOOGLE_APPLICATION_CREDENTIALS", ""),
            search_console_property=os.getenv("THB_SEARCH_CONSOLE_PROPERTY", ""),
            ga4_property_id=os.getenv("THB_GA4_PROPERTY_ID", ""),
        ),
        data_dir=data_dir,
        reports_dir=_dir_from_env("THB_REPORTS_DIR", "reports"),
        log_dir=_dir_from_env("THB_LOG_DIR", "logs"),
        screenshot_dir=data_dir / "screenshots",
        claude_model=os.getenv("THB_CLAUDE_MODEL") or "claude-opus-5",
        claude_effort=os.getenv("THB_CLAUDE_EFFORT") or "high",
        wordpress_writes_enabled=_as_bool(
            os.getenv("THB_WORDPRESS_WRITES_ENABLED"), False
        ),
        log_level=(os.getenv("THB_LOG_LEVEL") or "INFO").upper(),
    )
