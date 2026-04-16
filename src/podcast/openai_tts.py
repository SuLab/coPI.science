"""OpenAI TTS client wrapper.

Uses the OpenAI /v1/audio/speech endpoint.  Returns raw MP3 bytes.

Set in .env:
    PODCAST_TTS_BACKEND=openai
    OPENAI_API_KEY=sk-...
    OPENAI_TTS_MODEL=tts-1          # or tts-1-hd / gpt-4o-mini-tts
    OPENAI_TTS_DEFAULT_VOICE=alloy  # alloy echo fable onyx nova shimmer
"""

import json
import logging
from pathlib import Path

import httpx

from src.config import get_settings
from src.podcast.tts_utils import get_audio_duration_seconds, normalize_audio, strip_markdown

logger = logging.getLogger(__name__)

VOICES_FILE = Path("data/podcast_voices.json")
OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"

__all__ = ["generate_audio", "get_audio_duration_seconds"]


def get_voice(agent_id: str, voice_override: str | None = None) -> str:
    """Return the TTS voice for an agent.

    Priority: voice_override (from DB preferences) → podcast_voices.json → env default.
    """
    if voice_override:
        return voice_override
    settings = get_settings()
    if VOICES_FILE.exists():
        try:
            voices = json.loads(VOICES_FILE.read_text(encoding="utf-8"))
            if agent_id in voices:
                return voices[agent_id]
        except Exception as exc:
            logger.warning("Failed to load podcast_voices.json: %s", exc)
    return settings.openai_tts_default_voice or "alloy"


async def generate_audio(
    text: str, agent_id: str, output_path: Path, voice_override: str | None = None
) -> bool:
    """Generate TTS audio via OpenAI and save to output_path.

    Returns True on success, False on failure.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        logger.warning("OPENAI_API_KEY not set — skipping audio generation")
        return False

    voice = get_voice(agent_id, voice_override=voice_override)
    clean_text = strip_markdown(text)
    payload = {
        "model": settings.openai_tts_model,
        "input": clean_text,
        "voice": voice,
        "response_format": "mp3",
    }
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }

    logger.info(
        "OpenAI TTS request (model=%s, voice=%s)", settings.openai_tts_model, voice
    )

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(OPENAI_TTS_URL, json=payload, headers=headers)
            if not resp.is_success:
                logger.error("OpenAI TTS API error %s: %s", resp.status_code, resp.text)
            resp.raise_for_status()

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(resp.content)
        logger.info("Audio saved to %s (%d bytes)", output_path, len(resp.content))
        if settings.podcast_normalize_audio:
            normalize_audio(output_path)
        return True
    except Exception as exc:
        logger.error("OpenAI TTS failed for agent %s: %s", agent_id, exc)
        return False
