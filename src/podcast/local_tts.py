"""Local TTS backend using a vLLM-Omni server.

vLLM-Omni exposes an OpenAI-compatible /v1/audio/speech endpoint that accepts
the same JSON payload as OpenAI TTS and returns raw audio bytes directly.

Start a vLLM-Omni server with, e.g.:
    vllm serve Qwen/Qwen2-Audio-7B-Instruct --port 8010

Then set in .env:
    PODCAST_TTS_BACKEND=local
    LOCAL_TTS_HOST=127.0.0.1
    LOCAL_TTS_PORT=8010
    LOCAL_TTS_MODEL=Qwen/Qwen2-Audio-7B-Instruct
    LOCAL_TTS_VOICE=default
"""

import json
import logging
from pathlib import Path

import httpx

from src.config import get_settings
from src.podcast.tts_utils import get_audio_duration_seconds, normalize_audio, strip_markdown

logger = logging.getLogger(__name__)

VOICES_FILE = Path("data/podcast_voices.json")

__all__ = ["generate_audio", "get_audio_duration_seconds"]


def _get_local_tts_url() -> str:
    settings = get_settings()
    return f"http://{settings.local_tts_host}:{settings.local_tts_port}/v1/audio/speech"


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
    return settings.local_tts_voice or "default"


async def generate_audio(text: str, agent_id: str, output_path: Path) -> bool:
    """Generate TTS audio via a local vLLM-Omni server and save to output_path.

    The server must expose an OpenAI-compatible /v1/audio/speech endpoint
    that returns raw audio bytes.

    Returns True on success, False on failure.
    """
    settings = get_settings()
    url = _get_local_tts_url()
    voice = get_voice(agent_id)
    clean_text = strip_markdown(text)

    payload = {
        "model": settings.local_tts_model,
        "input": clean_text,
        "voice": voice,
        "response_format": "mp3",
    }
    headers = {"Content-Type": "application/json"}

    logger.info("Local TTS request to %s (model=%s, voice=%s)", url, settings.local_tts_model, voice)

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if not resp.is_success:
                logger.error("Local TTS error %s: %s", resp.status_code, resp.text)
            resp.raise_for_status()

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(resp.content)
        logger.info("Audio saved to %s (%d bytes)", output_path, len(resp.content))
        if settings.podcast_normalize_audio:
            normalize_audio(output_path)
        return True
    except httpx.ConnectError:
        logger.error(
            "Could not connect to local TTS server at %s — is vLLM-Omni running?", url
        )
        return False
    except Exception as exc:
        logger.error("Local TTS failed for agent %s: %s", agent_id, exc)
        return False
