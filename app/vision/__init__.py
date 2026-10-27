"""Provider selection with graceful degradation.

Order of preference for ``VISION_PROVIDER=auto``:
    gemini-grounded -> openai -> gemini -> anthropic -> ollama -> offline
(the grounded variant is preferred when a Gemini key exists and SEARCH_GROUNDING
is on, because a searched part number is worth more than a guessed one)
A configured provider that errors at runtime falls back to offline and says so
in the report warnings, so a shop never hits a dead end at the counter.
"""

from __future__ import annotations

import logging
import time

import httpx

from ..config import settings
from ..models import IdentificationReport
from .grounded import GeminiGroundedProvider, GroundingUnavailable
from .llm import AnthropicProvider, GeminiProvider, OllamaProvider, OpenAIProvider
from .offline import OfflineIdentifier

log = logging.getLogger(__name__)

_PROVIDERS = {
    "gemini-grounded": GeminiGroundedProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "anthropic": AnthropicProvider,
    "ollama": OllamaProvider,
}

# Ordered list used by ``auto``. Grounded search first: it produces verified
# numbers plus citations, which is the whole point of the feature.
_AUTO_ORDER = ("gemini-grounded", "openai", "gemini", "anthropic", "ollama")

_PROVIDER_LABELS = {
    "gemini-grounded": "Gemini + Google Search",
    "openai": "OpenAI vision",
    "gemini": "Gemini vision",
    "anthropic": "Claude vision",
    "ollama": "Ollama (local)",
    "offline": "Offline reference matcher",
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
        "gemini-grounded": bool(settings.gemini_api_key) and settings.grounding_enabled,
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
    if name == "gemini-grounded":
        return GeminiGroundedProvider(
            api_key=settings.gemini_api_key,
            model=settings.grounding_model or settings.vision_model,
        )
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
    for candidate in _AUTO_ORDER:
        if _configured(candidate):
            return candidate
    return "offline"


def _provider_chain(first: str) -> list[str]:
    """The provider to try, then every other configured one, then offline."""
    chain = [first] if first != "offline" else []
    chain.extend(p for p in _AUTO_ORDER if p != first and _configured(p))
    chain.append("offline")
    return chain


def identify(image_bytes: bytes, mime: str = "image/jpeg") -> IdentificationReport:
    """Identify a part, never raising for provider problems.

    Walks the provider chain so a dead key, a region-blocked search tool or a
    stopped local model degrades to the next best option instead of failing the
    scan. Every downgrade is recorded in ``report.warnings``.
    """
    requested = (settings.vision_provider or "auto").lower()
    first = resolve_provider_name()
    if requested not in {"auto", "offline"} and requested != first:
        log.warning("Requested provider %s is not configured; using %s", requested, first)

    chain = _provider_chain(first)
    failures: list[str] = []

    for name in chain:
        try:
            report = _build(name).identify(image_bytes, mime)
        except Exception as exc:  # noqa: BLE001 - a provider must never break the counter
            log.exception("Vision provider %s failed", name)
            failures.append(f"{name} ({type(exc).__name__})")
            continue

        if failures:
            report.warnings.insert(
                0,
                "Primary identification unavailable ("
                + ", ".join(failures)
                + f"); fell back to {name}. Treat part numbers with extra care.",
            )
            report.needs_manual_review = True

        if name != "offline" and not report.grounded:
            report.warnings.insert(
                0,
                "Identified by a vision model: verify part numbers against the vehicle EPC before quoting.",
            )
        return report

    # Only reachable if even the offline matcher blew up.
    log.error("All identification providers failed: %s", failures)
    return IdentificationReport(
        provider="none",
        model_name="",
        needs_manual_review=True,
        warnings=[
            "Every identification provider failed ("
            + ", ".join(failures)
            + "). Enter the part manually.",
        ],
    )


def provider_label(name: str | None = None) -> str:
    """Human label for the badge in the UI."""
    return _PROVIDER_LABELS.get(name or resolve_provider_name(), "Unknown provider")


def describe() -> dict:
    """Provider state for /api/health - what is on, and why."""
    resolved = resolve_provider_name()
    return {
        "provider": resolved,
        "label": provider_label(resolved),
        "available": available_providers(),
        "grounding_enabled": settings.grounding_enabled,
        "grounding_model": settings.grounding_model,
        "grounding_active": resolved == "gemini-grounded",
        "gemini_key_present": bool(settings.gemini_api_key),
    }


__all__ = [
    "identify",
    "resolve_provider_name",
    "available_providers",
    "provider_label",
    "describe",
    "OfflineIdentifier",
    "GroundingUnavailable",
]
