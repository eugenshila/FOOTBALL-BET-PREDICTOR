"""Provider selection with graceful degradation.

Order of preference for ``VISION_PROVIDER=auto``:
    openai -> gemini -> anthropic -> ollama -> offline
A configured provider that errors at runtime falls back to offline and says so
in the report warnings, so a shop never hits a dead end at the counter.
"""

from __future__ import annotations

import logging
import time

import httpx

from ..config import settings
from ..models import IdentificationReport
from .llm import AnthropicProvider, GeminiProvider, OllamaProvider, OpenAIProvider
from .offline import OfflineIdentifier

log = logging.getLogger(__name__)

_PROVIDERS = {
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "anthropic": AnthropicProvider,
    "ollama": OllamaProvider,
}


_ollama_probe: tuple[float, bool] = (0.0, False)
_OLLAMA_TTL = 60.0


def _ollama_running() -> bool:
    """Ollama needs no key, so probe for it instead of assuming it exists."""
    global _ollama_probe
    stamp, cached = _ollama_probe
    if time.time() - stamp < _OLLAMA_TTL:
        return cached
    try:
        with httpx.Client(timeout=httpx.Timeout(1.2)) as client:
            running = client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags").status_code == 200
    except Exception:  # noqa: BLE001 - absence of a local server is normal
        running = False
    _ollama_probe = (time.time(), running)
    return running


def _configured(name: str) -> bool:
    return {
        "openai": bool(settings.openai_api_key),
        "gemini": bool(settings.gemini_api_key),
        "anthropic": bool(settings.anthropic_api_key),
        "ollama": _ollama_running(),
    }.get(name, False)


def available_providers() -> list[str]:
    names = ["offline"]
    names.extend(name for name in _PROVIDERS if _configured(name))
    return names


def _build(name: str):
    if name == "openai":
        return OpenAIProvider(
            api_key=settings.openai_api_key,
            model=settings.vision_model,
            base_url=settings.openai_base_url,
        )
    if name == "gemini":
        return GeminiProvider(api_key=settings.gemini_api_key, model=settings.vision_model)
    if name == "anthropic":
        return AnthropicProvider(api_key=settings.anthropic_api_key, model=settings.vision_model)
    if name == "ollama":
        return OllamaProvider(model=settings.ollama_model or settings.vision_model, base_url=settings.ollama_base_url)
    return OfflineIdentifier()


def resolve_provider_name() -> str:
    requested = (settings.vision_provider or "auto").lower()
    if requested != "auto":
        if requested == "offline":
            return "offline"
        if requested in _PROVIDERS and _configured(requested):
            return requested
        return "offline"
    for candidate in ("openai", "gemini", "anthropic", "ollama"):
        if _configured(candidate):
            return candidate
    return "offline"


def identify(image_bytes: bytes, mime: str = "image/jpeg") -> IdentificationReport:
    """Identify a part, never raising for provider problems."""
    name = resolve_provider_name()
    requested = (settings.vision_provider or "auto").lower()
    if requested not in {"auto", "offline"} and requested != name:
        log.warning("Requested provider %s is not configured; using %s", requested, name)

    try:
        report = _build(name).identify(image_bytes, mime)
        if name != "offline":
            report.warnings = [
                "Identified by a vision model: verify part numbers against the vehicle EPC before quoting.",
                *report.warnings,
            ]
        return report
    except Exception as exc:  # noqa: BLE001 - a failed provider must not break the counter
        log.exception("Vision provider %s failed", name)
        fallback = OfflineIdentifier().identify(image_bytes, mime)
        fallback.warnings.insert(
            0,
            f"Vision provider '{name}' failed ({type(exc).__name__}); fell back to offline reference matching.",
        )
        fallback.needs_manual_review = True
        return fallback


__all__ = ["identify", "resolve_provider_name", "available_providers", "OfflineIdentifier"]
