"""One-tap reverse-image-search links.

Two zero-key paths for the operator, both free:

* **Google Lens** - ``lens.google.com/uploadbyurl?url=<public photo url>`` opens
  the uploaded photo in Lens, which is genuinely good at automotive parts. It
  needs the photo to be reachable on the public internet, so this only produces a
  link when the app is served from a public host (or ``PUBLIC_BASE_URL`` is set).
* **Google Images** - the same idea via ``searchbyimage``, kept as a fallback for
  when Lens is unavailable in a region.

Neither call costs money or needs an API key: they are plain links the operator
taps. They are also the honest fallback when no vision provider is configured.
"""

from __future__ import annotations

from urllib.parse import quote, urlparse

from .config import settings

_PRIVATE_HOSTS = ("0.0.0.0", "127.0.0.1", "localhost", "::1")


def is_public_base(base_url: str) -> bool:
    """True when the base URL is reachable from the outside world."""
    if not base_url:
        return False
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host in _PRIVATE_HOSTS:
        return False
    return not (host.startswith("192.168.") or host.startswith("10.") or host.startswith("172.16."))


def resolve_base_url(request_base: str = "") -> str:
    """Prefer an explicit PUBLIC_BASE_URL, else the request's own origin."""
    if settings.public_base_url:
        return settings.public_base_url
    candidate = (request_base or "").rstrip("/")
    return candidate if is_public_base(candidate) else ""


def absolute_media_url(image_url: str, base_url: str) -> str:
    if not image_url or not base_url:
        return ""
    if image_url.startswith("http"):
        return image_url
    return f"{base_url}{image_url if image_url.startswith('/') else '/' + image_url}"


def lens_url(public_image_url: str) -> str:
    """Google Lens link for a publicly reachable image; '' when not possible."""
    if not public_image_url or not is_public_base(public_image_url):
        return ""
    return f"https://lens.google.com/uploadbyurl?url={quote(public_image_url, safe='')}"


def google_images_url(public_image_url: str) -> str:
    if not public_image_url or not is_public_base(public_image_url):
        return ""
    return f"https://www.google.com/searchbyimage?image_url={quote(public_image_url, safe='')}"


def links_for(image_url: str, base_url: str) -> tuple[str, str]:
    """(lens_url, google_images_url) for a media path on this deployment."""
    absolute = absolute_media_url(image_url, base_url)
    return lens_url(absolute), google_images_url(absolute)
