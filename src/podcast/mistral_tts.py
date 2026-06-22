"""Mistral AI TTS client wrapper."""

import base64
import json
import logging
from pathlib import Path

import httpx

from src.config import get_settings
from src.podcast.tts_utils import get_audio_duration_seconds, normalize_audio, strip_markdown

logger = logging.getLogger(__name__)

VOICES_FILE = Path("data/podcast_voices.json")
MISTRAL_TTS_URL = "https://api.mistral.ai/v1/audio/speech"

__all__ = ["generate_audio", "get_audio_duration_seconds"]


def _get_default_voice() -> str:
    """Return the configured default voice, falling back to the first cached voice."""
    settings = get_settings()
    if settings.mistral_tts_default_voice:
        return settings.mistral_tts_default_voice
    from src.podcast.voice_registry import get_cached_voices
    cached = get_cached_voices("mistral")
    return cached[0][0] if cached else "en_paul_neutral"


def get_voice(agent_id: str, voice_override: str | None = None) -> str:
    """Return the TTS voice for an agent.

    Priority: voice_override (from DB preferences) → podcast_voices.json → env default.
    """
    if voice_override:
        return voice_override
    if VOICES_FILE.exists():
        try:
            voices = json.loads(VOICES_FILE.read_text(encoding="utf-8"))
            if agent_id in voices:
                return voices[agent_id]
        except Exception as exc:
            logger.warning("Failed to load podcast_voices.json: %s", exc)
    return _get_default_voice()


async def generate_audio(
    text: str, agent_id: str, output_path: Path, voice_override: str | None = None
) -> bool:
    """Generate TTS audio via Mistral AI and save to output_path.

    Returns True on success, False on failure.
    """
    settings = get_settings()
    if not settings.mistral_api_key:
        logger.warning("MISTRAL_API_KEY not set — skipping audio generation")
        return False

    voice = get_voice(agent_id, voice_override=voice_override)
    clean_text = strip_markdown(text)
    payload = {
        "model": settings.mistral_tts_model,
        "input": clean_text,
        "voice": voice,
    }
    headers = {
        "Authorization": f"Bearer {settings.mistral_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(MISTRAL_TTS_URL, json=payload, headers=headers)
            default_voice = _get_default_voice()
            if resp.status_code == 404 and voice != default_voice:
                # Voice was removed or renamed; retry with the configured default
                logger.warning(
                    "Mistral TTS voice %r not found — retrying with default voice %r",
                    voice, default_voice,
                )
                payload["voice"] = default_voice
                resp = await client.post(MISTRAL_TTS_URL, json=payload, headers=headers)
            if not resp.is_success:
                logger.error("Mistral TTS API error %s: %s", resp.status_code, resp.text)
            resp.raise_for_status()

        # Mistral returns {"audio_data": "<base64-encoded mp3>"}
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type or resp.content[:1] == b"{":
            audio_bytes = base64.b64decode(resp.json()["audio_data"])
        else:
            audio_bytes = resp.content

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(audio_bytes)
        logger.info("Audio saved to %s (%d bytes)", output_path, len(audio_bytes))
        if settings.podcast_normalize_audio:
            normalize_audio(output_path)
        return True
    except Exception as exc:
        logger.error("Mistral TTS failed for agent %s: %s", agent_id, exc)
        return False


