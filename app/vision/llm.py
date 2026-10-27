"""Multimodal providers: OpenAI-compatible, Gemini, Anthropic and Ollama.

All of them return the same :class:`IdentificationReport`, so swapping provider
never changes anything downstream. Every provider is optional - the app falls
back to the offline matcher when no key is configured.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx

from ..models import Confidence, IdentificationReport, Source, VehicleFitment
from .prompt import RESPONSE_SCHEMA, SYSTEM_PROMPT, USER_PROMPT

TIMEOUT = httpx.Timeout(60.0, connect=10.0)

_COERCIONS = {
    "high": Confidence.high,
    "medium": Confidence.medium,
    "med": Confidence.medium,
    "low": Confidence.low,
}


def _confidence(value: Any, default: Confidence = Confidence.low) -> Confidence:
    if isinstance(value, str):
        return _COERCIONS.get(value.strip().lower(), default)
    return default


def _clean_number(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().upper()
    if text in {"", "N/A", "NA", "UNKNOWN", "NONE", "NOT VISIBLE", "N/A."}:
        return ""
    # Part numbers are alphanumeric with optional dashes; reject sentences.
    if len(text) > 24 or not re.fullmatch(r"[A-Z0-9][A-Z0-9\-\s./]{2,23}", text):
        return ""
    return value.strip()


def _clean_numbers(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        cleaned = _clean_number(value)
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model response."""
    if not text:
        raise ValueError("empty model response")
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object in model response")
    return json.loads(text[start : end + 1])


def report_from_payload(payload: dict, provider: str, model: str) -> IdentificationReport:
    fitment: list[VehicleFitment] = []
    for raw in payload.get("fitment") or []:
        if isinstance(raw, dict):
            fitment.append(
                VehicleFitment(
                    make=str(raw.get("make", "") or ""),
                    model=str(raw.get("model", "") or ""),
                    engine=str(raw.get("engine", "") or ""),
                    years=str(raw.get("years", "") or ""),
                )
            )

    notes = [str(n) for n in (payload.get("technical_notes") or []) if str(n).strip()]
    model_notes = str(payload.get("notes") or "").strip()
    if model_notes:
        notes.append(f"Model note: {model_notes}")

    overall = _confidence(payload.get("confidence"))
    number_conf = _confidence(payload.get("number_confidence"), default=overall)
    oem = _clean_number(payload.get("oem_part_number"))

    warnings: list[str] = []
    if not oem:
        warnings.append("No OEM number returned - read the casting/etched number on the part or use the EPC.")
    if number_conf is not Confidence.high:
        warnings.append("Part number is not high-confidence - verify against the vehicle's EPC before quoting.")

    part_name = str(payload.get("component_name") or "").strip()
    if not part_name:
        warnings.append("Model did not name the component; manual identification required.")

    return IdentificationReport(
        part_name=part_name,
        category=str(payload.get("category") or "").strip(),
        oem_part_number=oem,
        alternate_numbers=_clean_numbers(payload.get("alternate_numbers")),
        superseded_numbers=_clean_numbers(payload.get("superseded_numbers")),
        number_confidence=number_conf,
        fitment=fitment,
        technical_notes=notes,
        visual_evidence=[str(v) for v in (payload.get("visual_evidence") or []) if str(v).strip()],
        overall_confidence=overall,
        provider=provider,
        model_name=model,
        needs_manual_review=overall is not Confidence.high or number_conf is not Confidence.high,
        warnings=warnings,
        raw_model_output=json.dumps(payload, ensure_ascii=False)[:4000],
    )


class _BaseProvider:
    name = "base"
    default_model = ""

    def __init__(self, api_key: str = "", model: str = "", base_url: str = "") -> None:
        self.api_key = api_key
        self.model_name = model or self.default_model
        self.base_url = base_url.rstrip("/") if base_url else ""

    def identify(self, image_bytes: bytes, mime: str = "image/jpeg") -> IdentificationReport:
        raise NotImplementedError

    @staticmethod
    def _b64(image_bytes: bytes) -> str:
        return base64.b64encode(image_bytes).decode("ascii")


class OpenAIProvider(_BaseProvider):
    """Any OpenAI-compatible vision endpoint (OpenAI, Azure-style proxies, vLLM)."""

    name = "openai"
    default_model = "gpt-4o-mini"

    def identify(self, image_bytes: bytes, mime: str = "image/jpeg") -> IdentificationReport:
        payload = {
            "model": self.model_name,
            "temperature": 0,
            "max_tokens": 1400,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": USER_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{self._b64(image_bytes)}"},
                        },
                    ],
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "part_identification", "strict": True, "schema": RESPONSE_SCHEMA},
            },
        }
        url = f"{self.base_url or 'https://api.openai.com/v1'}/chat/completions"
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(url, json=payload, headers={"Authorization": f"Bearer {self.api_key}"})
            response.raise_for_status()
            data = response.json()
        content = data["choices"][0]["message"]["content"]
        return report_from_payload(extract_json(content), self.name, self.model_name)


class GeminiProvider(_BaseProvider):
    name = "gemini"
    default_model = "gemini-2.0-flash"

    def identify(self, image_bytes: bytes, mime: str = "image/jpeg") -> IdentificationReport:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model_name}:generateContent?key={self.api_key}"
        )
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": USER_PROMPT},
                        {"inline_data": {"mime_type": mime, "data": self._b64(image_bytes)}},
                    ],
                }
            ],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return report_from_payload(extract_json(text), self.name, self.model_name)


class AnthropicProvider(_BaseProvider):
    name = "anthropic"
    default_model = "claude-sonnet-4-20250514"

    def identify(self, image_bytes: bytes, mime: str = "image/jpeg") -> IdentificationReport:
        payload = {
            "model": self.model_name,
            "max_tokens": 1600,
            "temperature": 0,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime,
                                "data": self._b64(image_bytes),
                            },
                        },
                        {"type": "text", "text": USER_PROMPT},
                    ],
                }
            ],
        }
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(
                "https://api.anthropic.com/v1/messages",
                json=payload,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            response.raise_for_status()
            data = response.json()
        text = "".join(block.get("text", "") for block in data.get("content", []))
        return report_from_payload(extract_json(text), self.name, self.model_name)


class OllamaProvider(_BaseProvider):
    """Fully local vision model, e.g. llava or llama3.2-vision."""

    name = "ollama"
    default_model = "llava"

    def identify(self, image_bytes: bytes, mime: str = "image/jpeg") -> IdentificationReport:
        payload = {
            "model": self.model_name,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_PROMPT, "images": [self._b64(image_bytes)]},
            ],
        }
        with httpx.Client(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
            response = client.post(f"{self.base_url or 'http://127.0.0.1:11434'}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
        content = data.get("message", {}).get("content", "")
        return report_from_payload(extract_json(content), self.name, self.model_name)
