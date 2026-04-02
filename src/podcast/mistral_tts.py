"""Mistral AI TTS client wrapper."""

import base64
import json
import logging
import re
from pathlib import Path

import httpx

from src.config import get_settings

logger = logging.getLogger(__name__)

VOICES_FILE = Path("data/podcast_voices.json")
MISTRAL_TTS_URL = "https://api.mistral.ai/v1/audio/speech"


def get_voice(agent_id: str) -> str:
    """Return the configured voice for an agent, falling back to default."""
    settings = get_settings()
    if VOICES_FILE.exists():
        try:
            voices = json.loads(VOICES_FILE.read_text(encoding="utf-8"))
            if agent_id in voices:
                return voices[agent_id]
        except Exception as exc:
            logger.warning("Failed to load podcast_voices.json: %s", exc)
    return settings.mistral_tts_default_voice


def _strip_markdown(text: str) -> str:
    """Remove markdown formatting so TTS reads clean prose."""
    # Remove bold/italic markers (* and _)
    text = re.sub(r"\*+([^*]+)\*+", r"\1", text)
    text = re.sub(r"_+([^_]+)_+", r"\1", text)
    # Remove inline code
    text = re.sub(r"`[^`]+`", "", text)
    # Remove URLs but keep surrounding text
    text = re.sub(r"https?://\S+", "", text)
    return text.strip()


async def generate_audio(text: str, agent_id: str, output_path: Path) -> bool:
    """Generate TTS audio via Mistral AI and save to output_path.

    Returns True on success, False on failure.
    """
    settings = get_settings()
    if not settings.mistral_api_key:
        logger.warning("MISTRAL_API_KEY not set — skipping audio generation")
        return False

    voice = get_voice(agent_id)
    clean_text = _strip_markdown(text)
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
        return True
    except Exception as exc:
        logger.error("Mistral TTS failed for agent %s: %s", agent_id, exc)
        return False


def get_audio_duration_seconds(audio_path: Path) -> int | None:
    """Return audio duration in seconds using mutagen, or None if unavailable."""
    try:
        from mutagen.mp3 import MP3
        audio = MP3(str(audio_path))
        return int(audio.info.length)
    except Exception as exc:
        logger.debug("Could not read audio duration from %s: %s", audio_path, exc)
        return None
