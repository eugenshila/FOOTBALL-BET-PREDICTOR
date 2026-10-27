"""Google-Search-grounded identification (Gemini + ``google_search`` tool).

Why this exists: a plain vision model recognises shapes, but part numbers live in
catalogues on the web. Letting the model *search* turns "looks like a PCV valve"
into "PCV valve, OEM 5184894AA, fits 2011-2021 Pentastar" **with the pages it used
listed as sources**, so a human can check the claim before quoting.

This uses the Gemini API free tier (no billing required). The same pattern is what
Google's own "grounding" demos use: an image + a search tool in one request.

Cost control: grounding bills per search query, so ``SEARCH_GROUNDING=false`` or
``VISION_PROVIDER=gemini`` disables it and falls back to plain vision.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ..models import IdentificationReport, SearchSource
from .llm import TIMEOUT, report_from_payload
from .prompt import GROUNDED_SYSTEM_PROMPT, GROUNDED_USER_PROMPT, RESPONSE_SCHEMA

log = logging.getLogger(__name__)

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GroundingUnavailable(RuntimeError):
    """Raised when the API rejects the search tool (model/region/plan limits)."""


def _sources_from(payload: dict) -> tuple[list[SearchSource], list[str]]:
    """Pull web citations and the queries the model actually ran."""
    metadata = (
        payload.get("candidates", [{}])[0].get("groundingMetadata")
        or payload.get("groundingMetadata")
        or {}
    )
    sources: list[SearchSource] = []
    seen: set[str] = set()
    for chunk in metadata.get("groundingChunks") or []:
        web = (chunk or {}).get("web") or {}
        url = str(web.get("uri") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        title = str(web.get("title") or "").strip() or url.split("/")[2]
        sources.append(SearchSource(title=title, url=url))

    queries = [str(q) for q in (metadata.get("webSearchQueries") or []) if str(q).strip()]
    return sources[:8], queries[:6]


class GeminiGroundedProvider:
    """Gemini vision + Google Search grounding."""

    name = "gemini-grounded"
    default_model = "gemini-2.5-flash"

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self.api_key = api_key
        self.model_name = model or self.default_model
        self._transport = transport
        self._timeout = timeout or TIMEOUT

    # -- request plumbing ---------------------------------------------------
    def _body(self, image_bytes: bytes, mime: str, *, json_mode: bool) -> dict[str, Any]:
        import base64

        generation: dict[str, Any] = {"temperature": 0}
        if json_mode:
            generation["responseMimeType"] = "application/json"

        return {
            "system_instruction": {"parts": [{"text": GROUNDED_SYSTEM_PROMPT}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": GROUNDED_USER_PROMPT},
                        {
                            "inline_data": {
                                "mime_type": mime,
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            }
                        },
                    ],
                }
            ],
            "tools": [{"google_search": {}}],
            "generationConfig": generation,
        }

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        url = ENDPOINT.format(model=self.model_name)
        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            return client.post(
                url,
                json=body,
                headers={"x-goog-api-key": self.api_key, "content-type": "application/json"},
            )

    # -- public API ---------------------------------------------------------
    def identify(self, image_bytes: bytes, mime: str = "image/jpeg") -> IdentificationReport:
        response = self._post(self._body(image_bytes, mime, json_mode=True))

        # Some models reject JSON mode together with tools; retry plain and
        # extract the JSON object from the text instead.
        if response.status_code == 400:
            detail = response.text[:400]
            response = self._post(self._body(image_bytes, mime, json_mode=False))
            if response.status_code == 400:
                raise GroundingUnavailable(
                    f"Gemini rejected the search tool for {self.model_name}: {detail}"
                )

        response.raise_for_status()
        data = response.json()

        candidates = data.get("candidates") or []
        if not candidates:
            raise GroundingUnavailable("Gemini returned no candidates (possibly safety-blocked)")

        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(str(p.get("text", "")) for p in parts).strip()
        if not text:
            raise GroundingUnavailable("Gemini returned an empty response")

        payload = json.loads(_first_json_object(text))
        report = report_from_payload(payload, self.name, self.model_name)

        sources, queries = _sources_from(data)
        report.grounded = True
        report.sources = sources
        report.search_queries = queries

        if sources:
            report.visual_evidence.append(
                f"Google Search grounding: {len(sources)} source(s) consulted."
            )
            report.warnings.append(
                "Part numbers came from live web sources - open the cited pages and confirm "
                "against the vehicle EPC before quoting."
            )
        else:
            report.warnings.append(
                "Search grounding returned no sources; treat the part number as model-only."
            )
        return report


def _first_json_object(text: str) -> str:
    """Return the first complete JSON object in ``text``."""
    start = text.find("{")
    if start == -1:
        raise GroundingUnavailable("no JSON object in the grounded response")
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise GroundingUnavailable("unterminated JSON object in the grounded response")


__all__ = ["GeminiGroundedProvider", "GroundingUnavailable", "RESPONSE_SCHEMA"]
