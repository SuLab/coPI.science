"""One-shot test: run the podcast pipeline for agent 'su' only.

Outputs:
  .labbot-tests/su-summary-<date>.txt   — generated text summary
  .labbot-tests/su-audio-<date>.mp3     — TTS audio (if MISTRAL_API_KEY is set)

Usage:
    DATABASE_URL=postgresql+asyncpg://copi:copi@localhost:5432/copi \
    python scripts/test_podcast_su.py
"""

import asyncio
import logging
import os
import shutil
from datetime import date
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(".labbot-tests")
AUDIO_DIR = Path("data/podcast_audio")


async def run():
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from src.config import get_settings
    from src.podcast.pipeline import (
        _generate_summary,
        _load_public_profile,
        _parse_profile_markdown,
        _select_article,
        _try_fetch_full_text,
    )
    from src.podcast.mistral_tts import generate_audio, get_audio_duration_seconds
    from src.podcast.pubmed_search import build_queries, fetch_candidates
    from src.podcast.state import get_delivered_pmids, record_delivery

    settings = get_settings()
    agent_id = "su"
    today = date.today()
    OUTPUT_DIR.mkdir(exist_ok=True)

    logger.info("=== LabBot Podcast test run for agent: %s ===", agent_id)

    # 1. Load profile
    profile_text = _load_public_profile(agent_id)
    if not profile_text:
        logger.error("No public profile found for agent: %s", agent_id)
        return
    logger.info("Loaded profile (%d chars)", len(profile_text))

    # 2. Build queries and fetch candidates
    profile_dict = _parse_profile_markdown(profile_text)
    queries = build_queries(profile_dict)
    logger.info("Search queries: %s", queries)

    already_delivered = get_delivered_pmids(agent_id)
    logger.info("Already delivered PMIDs: %s", already_delivered)

    candidates = await fetch_candidates(
        queries,
        already_delivered=already_delivered,
        days=settings.podcast_search_window_days,
        max_total=settings.podcast_max_candidates,
    )
    logger.info("Fetched %d candidates", len(candidates))
    if not candidates:
        logger.error("No candidate articles found — aborting")
        return

    # 3. LLM article selection
    selected, justification = await _select_article(profile_text, candidates, agent_id)
    if selected is None:
        logger.error("No article selected — aborting")
        return
    pmid = selected.get("pmid", "")
    logger.info("Selected PMID: %s", pmid)
    logger.info("Justification: %s", justification)

    # 4. Fetch full text
    full_text = await _try_fetch_full_text(pmid)
    logger.info("Full text fetched: %s", bool(full_text))

    # 5. Generate text summary
    summary = await _generate_summary(profile_text, selected, full_text, agent_id)
    if not summary:
        logger.error("Summary generation failed — aborting")
        return

    summary_path = OUTPUT_DIR / f"su-summary-{today.isoformat()}.txt"
    summary_path.write_text(summary, encoding="utf-8")
    logger.info("Summary written to %s", summary_path)
    print("\n" + "=" * 60)
    print("TEXT SUMMARY")
    print("=" * 60)
    print(summary)
    print("=" * 60 + "\n")

    # 6. Generate audio
    audio_src = AUDIO_DIR / agent_id / f"{today.isoformat()}.mp3"
    audio_ok = await generate_audio(summary, agent_id, audio_src)

    if audio_ok:
        audio_dest = OUTPUT_DIR / f"su-audio-{today.isoformat()}.mp3"
        shutil.copy2(audio_src, audio_dest)
        duration = get_audio_duration_seconds(audio_src)
        logger.info("Audio saved to %s (duration: %ss)", audio_dest, duration)
    else:
        logger.warning("Audio generation skipped or failed (check MISTRAL_API_KEY)")

    logger.info("=== Test run complete ===")
    logger.info("  PMID: %s", pmid)
    logger.info("  Summary: %s", summary_path)
    if audio_ok:
        logger.info("  Audio: %s", audio_dest)


if __name__ == "__main__":
    asyncio.run(run())
