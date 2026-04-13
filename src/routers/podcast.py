"""Podcast RSS feed, audio serving, and on-demand generation endpoints."""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.database import get_db, get_session_factory
from src.models.agent_registry import AgentRegistry
from src.models.podcast import PodcastEpisode
from src.podcast.rss import build_feed

logger = logging.getLogger(__name__)
router = APIRouter()

AUDIO_DIR = Path("data/podcast_audio")


@router.get("/{agent_id}/feed.xml", response_class=Response)
async def podcast_feed(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
):
    """RSS 2.0 podcast feed for a PI's daily research briefings."""
    # Verify agent exists
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == agent_id)
    )
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Fetch episodes newest-first
    episodes_result = await db.execute(
        select(PodcastEpisode)
        .where(PodcastEpisode.agent_id == agent_id)
        .order_by(PodcastEpisode.episode_date.desc())
        .limit(30)
    )
    episodes = episodes_result.scalars().all()

    settings = get_settings()
    base_url = settings.podcast_base_url or settings.base_url

    xml = build_feed(
        agent_id=agent_id,
        pi_name=agent.pi_name,
        episodes=episodes,
        base_url=base_url,
    )

    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


@router.get("/{agent_id}/audio/{date}.mp3")
async def podcast_audio(agent_id: str, date: str):
    """Stream a podcast audio file."""
    # Basic validation to prevent path traversal
    if "/" in date or ".." in date or not date.replace("-", "").isdigit():
        raise HTTPException(status_code=400, detail="Invalid date format")

    audio_path = AUDIO_DIR / agent_id / f"{date}.mp3"
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")

    return FileResponse(
        path=str(audio_path),
        media_type="audio/mpeg",
        filename=f"{agent_id}-{date}.mp3",
    )


async def _run_pipeline_background(agent_id: str, bot_name: str, pi_name: str, bot_token: str, slack_user_id: str | None) -> None:
    """Run the podcast pipeline in a background task with its own DB session."""
    from src.podcast.pipeline import run_pipeline_for_agent

    session_factory = get_session_factory()
    try:
        async with session_factory() as db:
            ok = await run_pipeline_for_agent(
                agent_id=agent_id,
                bot_name=bot_name,
                pi_name=pi_name,
                bot_token=bot_token,
                slack_user_id=slack_user_id,
                db_session=db,
            )
            await db.commit()
            logger.info("On-demand podcast pipeline for %s: %s", agent_id, "produced" if ok else "no episode")
    except Exception as exc:
        logger.error("On-demand podcast pipeline failed for %s: %s", agent_id, exc, exc_info=True)


@router.api_route("/{agent_id}/generate", methods=["GET", "POST"])
async def podcast_generate(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Trigger on-demand podcast generation for an agent.

    Returns immediately; pipeline runs in the background.
    Check the RSS feed or DB for the resulting episode.
    """
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == agent_id)
    )
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    settings = get_settings()
    slack_tokens = settings.get_slack_tokens()
    bot_token = agent.slack_bot_token or slack_tokens.get(agent_id, {}).get("bot", "")

    asyncio.create_task(
        _run_pipeline_background(
            agent_id=agent_id,
            bot_name=agent.bot_name,
            pi_name=agent.pi_name,
            bot_token=bot_token,
            slack_user_id=agent.slack_user_id,
        )
    )

    return {"status": "started", "agent_id": agent_id, "message": f"Podcast pipeline started for {agent.pi_name}. Check the RSS feed shortly."}
