"""Shared utilities for podcast TTS backends."""

import logging
import re
import shutil
import subprocess
import tempfile
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


def normalize_audio(audio_path: Path) -> bool:
    """Normalize audio loudness in-place using ffmpeg loudnorm (EBU R128).

    Targets -16 LUFS integrated loudness, -1.5 dBTP true peak — standard
    podcast levels. Writes to a temp file then atomically replaces the original.

    Returns True if normalization succeeded, False if ffmpeg is unavailable or
    the command fails (the original file is preserved on failure).
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.warning("ffmpeg not found on PATH — skipping audio normalization")
        return False

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-y",                          # overwrite tmp if it exists
                "-i", str(audio_path),
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-ar", "44100",
                str(tmp_path),
            ],
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            logger.error(
                "ffmpeg loudnorm failed (exit %d): %s",
                result.returncode,
                result.stderr.decode(errors="replace")[-500:],
            )
            tmp_path.unlink(missing_ok=True)
            return False

        tmp_path.replace(audio_path)
        logger.info("Audio normalized (loudnorm -16 LUFS) → %s", audio_path)
        return True
    except Exception as exc:
        logger.error("Audio normalization failed: %s", exc)
        tmp_path.unlink(missing_ok=True)
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
