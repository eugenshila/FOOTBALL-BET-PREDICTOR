"""Tests for the Google-Search-grounded provider.

The sandbox cannot reach Google, so these drive the real client through an
``httpx.MockTransport``: the request we build, the parsing of Gemini's grounding
metadata, and every failure path are all exercised for real.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.models import Confidence
from app.vision.grounded import GeminiGroundedProvider, GroundingUnavailable, _first_json_object
from app.vision.prompt import GROUNDED_SYSTEM_PROMPT

PART_JSON = {
    "component_name": "PCV Valve",
    "category": "Engine",
    "oem_part_number": "5184894AA",
    "alternate_numbers": ["68083202AA"],
    "superseded_numbers": ["5184894AB"],
    "fitment": [
        {"make": "Jeep", "model": "Grand Cherokee", "engine": "3.6L Pentastar V6", "years": "2011-2021"}
    ],
    "technical_notes": ["Lubricate the O-ring before seating."],
    "visual_evidence": ["Black moulded elbow with hose barb."],
    "confidence": "high",
    "number_confidence": "high",
    "notes": "",
}


def _gemini_response(text: str, *, sources=(), queries=()) -> dict:
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": text}]},
                "groundingMetadata": {
                    "webSearchQueries": list(queries),
                    "groundingChunks": [
                        {"web": {"uri": uri, "title": title}} for title, uri in sources
                    ],
                },
            }
        ]
    }


def _provider(handler) -> GeminiGroundedProvider:
    return GeminiGroundedProvider(
        api_key="test-key",
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(handler),
    )


# ── request shape ─────────────────────────────────────────────────────────
def test_request_asks_for_search_grounding():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=_gemini_response(json.dumps(PART_JSON)))

    report = _provider(handler).identify(b"\xff\xd8fakejpeg", "image/jpeg")

    assert "gemini-2.5-flash:generateContent" in captured["url"]
    assert captured["headers"]["x-goog-api-key"] == "test-key"
    body = captured["body"]
    assert body["tools"] == [{"google_search": {}}]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["system_instruction"]["parts"][0]["text"] == GROUNDED_SYSTEM_PROMPT
    assert body["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "image/jpeg"
    assert report.part_name == "PCV Valve"


def test_grounded_prompt_forbids_guessing_numbers():
    lowered = " ".join(GROUNDED_SYSTEM_PROMPT.lower().split())
    assert "google search tool" in lowered
    assert "never fill a number from memory" in lowered
    assert "leave oem_part_number empty" in lowered


# ── parsing ───────────────────────────────────────────────────────────────
def test_sources_and_queries_are_captured():
    def handler(_request):
        return httpx.Response(
            200,
            json=_gemini_response(
                json.dumps(PART_JSON),
                sources=[
                    ("5184894AA PCV Valve | Mopar Parts", "https://www.moparparts.com/p/5184894aa"),
                    ("PCV valve cross reference", "https://www.rockauto.com/en/partsearch/?partnum=5184894AA"),
                ],
                queries=["5184894AA PCV valve OEM number", "pentastar 3.6 pcv valve part number"],
            ),
        )

    report = _provider(handler).identify(b"img")

    assert report.grounded is True
    assert len(report.sources) == 2
    assert report.sources[0].url.startswith("https://www.moparparts.com")
    assert report.search_queries == [
        "5184894AA PCV valve OEM number",
        "pentastar 3.6 pcv valve part number",
    ]
    assert any("Google Search grounding" in e for e in report.visual_evidence)
    assert any("cited pages" in w or "EPC" in w for w in report.warnings)


def test_duplicate_sources_are_deduplicated():
    def handler(_request):
        return httpx.Response(
            200,
            json=_gemini_response(
                json.dumps(PART_JSON),
                sources=[("A", "https://a.example/x"), ("A again", "https://a.example/x")],
            ),
        )

    report = _provider(handler).identify(b"img")
    assert len(report.sources) == 1


def test_ungrounded_response_is_flagged():
    def handler(_request):
        payload = _gemini_response(json.dumps(PART_JSON))
        payload["candidates"][0]["groundingMetadata"] = {}
        return httpx.Response(200, json=payload)

    report = _provider(handler).identify(b"img")
    assert report.sources == []
    assert any("no sources" in w.lower() for w in report.warnings)


def test_fenced_json_is_parsed():
    def handler(_request):
        text = f"Here you go:\n```json\n{json.dumps(PART_JSON)}\n```"
        return httpx.Response(200, json=_gemini_response(text))

    report = _provider(handler).identify(b"img")
    assert report.oem_part_number == "5184894AA"


def test_extra_text_around_json_is_tolerated():
    def handler(_request):
        text = "Based on the search results: " + json.dumps(PART_JSON) + " Hope this helps."
        return httpx.Response(200, json=_gemini_response(text))

    report = _provider(handler).identify(b"img")
    assert report.part_name == "PCV Valve"


def test_braces_inside_strings_do_not_break_extraction():
    payload = dict(PART_JSON, technical_notes=["Use {OEM} spec only", "Torque 25 Nm"])
    text = "note " + json.dumps(payload)
    parsed = json.loads(_first_json_object(text))
    assert parsed["technical_notes"] == ["Use {OEM} spec only", "Torque 25 Nm"]


def test_missing_number_is_reported_not_invented():
    payload = dict(PART_JSON, oem_part_number="", number_confidence="low")

    def handler(_request):
        return httpx.Response(200, json=_gemini_response(json.dumps(payload)))

    report = _provider(handler).identify(b"img")
    assert report.oem_part_number == ""
    assert report.number_confidence is Confidence.low
    assert report.needs_manual_review is True
    assert any("No OEM number" in w for w in report.warnings)


# ── resilience ────────────────────────────────────────────────────────────
def test_json_mode_rejection_is_retried_without_it():
    calls: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["generationConfig"].get("responseMimeType") == "application/json":
            return httpx.Response(400, json={"error": {"message": "responseMimeType unsupported with tools"}})
        return httpx.Response(200, json=_gemini_response(json.dumps(PART_JSON)))

    report = _provider(handler).identify(b"img")
    assert len(calls) == 2
    assert "responseMimeType" not in calls[1]["generationConfig"]
    assert report.part_name == "PCV Valve"


def test_persistent_400_raises_grounding_unavailable():
    def handler(_request):
        return httpx.Response(400, json={"error": {"message": "search not available in your region"}})

    with pytest.raises(GroundingUnavailable):
        _provider(handler).identify(b"img")


def test_safety_block_without_candidates_raises():
    def handler(_request):
        return httpx.Response(200, json={"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})

    with pytest.raises(GroundingUnavailable):
        _provider(handler).identify(b"img")


def test_empty_text_raises():
    def handler(_request):
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": ""}]}}]})

    with pytest.raises(GroundingUnavailable):
        _provider(handler).identify(b"img")


def test_http_error_propagates():
    def handler(_request):
        return httpx.Response(429, json={"error": {"message": "quota"}})

    with pytest.raises(httpx.HTTPStatusError):
        _provider(handler).identify(b"img")


# ── provider registry ─────────────────────────────────────────────────────
def test_grounded_provider_is_preferred_when_gemini_key_present(monkeypatch):
    from app import vision
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "redacted-test-key")
    monkeypatch.setattr(settings, "grounding_enabled", True)
    monkeypatch.setattr(settings, "vision_provider", "auto")
    assert vision.resolve_provider_name() == "gemini-grounded"

    monkeypatch.setattr(settings, "grounding_enabled", False)
    assert vision.resolve_provider_name() == "gemini"


def test_grounding_can_be_disabled_explicitly(monkeypatch):
    from app import vision
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "redacted-test-key")
    monkeypatch.setattr(settings, "vision_provider", "gemini")
    assert vision.resolve_provider_name() == "gemini"


# ── provider chain fallback ───────────────────────────────────────────────
class _Boom:
    name = "boom"

    def identify(self, image_bytes, mime="image/jpeg"):
        raise GroundingUnavailable("search not available in your region")


class _Fine:
    name = "fine"

    def identify(self, image_bytes, mime="image/jpeg"):
        from app.models import Confidence, IdentificationReport

        return IdentificationReport(
            part_name="Serpentine Belt Tensioner",
            oem_part_number="53034046AB",
            overall_confidence=Confidence.medium,
            number_confidence=Confidence.medium,
            provider="fake",
            model_name="fake-1",
            needs_manual_review=False,
        )


def test_failed_grounding_falls_through_to_next_provider(monkeypatch):
    from app import vision
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "redacted-test-key")
    monkeypatch.setattr(settings, "openai_api_key", "redacted-test-key")
    monkeypatch.setattr(settings, "vision_provider", "auto")
    monkeypatch.setattr(settings, "grounding_enabled", True)
    monkeypatch.setattr(vision, "_ollama_running", lambda: False)
    monkeypatch.setattr(
        vision,
        "_build",
        lambda name: _Boom() if name == "gemini-grounded" else _Fine(),
    )

    report = vision.identify(b"img")

    assert report.part_name == "Serpentine Belt Tensioner"
    assert report.provider == "fake"
    assert report.needs_manual_review is True
    assert any("fell back to openai" in w for w in report.warnings)
    assert any("gemini-grounded" in w for w in report.warnings)


def test_total_failure_still_returns_a_usable_report(monkeypatch):
    from app import vision
    from app.config import settings

    monkeypatch.setattr(settings, "vision_provider", "auto")
    monkeypatch.setattr(vision, "_build", lambda name: _Boom())

    report = vision.identify(b"img")

    assert report.provider == "none"
    assert report.needs_manual_review is True
    assert "manually" in " ".join(report.warnings)
