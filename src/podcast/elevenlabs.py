"""ElevenLabs TTS client wrapper."""

import json
import logging
from pathlib import Path

import httpx

from src.config import get_settings

logger = logging.getLogger(__name__)

VOICES_FILE = Path("data/podcast_voices.json")
ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"


def get_voice_id(agent_id: str) -> str:
    """Return the configured voice ID for an agent, falling back to default."""
    settings = get_settings()
    if VOICES_FILE.exists():
        try:
            voices = json.loads(VOICES_FILE.read_text(encoding="utf-8"))
            if agent_id in voices:
                return voices[agent_id]
        except Exception as exc:
            logger.warning("Failed to load podcast_voices.json: %s", exc)
    return settings.elevenlabs_default_voice_id


async def generate_audio(text: str, agent_id: str, output_path: Path) -> bool:
    """Generate TTS audio via ElevenLabs and save to output_path.

    Returns True on success, False on failure.
    """
    settings = get_settings()
    if not settings.elevenlabs_api_key:
        logger.warning("ELEVENLABS_API_KEY not set — skipping audio generation")
        return False

    voice_id = get_voice_id(agent_id)
    url = f"{ELEVENLABS_API_BASE}/text-to-speech/{voice_id}"

    payload = {
        "text": text,
        "model_id": settings.elevenlabs_model,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
        },
    }
    headers = {
        "xi-api-key": settings.elevenlabs_api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(resp.content)
        logger.info("Audio saved to %s (%d bytes)", output_path, len(resp.content))
        return True
    except Exception as exc:
        logger.error("ElevenLabs TTS failed for agent %s: %s", agent_id, exc)
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
