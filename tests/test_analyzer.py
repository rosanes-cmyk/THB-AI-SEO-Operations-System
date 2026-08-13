"""Rule 7: Claude cannot silently guess, and a bad response cannot crash us."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from analysis.claude_analyzer import (
    ClaudeAnalyzer,
    RateLimiter,
    _validate_vision_payload,
)
from core.config import PageConfig, PageExpectations, Settings


class FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, text: str = "", stop_reason: str = "end_turn") -> None:
        self.content = [FakeBlock(text)] if text else []
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, response: Any = None, raises: Exception | None = None) -> None:
        self._response = response
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        return self._response


class FakeClient:
    def __init__(self, response: Any = None, raises: Exception | None = None) -> None:
        self.messages = FakeMessages(response, raises)


class FakeEvidence:
    def __init__(self, screenshot: str) -> None:
        self.screenshot_path = screenshot
        self.viewport = "mobile"
        self.viewport_width = 390
        self.viewport_height = 844

    def to_dict(self) -> dict[str, Any]:
        return {"viewport": self.viewport, "http_status": 200}


def money_page() -> PageConfig:
    return PageConfig(
        url="https://example.test/",
        name="Homepage",
        money_page=True,
        revenue_weight=95,
        expect=PageExpectations(form=True, phone_cta=True, min_visible_buttons=1),
    )


def a_screenshot(tmp_path: Path) -> Path:
    # A 1x1 PNG is enough — the fake client never looks at it.
    path = tmp_path / "shot.png"
    path.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000a49444154789c6300010000050001"
            "0d0a2db40000000049454e44ae426082"
        )
    )
    return path


# -- payload validation ----------------------------------------------------


def test_valid_payload_accepted() -> None:
    payload, error = _validate_vision_payload(
        {
            "verdict": "critical",
            "confidence": 0.9,
            "conversion_blocking": True,
            "summary": "Form is covered.",
            "findings": [
                {
                    "type": "conversion_blocking",
                    "severity": "critical",
                    "description": "Cookie banner covers the submit button",
                    "business_impact": "No leads can be submitted",
                    "visual_evidence": "Banner overlaps the CTA",
                    "recommended_verification": "Re-render at 390x844",
                }
            ],
        }
    )
    assert error == ""
    assert payload is not None and len(payload["findings"]) == 1


def test_bad_verdict_rejected() -> None:
    payload, error = _validate_vision_payload({"verdict": "catastrophic"})
    assert payload is None and "invalid verdict" in error


def test_non_object_rejected() -> None:
    payload, error = _validate_vision_payload(["not", "an", "object"])
    assert payload is None and error


def test_non_numeric_confidence_rejected() -> None:
    payload, error = _validate_vision_payload(
        {"verdict": "healthy", "confidence": "very", "findings": []}
    )
    assert payload is None and "confidence" in error


def test_out_of_range_confidence_is_clamped() -> None:
    payload, _ = _validate_vision_payload(
        {"verdict": "healthy", "confidence": 7.5, "findings": []}
    )
    assert payload is not None and payload["confidence"] == 1.0


def test_findings_must_be_a_list() -> None:
    payload, error = _validate_vision_payload(
        {"verdict": "warning", "confidence": 0.8, "findings": "none"}
    )
    assert payload is None and "findings" in error


def test_garbage_findings_are_dropped_not_fatal() -> None:
    payload, error = _validate_vision_payload(
        {
            "verdict": "warning",
            "confidence": 0.8,
            "findings": ["a string", {"no": "description"}, None],
        }
    )
    assert error == ""
    assert payload is not None and payload["findings"] == []


# -- analyzer behavior -----------------------------------------------------


def test_missing_api_key_is_unavailable_not_fatal(settings: Settings) -> None:
    analyzer = ClaudeAnalyzer(settings)
    assert analyzer.available is False
    assert "ANTHROPIC_API_KEY" in analyzer.unavailable_reason


def test_malformed_json_yields_unknown_verdict(
    settings: Settings, tmp_path: Path
) -> None:
    analyzer = ClaudeAnalyzer(settings, client=FakeClient(FakeResponse("not json {{")))
    verdict = analyzer.inspect_screenshot(money_page(), FakeEvidence(str(a_screenshot(tmp_path))))

    assert verdict is not None
    assert verdict.verdict == "unknown"
    assert verdict.findings == []
    assert "JSON" in verdict.error


def test_api_exception_yields_unknown_verdict(
    settings: Settings, tmp_path: Path
) -> None:
    analyzer = ClaudeAnalyzer(
        settings, client=FakeClient(raises=ConnectionError("injected outage"))
    )
    verdict = analyzer.inspect_screenshot(money_page(), FakeEvidence(str(a_screenshot(tmp_path))))

    assert verdict is not None and verdict.verdict == "unknown"
    assert "ConnectionError" in verdict.error


def test_refusal_is_handled(settings: Settings, tmp_path: Path) -> None:
    analyzer = ClaudeAnalyzer(
        settings, client=FakeClient(FakeResponse("{}", stop_reason="refusal"))
    )
    verdict = analyzer.inspect_screenshot(money_page(), FakeEvidence(str(a_screenshot(tmp_path))))
    assert verdict is not None and verdict.verdict == "unknown"
    assert "declined" in verdict.error


def test_healthy_verdict_produces_no_findings(
    settings: Settings, tmp_path: Path
) -> None:
    body = json.dumps(
        {
            "verdict": "healthy",
            "confidence": 0.95,
            "conversion_blocking": False,
            "summary": "Looks fine.",
            "findings": [],
        }
    )
    analyzer = ClaudeAnalyzer(settings, client=FakeClient(FakeResponse(body)))
    verdict = analyzer.inspect_screenshot(money_page(), FakeEvidence(str(a_screenshot(tmp_path))))

    assert verdict is not None and verdict.verdict == "healthy"
    assert verdict.findings == []
    assert verdict.usable is True


def test_critical_verdict_becomes_weighted_findings(
    settings: Settings, tmp_path: Path
) -> None:
    body = json.dumps(
        {
            "verdict": "critical",
            "confidence": 0.93,
            "conversion_blocking": True,
            "summary": "The form is covered by a modal.",
            "findings": [
                {
                    "type": "conversion_blocking",
                    "severity": "critical",
                    "description": "A modal covers the seller form",
                    "business_impact": "No seller can submit a lead",
                    "visual_evidence": "Modal spans the form area",
                    "recommended_verification": "Re-render at mobile viewport",
                }
            ],
        }
    )
    analyzer = ClaudeAnalyzer(settings, client=FakeClient(FakeResponse(body)))
    page = money_page()
    verdict = analyzer.inspect_screenshot(page, FakeEvidence(str(a_screenshot(tmp_path))))

    assert verdict is not None
    assert len(verdict.findings) == 1
    finding = verdict.findings[0]
    assert finding.revenue_weight == page.revenue_weight
    assert finding.conversion_blocking is True
    assert finding.viewport == "mobile"
    assert finding.source_agent == "vision_ai"


def test_missing_screenshot_returns_none(settings: Settings) -> None:
    analyzer = ClaudeAnalyzer(settings, client=FakeClient(FakeResponse("{}")))
    assert analyzer.inspect_screenshot(money_page(), FakeEvidence("/nope/missing.png")) is None


def test_uses_structured_output_and_configured_model(
    settings: Settings, tmp_path: Path
) -> None:
    client = FakeClient(FakeResponse('{"verdict":"healthy","confidence":1,"conversion_blocking":false,"summary":"","findings":[]}'))
    analyzer = ClaudeAnalyzer(settings, client=client)
    analyzer.inspect_screenshot(money_page(), FakeEvidence(str(a_screenshot(tmp_path))))

    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == "high"


def test_prioritize_degrades_when_claude_fails(settings: Settings) -> None:
    from core.models import Finding

    analyzer = ClaudeAnalyzer(settings, client=FakeClient(raises=TimeoutError("slow")))
    report = analyzer.prioritize([Finding(source_agent="x", problem="y")])

    assert report.available is False
    assert report.critical_now == []
    assert "TimeoutError" in report.error


def test_prioritize_with_no_findings_is_trivially_available(settings: Settings) -> None:
    analyzer = ClaudeAnalyzer(settings, client=FakeClient(FakeResponse("{}")))
    report = analyzer.prioritize([])
    assert report.available is True


# -- cost control ----------------------------------------------------------


def test_rate_limiter_blocks_past_budget() -> None:
    limiter = RateLimiter(3)
    assert [limiter.allow() for _ in range(5)] == [True, True, True, False, False]


def test_analyzer_respects_hourly_budget(settings: Settings, tmp_path: Path) -> None:
    from dataclasses import replace

    from core.config import LimitsConfig

    tight = replace(settings, limits=LimitsConfig(claude_max_calls_per_hour=1))
    body = '{"verdict":"healthy","confidence":1,"conversion_blocking":false,"summary":"","findings":[]}'
    client = FakeClient(FakeResponse(body))
    analyzer = ClaudeAnalyzer(tight, client=client)
    shot = FakeEvidence(str(a_screenshot(tmp_path)))

    first = analyzer.inspect_screenshot(money_page(), shot)
    second = analyzer.inspect_screenshot(money_page(), shot)

    assert first is not None and first.verdict == "healthy"
    assert second is not None and second.verdict == "unknown"
    assert "budget" in second.error
    assert len(client.messages.calls) == 1, "over-budget call must not reach the API"
