"""URL normalization, retention, logging redaction, and config validation."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from core.config import ConfigError, RetentionConfig, load_settings
from core.logging_setup import RedactionFilter
from core.retention import directory_size_bytes, prune_directory, run_retention
from core.urls import is_same_site, normalize_url, same_page


# -- URL normalization (the GSC/GA4 join key) ------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/page/", "https://example.com/page"),
        ("http://example.com/page", "https://example.com/page"),
        ("https://www.example.com/page", "https://example.com/page"),
        ("https://EXAMPLE.com/Page", "https://example.com/Page"),
        ("https://example.com:443/page", "https://example.com/page"),
        ("https://example.com/page#anchor", "https://example.com/page"),
        ("https://example.com/page?utm_source=google", "https://example.com/page"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com/", "https://example.com/"),
        ("example.com/page", "https://example.com/page"),
        ("  https://example.com/page  ", "https://example.com/page"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_path_case_is_preserved() -> None:
    """Hosts are case-insensitive; paths are not. Lowercasing paths would
    merge two genuinely different pages."""
    assert normalize_url("https://example.com/Page") != normalize_url(
        "https://example.com/page"
    )


def test_non_default_port_is_significant() -> None:
    assert normalize_url("https://example.com:8443/p") == "https://example.com:8443/p"


def test_keep_query_drops_only_tracking_params() -> None:
    result = normalize_url(
        "https://example.com/p?city=austin&utm_source=g&gclid=x", keep_query=True
    )
    assert result == "https://example.com/p?city=austin"


@pytest.mark.parametrize("raw", ["", "   ", None, 123])
def test_unusable_input_returns_empty(raw: object) -> None:
    assert normalize_url(raw) == ""  # type: ignore[arg-type]


def test_root_relative_paths_are_preserved() -> None:
    assert normalize_url("/contact/") == "/contact"


def test_same_page_across_source_shapes() -> None:
    assert same_page("http://www.example.com/contact/", "https://example.com/contact")
    assert not same_page("https://example.com/a", "https://example.com/b")


def test_is_same_site_excludes_subdomains() -> None:
    base = "https://example.com"
    assert is_same_site("https://www.example.com/x", base)
    assert not is_same_site("https://blog.example.com/x", base)
    assert not is_same_site("https://other.com/x", base)


# -- retention -------------------------------------------------------------


def test_prune_by_count_removes_oldest(tmp_path: Path) -> None:
    for i in range(5):
        path = tmp_path / f"{i}.png"
        path.write_bytes(b"x" * 10)
        # Distinct mtimes so "oldest" is well-defined.
        import os

        os.utime(path, (time.time() - (100 - i), time.time() - (100 - i)))

    result = prune_directory(tmp_path, max_files=2, patterns=("*.png",))

    assert result.removed_files == 3
    assert result.remaining_files == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["3.png", "4.png"]


def test_prune_by_age(tmp_path: Path) -> None:
    import os

    old = tmp_path / "old.png"
    new = tmp_path / "new.png"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    old_time = time.time() - (40 * 86400)
    os.utime(old, (old_time, old_time))

    result = prune_directory(tmp_path, max_age_days=14, patterns=("*.png",))

    assert result.removed_files == 1
    assert new.exists() and not old.exists()


def test_prune_missing_directory_is_a_noop(tmp_path: Path) -> None:
    result = prune_directory(tmp_path / "nope", max_files=1)
    assert result.removed_files == 0


def test_prune_ignores_unmatched_patterns(tmp_path: Path) -> None:
    (tmp_path / "keep.json").write_text("{}")
    (tmp_path / "drop.png").write_bytes(b"x")
    prune_directory(tmp_path, max_files=0, patterns=("*.png",))
    assert (tmp_path / "keep.json").exists()


def test_run_retention_reports_budget(tmp_path: Path) -> None:
    for name in ("screenshots", "reports", "logs"):
        (tmp_path / name).mkdir()
    (tmp_path / "screenshots" / "a.png").write_bytes(b"x" * 2048)

    report = run_retention(
        RetentionConfig(max_data_dir_mb=0),
        screenshot_dir=tmp_path / "screenshots",
        reports_dir=tmp_path / "reports",
        log_dir=tmp_path / "logs",
        data_dir=tmp_path,
    )

    assert report["over_budget"] is True
    assert len(report["pruned"]) == 3


def test_directory_size(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.bin").write_bytes(b"x" * 500)
    assert directory_size_bytes(tmp_path) == 500


# -- log redaction (Rule 11) ----------------------------------------------


def make_record(message: str) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, message, None, None)


def test_known_secret_is_redacted() -> None:
    filt = RedactionFilter(["super-secret-value-12345"])
    record = make_record("connecting with super-secret-value-12345 now")
    filt.filter(record)
    assert "super-secret-value-12345" not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


def test_credential_shaped_strings_are_redacted_even_if_unknown() -> None:
    filt = RedactionFilter([])
    for message in (
        "key sk-ant-api03-abcdefghijklmnop",
        "Authorization: Bearer abcdefghijklmnopqrstu",
        "posting to https://chat.googleapis.com/v1/spaces/AAA/messages?key=zzz",
        "api_key=hunter2hunter2",
    ):
        record = make_record(message)
        filt.filter(record)
        assert "[REDACTED]" in record.getMessage(), message


def test_ordinary_message_is_untouched() -> None:
    filt = RedactionFilter(["a-secret-value-here"])
    record = make_record("heartbeat ok for https://example.com/contact")
    filt.filter(record)
    assert record.getMessage() == "heartbeat ok for https://example.com/contact"


def test_short_values_are_not_treated_as_secrets() -> None:
    """A short secret would redact common words out of every log line."""
    filt = RedactionFilter(["abc"])
    record = make_record("abc is a normal word here")
    filt.filter(record)
    assert record.getMessage() == "abc is a normal word here"


# -- configuration ---------------------------------------------------------


def test_example_config_is_valid() -> None:
    """The committed example must always load — it is the fallback config."""
    settings = load_settings("config.example.yaml")
    assert settings.pages
    assert settings.money_pages
    assert settings.timezone


def test_missing_base_url_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("site: {}\npages: [{url: 'https://x.test/'}]\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(path)


def test_no_pages_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("site:\n  base_url: https://x.test\npages: []\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(path)


def test_out_of_range_revenue_weight_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "site:\n  base_url: https://x.test\n"
        "pages:\n  - url: https://x.test/\n    revenue_weight: 500\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_settings(path)


def test_malformed_yaml_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("site: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(path)


def test_secrets_never_repr_their_values() -> None:
    from core.config import Secrets

    secrets = Secrets(anthropic_api_key="sk-ant-should-not-appear")
    assert "sk-ant-should-not-appear" not in repr(secrets)
    assert "sk-ant-should-not-appear" not in str(secrets)
    assert "anthropic_api_key" in repr(secrets)
