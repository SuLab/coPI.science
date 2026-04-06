"""Shared utilities for podcast TTS backends."""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def strip_markdown(text: str) -> str:
    """Remove markdown formatting so TTS reads clean prose."""
    # Remove bold/italic markers (* and _)
    text = re.sub(r"\*+([^*]+)\*+", r"\1", text)
    text = re.sub(r"_+([^_]+)_+", r"\1", text)
    # Remove inline code
    text = re.sub(r"`[^`]+`", "", text)
    # Remove URLs but keep surrounding text
    text = re.sub(r"https?://\S+", "", text)
    return text.strip()


def get_audio_duration_seconds(audio_path: Path) -> int | None:
    """Return audio duration in seconds using mutagen, or None if unavailable."""
    try:
        from mutagen.mp3 import MP3
        audio = MP3(str(audio_path))
        return int(audio.info.length)
    except Exception as exc:
        logger.debug("Could not read audio duration from %s: %s", audio_path, exc)
        return None
