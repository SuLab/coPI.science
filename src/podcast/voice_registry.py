"""Runtime voice registry: fetch and cache available TTS voices per backend.

At startup the app calls refresh_voices() to pre-warm the cache.
Settings pages call get_voices() which returns the cached list immediately
(or refreshes if the cache is stale / missing).

Mistral  — fetched from /v1/audio/voices (paginated, deduped by slug)
Local    — fetched from /v1/audio/voices on the configured vLLM server
OpenAI   — fixed set; no API call needed

All paths fall back to a hardcoded list if the remote endpoint is unreachable,
so the settings UI always renders even if a TTS service is down.
"""

import logging
import time

import httpx

logger = logging.getLogger(__name__)

_CACHE_TTL = 86_400  # 24 hours

# (voice_id, display_label) pairs, keyed by backend name
_cache: dict[str, tuple[list[tuple[str, str]], float]] = {}

# Static fallbacks used when the live endpoint is unreachable
_FALLBACK: dict[str, list[tuple[str, str]]] = {
    "mistral": [
        ("en_paul_neutral",    "Paul — US English, male, neutral"),
        ("en_paul_confident",  "Paul — US English, male, confident"),
        ("en_paul_cheerful",   "Paul — US English, male, cheerful"),
        ("en_paul_happy",      "Paul — US English, male, happy"),
        ("gb_oliver_neutral",  "Oliver — British English, male, neutral"),
        ("gb_jane_sarcasm",    "Jane — British English, female"),
    ],
    "openai": [
        ("alloy",   "Alloy"),
        ("echo",    "Echo"),
        ("fable",   "Fable"),
        ("onyx",    "Onyx"),
        ("nova",    "Nova"),
        ("shimmer", "Shimmer"),
    ],
    "local": [],
}


def get_cached_voices(backend: str) -> list[tuple[str, str]]:
    """Return cached voices synchronously without making any API calls.

    Returns stale cache if present, otherwise the static fallback.
    """
    if backend in _cache:
        return _cache[backend][0]
    return list(_FALLBACK.get(backend, []))


async def _fetch_mistral(api_key: str) -> list[tuple[str, str]]:
    voices: list[tuple[str, str]] = []
    seen: set[str] = set()
    page = 1
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            resp = await client.get(
                "https://api.mistral.ai/v1/audio/voices",
                params={"page": page, "page_size": 50},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])
            new_count = 0
            for v in items:
                slug = v.get("slug", "")
                if not slug or slug in seen:
                    continue
                seen.add(slug)
                name = v.get("name", slug)
                gender = v.get("gender", "")
                langs = ", ".join(v.get("languages") or [])
                parts = [p for p in [langs, gender] if p]
                label = f"{name} — {', '.join(parts)}" if parts else name
                voices.append((slug, label))
                new_count += 1
            total_pages = data.get("total_pages", 1)
            if new_count == 0 or page >= total_pages:
                break
            page += 1
    return voices


async def _fetch_local(host: str, port: int) -> list[tuple[str, str]]:
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(f"http://{host}:{port}/v1/audio/voices")
        resp.raise_for_status()
        data = resp.json()
    items = data if isinstance(data, list) else data.get("voices", data.get("items", []))
    voices: list[tuple[str, str]] = []
    for v in items:
        if isinstance(v, str):
            voices.append((v, v))
        elif isinstance(v, dict):
            vid = v.get("id") or v.get("name") or v.get("slug", "")
            label = v.get("name") or v.get("label") or vid
            if vid:
                voices.append((vid, str(label)))
    return voices


async def refresh_voices(backend: str | None = None) -> list[tuple[str, str]]:
    """Fetch fresh voices from the live endpoint and update the cache.

    Returns the newly cached list, or falls back to stale cache / static fallback
    if the endpoint is unreachable.
    """
    from src.config import get_settings

    settings = get_settings()
    active = backend or settings.podcast_tts_backend

    try:
        if active == "mistral":
            voices = await _fetch_mistral(settings.mistral_api_key)
        elif active == "local":
            voices = await _fetch_local(settings.local_tts_host, settings.local_tts_port)
        elif active == "openai":
            voices = list(_FALLBACK["openai"])  # fixed set
        else:
            voices = []

        if voices:
            _cache[active] = (voices, time.monotonic())
            logger.info("Voice registry: cached %d voices for backend %r", len(voices), active)
        else:
            logger.warning("Voice registry: no voices returned for backend %r", active)

    except Exception as exc:
        logger.warning("Voice registry: fetch failed for backend %r: %s", active, exc)

    return get_cached_voices(active)


async def get_voices(backend: str | None = None) -> list[tuple[str, str]]:
    """Return voices for the backend, refreshing if the cache is stale or missing."""
    from src.config import get_settings

    settings = get_settings()
    active = backend or settings.podcast_tts_backend

    now = time.monotonic()
    if active in _cache:
        _, fetched_at = _cache[active]
        if now - fetched_at < _CACHE_TTL:
            return _cache[active][0]

    return await refresh_voices(active)
